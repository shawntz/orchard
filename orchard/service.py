"""Research workflow independent of HTTP and UI."""
import csv
import hashlib
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import urlsplit
from .db import Database, now, uid, dump, audit
from .terms import parse_document, normalize, build_query
from .providers import EuropePMC, normalize_epmc


class Conflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def doi_normalize(doi):
    if not isinstance(doi, str):
        return None
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', doi.strip(), flags=re.I).lower()
    return value if re.fullmatch(r'10\.\d{4,9}/\S+', value) else None


def plain(text):
    return unescape(re.sub(r'<[^>]+>', ' ', text or '')).strip()


def title_key(value):
    return re.sub(r'[^\w]+', ' ', normalize(plain(value))).strip()


REPOS = ('osf.io', 'zenodo.org', 'datadryad.org', 'figshare.com', 'openneuro.org', 'github.com',
         'gitlab.com', 'data.mendeley.com', 'dataverse.harvard.edu', 'gin.g-node.org')
FIELDS = ('target_finding', 'dataset_identity', 'data_level', 'timing_metadata', 'acquisition',
          'original_workflow', 'confounds', 'reuse_feasibility')


class Service:
    def __init__(self, path, provider=None):
        self.db = Database(path)
        self.provider = provider or EuropePMC()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='orchard')
        self.jobs = set()
        self.jobs_lock = threading.Lock()
        self.search_lock = threading.Lock()
        with self.db.connect() as c:
            for row in c.execute("SELECT id FROM runs WHERE status IN ('queued','running')").fetchall():
                c.execute("UPDATE runs SET status='interrupted',error='Application stopped during search; resume explicitly.' WHERE id=?", (row['id'],))
                audit(c, 'search_interrupted', row['id'])

    def close(self):
        self.pool.shutdown(wait=True)

    def require(self, table, id):
        if table not in ('documents', 'papers', 'queries', 'runs', 'duplicates', 'resources'):
            raise ValueError('Invalid entity')
        row = self.db.one(f'SELECT * FROM {table} WHERE id=?', (id,))
        if not row:
            raise KeyError('Record not found.')
        return row

    def set_project(self, name, question):
        if not name.strip():
            raise ValueError('Project name is required.')
        with self.db.connect() as c:
            previous = dict(c.execute('SELECT * FROM project').fetchone())
            c.execute('UPDATE project SET name=?,question=? WHERE id=1', (name.strip(), question.strip() or None))
            audit(c, 'project_updated', '1', {'before': previous, 'name': name, 'question': question})

    def import_document(self, name, text):
        groups, unparsed = parse_document(text)
        old = self.db.one('SELECT id FROM documents WHERE sha256=?', (digest(text),))
        if old:
            return {'id': old['id'], 'reused': True}
        id = uid()
        with self.db.connect() as c:
            c.execute('INSERT INTO documents VALUES(?,?,?,?,?)', (id, name[:200], text, digest(text), now()))
            for group in groups:
                cid = uid()
                c.execute('INSERT INTO concepts VALUES(?,?,?)', (cid, id, group['label']))
                for term in group['terms']:
                    c.execute('INSERT INTO terms VALUES(?,?,?,?,?,?)', (uid(), cid, term['original'], term['normalized'], term['line'], term['reason']))
            audit(c, 'document_imported', id, {'name': name, 'sha256': digest(text), 'groups': len(groups), 'unparsed_lines': len(unparsed)})
        return {'id': id, 'reused': False}

    def document(self, id):
        doc = self.require('documents', id)
        doc['concepts'] = self.db.rows('SELECT * FROM concepts WHERE document_id=?', (id,))
        for concept in doc['concepts']:
            concept['terms'] = self.db.rows('SELECT * FROM terms WHERE concept_id=? ORDER BY line,rowid', (concept['id'],))
        _, doc['unparsed'] = parse_document(doc['content'])
        return doc

    def save_query(self, label, query, term_ids=None, document_id=None, parent_id=None, approved=False, rationale='Researcher-authored query'):
        if not isinstance(query, str) or not query.strip() or len(query) > 6000:
            raise ValueError('Query must contain 1–6,000 characters.')
        if document_id:
            self.require('documents', document_id)
        if parent_id:
            self.require('queries', parent_id)
        term_ids = term_ids or []
        for tid in term_ids:
            if not self.db.one('SELECT id FROM terms WHERE id=?', (tid,)):
                raise ValueError('Unknown source term.')
        id = uid()
        with self.db.connect() as c:
            c.execute('INSERT INTO queries VALUES(?,?,?,?,?,?,?,?,?,?)', (id, parent_id, document_id, label[:160] or 'Search', query.strip(), self.provider.name, dump(term_ids), rationale, int(approved), now()))
            audit(c, 'query_approved' if approved else 'query_drafted', id, {'parent_id': parent_id, 'term_ids': term_ids})
        return self.require('queries', id)

    def generate_query(self, term_groups, document_id, label):
        groups, all_ids = [], []
        for group in term_groups:
            originals = []
            for tid in group:
                term = self.db.one('SELECT t.* FROM terms t JOIN concepts c ON c.id=t.concept_id WHERE t.id=? AND c.document_id=?', (tid, document_id))
                if not term:
                    raise ValueError('Term is not in this document.')
                originals.append(term['original'])
                all_ids.append(tid)
            groups.append(originals)
        query = build_query(groups)
        return self.save_query(label, query, all_ids, document_id, rationale='OR within selected source groups; AND between groups. No date, human-only, open-data, or exclusion filters added automatically.')

    def search(self, query_id, action='new', max_records=300, background=True):
        with self.search_lock:
            return self._search(query_id, action, max_records, background)

    def _search(self, query_id, action='new', max_records=300, background=True):
        q = self.require('queries', query_id)
        if not q['approved']:
            raise ValueError('Review and approve a query version before searching.')
        if action not in ('new', 'refresh', 'reuse') or not isinstance(max_records, int) or not 1 <= max_records <= 5000:
            raise ValueError('Choose a valid action and a retrieval cap from 1 to 5,000.')
        parameters = {'max_records_per_attempt': max_records, 'resultType': 'core', 'adapter_version': self.provider.version}
        # Do not reorder Boolean expressions or change spaces inside quoted phrases.
        fingerprint = digest(dump({'provider': q['provider'], 'query': q['query'].strip(), 'parameters': parameters}))
        prior = self.db.one('SELECT * FROM runs WHERE fingerprint=? ORDER BY rowid DESC LIMIT 1', (fingerprint,))
        if not prior and action == 'reuse':
            raise Conflict('No identical prior search exists. Choose Run search to create one.')
        if prior and action == 'reuse':
            return {'run': prior, 'reused': True}
        if prior and action == 'new':
            raise Conflict('An identical search exists. Choose reuse or refresh explicitly.')
        id = uid()
        with self.db.connect() as c:
            c.execute('INSERT INTO runs(id,query_id,query,provider,fingerprint,parameters,status,created,cursor,refresh_of) VALUES(?,?,?,?,?,?,?,?,?,?)', (id, q['id'], q['query'], q['provider'], fingerprint, dump(parameters), 'queued', now(), '*', prior['id'] if prior else None))
            audit(c, 'search_queued', id, {'query_id': q['id'], 'action': action, 'parameters': parameters})
        if background:
            self.submit(id)
        else:
            self.execute(id)
        return {'run': self.require('runs', id), 'reused': False}

    def submit(self, id):
        with self.jobs_lock:
            if id in self.jobs:
                raise Conflict('This search is already running.')
            self.jobs.add(id)
        def job():
            try:
                self.execute(id)
            finally:
                with self.jobs_lock:
                    self.jobs.discard(id)
        self.pool.submit(job)

    def resume(self, id):
        run = self.require('runs', id)
        if run['status'] not in ('failed', 'partial', 'interrupted') or run['provider'] != self.provider.name:
            raise Conflict('Only incomplete provider searches can be resumed.')
        with self.db.connect() as c:
            c.execute("UPDATE runs SET status='queued',error=NULL WHERE id=?", (id,))
            audit(c, 'search_resume_requested', id, {'cursor': run['cursor']})
        self.submit(id)
        return self.require('runs', id)

    def request_event(self, id, detail):
        with self.db.connect() as c:
            audit(c, 'provider_request_error', id, detail)

    def execute(self, id):
        run = self.require('runs', id)
        params = json.loads(run['parameters'])
        upper = run['retrieved'] + params['max_records_per_attempt']
        cursor, seen = run['cursor'] or '*', set()
        try:
            with self.db.connect() as c:
                c.execute("UPDATE runs SET status='running',error=NULL,finished=NULL WHERE id=?", (id,))
                audit(c, 'search_started', id, {'cursor': cursor})
            while True:
                count = self.db.one('SELECT COUNT(*) n FROM discoveries WHERE run_id=?', (id,))['n']
                page = self.provider.page(run['query'], cursor, min(100, upper-count), lambda d:self.request_event(id, d))
                with self.db.connect() as c:
                    audit(c, 'provider_page_received', id, {'cursor': cursor, 'records': len(page.records), 'hit_count': page.total, 'next_cursor': page.cursor})
                    c.execute('UPDATE runs SET total=? WHERE id=?', (page.total, id))
                for raw in page.records:
                    self.ingest(id, raw)
                count = self.db.one('SELECT COUNT(*) n FROM discoveries WHERE run_id=?', (id,))['n']
                complete = count >= page.total
                with self.db.connect() as c:
                    c.execute('UPDATE runs SET cursor=?,retrieved=? WHERE id=?', (page.cursor or cursor, count, id))
                if complete:
                    self.finish(id, 'completed')
                    return
                if not page.records or not page.cursor or page.cursor in seen or page.cursor == cursor:
                    self.finish(id, 'partial', 'Provider pagination ended before its reported hit count; coverage is incomplete.')
                    return
                if count >= upper:
                    self.finish(id, 'partial', 'Retrieval cap reached. Resume to retrieve another batch; the full result set has not been collected.')
                    return
                seen.add(cursor)
                cursor = page.cursor
        except Exception as exc:
            count = self.db.one('SELECT COUNT(*) n FROM discoveries WHERE run_id=?', (id,))['n']
            self.finish(id, 'partial' if count else 'failed', f'{type(exc).__name__}: {exc}')

    def finish(self, id, status, error=None):
        with self.db.connect() as c:
            count = c.execute('SELECT COUNT(*) FROM discoveries WHERE run_id=?', (id,)).fetchone()[0]
            c.execute('UPDATE runs SET status=?,error=?,finished=?,retrieved=? WHERE id=?', (status, error, now(), count, id))
            audit(c, 'search_' + status, id, {'retrieved': count, 'error': error})

    def ingest(self, run_id, raw):
        try:
            norm = normalize_epmc(raw)
        except Exception as exc:
            with self.db.connect() as c:
                audit(c, 'provider_record_rejected', run_id, {'raw': raw, 'error': str(exc)})
            raise
        norm['doi'] = doi_normalize(norm['doi'])
        for key in ('title', 'abstract', 'authors', 'year', 'publication_type'):
            if norm[key] is not None and not isinstance(norm[key], str):
                raise ValueError('Invalid metadata type: ' + key)
        ids = [('europepmc', norm['source_id'])]
        for key in ('doi', 'pmid', 'pmcid'):
            if norm.get(key):
                ids.append((key, str(norm[key])))
        raw_json = dump(raw)
        with self.db.connect() as c:
            previous = c.execute('SELECT * FROM source_records WHERE provider=? AND source_id=? AND sha256=?', ('europepmc', norm['source_id'], digest(raw_json))).fetchone()
            if previous:
                pid, sid = previous['paper_id'], previous['id']
            else:
                matches = set()
                for kind, value in ids:
                    match = c.execute('SELECT paper_id FROM identifiers WHERE kind=? AND value=?', (kind, value)).fetchone()
                    if match:
                        matches.add(match['paper_id'])
                pid = next(iter(matches)) if len(matches) == 1 else uid()
                if len(matches) != 1:
                    c.execute('INSERT INTO papers(id,title,abstract,authors,year,doi,publication_type,created) VALUES(?,?,?,?,?,?,?,?)', (pid, norm['title'], norm['abstract'], norm['authors'], norm['year'], norm['doi'], norm['publication_type'], now()))
                    key = title_key(norm['title'])
                    for other in c.execute('SELECT * FROM papers WHERE id<>? AND merged_into IS NULL', (pid,)).fetchall():
                        other_key = title_key(other['title'])
                        similarity = SequenceMatcher(None, key, other_key).ratio() if key and other_key else 0
                        if other['id'] in matches or (len(key) > 12 and similarity >= 0.88):
                            reason = 'Conflicting strong identifiers' if other['id'] in matches else f'Title similarity {similarity:.2f}; year {norm["year"]!r}/{other["year"]!r}; human review required'
                            a, b = sorted((pid, other['id']))
                            c.execute('INSERT OR IGNORE INTO duplicates VALUES(?,?,?,?,?,?)', (uid(), a, b, reason, 'pending', now()))
                    audit(c, 'paper_created', pid, {'source_id': norm['source_id']})
                else:
                    # First non-missing source value wins; every conflicting value remains in raw records.
                    for field in ('title', 'abstract', 'authors', 'year', 'doi', 'publication_type'):
                        c.execute(f'UPDATE papers SET {field}=COALESCE({field},?) WHERE id=?', (norm[field], pid))
                for kind, value in ids:
                    c.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?)', (kind, value, pid))
                sid = uid()
                c.execute('INSERT INTO source_records VALUES(?,?,?,?,?,?,?)', (sid, 'europepmc', norm['source_id'], pid, raw_json, digest(raw_json), now()))
                audit(c, 'source_record_stored', sid, {'paper_id': pid, 'sha256': digest(raw_json)})
                if norm['doi']:
                    self.add_resource(c, pid, 'landing', 'https://doi.org/' + norm['doi'], 'DOI from provider metadata', 'metadata')
                if norm.get('pmcid') and re.fullmatch(r'PMC\d+', norm['pmcid']):
                    self.add_resource(c, pid, 'fulltext', 'https://europepmc.org/articles/' + norm['pmcid'], 'PMCID from provider; access not yet checked', 'metadata')
                links = raw.get('fullTextUrlList', {}).get('fullTextUrl', [])
                if isinstance(links, list):
                    for link in links:
                        if isinstance(link, dict) and isinstance(link.get('url'), str):
                            self.add_resource(c, pid, 'fulltext', link['url'], dump(link), 'metadata')
                self.extract_resources(c, pid, plain(norm['abstract']), 'abstract')
            c.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)', (run_id, sid, now()))
            return pid

    def import_records(self, name, payload):
        records = payload.get('resultList', {}).get('result') if isinstance(payload, dict) else payload
        if not isinstance(records, list) or not records or len(records) > 5000:
            raise ValueError('Import 1–5,000 Europe PMC core records as an array or a REST response.')
        id = uid()
        with self.db.connect() as c:
            c.execute('INSERT INTO runs(id,query,provider,fingerprint,parameters,status,created) VALUES(?,?,?,?,?,?,?)', (id, 'Manual import: ' + name[:160], 'manual-import', digest(dump(records)), dump({'name': name, 'expected_records': len(records)}), 'running', now()))
            audit(c, 'records_import_started', id, {'name': name, 'count': len(records)})
        try:
            for record in records:
                self.ingest(id, record)
            self.finish(id, 'completed')
        except Exception as exc:
            self.finish(id, 'partial', str(exc))
        return self.require('runs', id)

    def add_resource(self, c, pid, kind, url, evidence, origin):
        if url and (not isinstance(url, str) or urlsplit(url).scheme not in ('https','http')):
            return
        c.execute('INSERT OR IGNORE INTO resources(id,paper_id,kind,url,repository,evidence,origin,created) VALUES(?,?,?,?,?,?,?,?)', (uid(), pid, kind, url, urlsplit(url).hostname if url else None, evidence[:8000], origin, now()))

    def extract_resources(self, c, pid, text, origin):
        for line in text.splitlines():
            if re.search(r'data.{0,100}(?:upon|on) (?:reasonable )?request', line, re.I):
                self.add_resource(c, pid, 'data_statement', '', line, origin)
                c.execute("UPDATE resources SET status='upon_request' WHERE paper_id=? AND kind='data_statement' AND url=''", (pid,))
            for match in re.finditer(r'https?://[^\s<>"\]\}]+', line):
                url = match.group(0).rstrip('.,;)')
                context = line[max(0,match.start()-250):match.end()+250]
                host = urlsplit(url).hostname or ''
                recognized = any(host == r or host.endswith('.'+r) for r in REPOS)
                is_data = bool(re.search(r'\b(data|dataset|datasets|code|scripts|supplement|supplementary|materials|preregistration|protocol)\b', context, re.I))
                if not is_data:
                    continue
                kind = 'code' if re.search(r'\b(code|scripts)\b', context, re.I) else 'dataset'
                if re.search(r'\b(preregistration|protocol)\b', context, re.I):
                    kind = 'protocol'
                elif 'supplement' in context.lower():
                    kind = 'supplement'
                self.add_resource(c, pid, kind, url, context, origin + ('; repository recognized' if recognized else '; context candidate'))

    def paper_ids(self, id):
        self.require('papers', id)
        return [r['id'] for r in self.db.rows('WITH RECURSIVE family(id) AS (SELECT ? UNION ALL SELECT p.id FROM papers p JOIN family f ON p.merged_into=f.id) SELECT id FROM family', (id,))]

    def paper(self, id):
        paper = self.require('papers', id)
        ids = self.paper_ids(id)
        marks = ','.join('?' for _ in ids)
        for key, table in [('sources','source_records'),('resources','resources'),('decisions','decisions'),('assessments','assessments'),('fulltexts','fulltexts')]:
            paper[key] = self.db.rows(f'SELECT * FROM {table} WHERE paper_id IN ({marks}) ORDER BY rowid DESC', ids)
        paper['discoveries'] = self.db.rows(f'SELECT d.*,r.query,r.provider,r.status FROM discoveries d JOIN source_records s ON s.id=d.source_record_id JOIN runs r ON r.id=d.run_id WHERE s.paper_id IN ({marks})', ids)
        paper['identifiers'] = self.db.rows(f'SELECT * FROM identifiers WHERE paper_id IN ({marks})', ids)
        paper['relationships'] = self.db.rows(f'SELECT * FROM relationships WHERE a IN ({marks}) OR b IN ({marks})', ids+ids)
        return paper

    def save_criteria(self, inclusion, exclusion):
        if not inclusion.strip() and not exclusion.strip():
            raise ValueError('Enter your eligibility criteria before screening.')
        id = uid()
        with self.db.connect() as c:
            c.execute('INSERT INTO criteria VALUES(?,?,?,?)', (id, inclusion.strip(), exclusion.strip(), now()))
            audit(c, 'criteria_version_created', id)
        return {'id': id}

    def screen(self, id, stage, decision, reason='', notes='', reviewer='Local researcher', revision=0, criteria_id=None):
        allowed = {'title_abstract': {'include','exclude','uncertain','needs_information'}, 'fulltext': {'include','exclude','uncertain','awaiting_fulltext','inaccessible'}}
        if stage not in allowed or decision not in allowed[stage]:
            raise ValueError('Invalid screening stage or decision.')
        if decision == 'exclude' and not reason.strip():
            raise ValueError('An exclusion reason is required.')
        if not reviewer.strip():
            raise ValueError('Reviewer name is required.')
        self.require('papers', id)
        with self.db.connect() as c:
            criteria = c.execute('SELECT id FROM criteria ORDER BY rowid DESC LIMIT 1').fetchone()
            if not criteria:
                raise ValueError('Configure eligibility criteria before screening.')
            if criteria_id and criteria_id != criteria['id']:
                raise Conflict('Eligibility criteria changed. Reload and review them before saving.')
            if c.execute('UPDATE papers SET revision=revision+1 WHERE id=? AND revision=? AND merged_into IS NULL', (id, revision)).rowcount != 1:
                raise Conflict('Paper changed in another view. Reload before saving.')
            did = uid()
            c.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)', (did, id, stage, decision, reason.strip(), notes, reviewer, criteria['id'], now()))
            audit(c, 'screening_decision_saved', id, {'decision_id': did, 'stage': stage, 'decision': decision, 'criteria_id': criteria['id']})
        return self.paper(id)

    def assess(self, id, fields):
        self.require('papers', id)
        clean = {k:str(v)[:8000] for k,v in fields.items() if k in FIELDS}
        with self.db.connect() as c:
            aid=uid()
            c.execute('INSERT INTO assessments VALUES(?,?,?,?)', (aid,id,dump(clean),now()))
            audit(c,'feasibility_assessment_saved',id,{'assessment_id':aid})
        return {'id':aid}

    def update_metadata(self, id, fields):
        before = self.require('papers', id)
        allowed = ('title','abstract','authors','year','publication_type')
        with self.db.connect() as c:
            for key,value in fields.items():
                if key in allowed:
                    c.execute(f'UPDATE papers SET {key}=?, revision=revision+1 WHERE id=?', (str(value) or None,id))
            audit(c,'metadata_corrected',id,{'before':before,'changes':{k:v for k,v in fields.items() if k in allowed}})
        return self.paper(id)

    def resolve_duplicate(self, id, action, reason):
        pair = self.require('duplicates', id)
        if pair['status'] != 'pending' or action not in ('merge','distinct','related') or not reason.strip():
            raise ValueError('Choose merge, distinct, or related and explain the decision.')
        a,b = pair['a'],pair['b']
        with self.db.connect() as c:
            if any(c.execute('SELECT merged_into FROM papers WHERE id=?',(p,)).fetchone()[0] for p in (a,b)):
                raise Conflict('One record was already merged; refresh the queue.')
            if action == 'merge':
                c.execute('UPDATE papers SET merged_into=?,revision=revision+1 WHERE id=?',(a,b))
                c.execute('UPDATE papers SET revision=revision+1 WHERE id=?',(a,))
                c.execute('UPDATE identifiers SET paper_id=? WHERE paper_id=?',(a,b))
                # Raw records and decisions stay attached to their original IDs; family queries preserve their history.
                c.execute("UPDATE duplicates SET status='superseded' WHERE status='pending' AND (a=? OR b=?)",(b,b))
            c.execute('UPDATE duplicates SET status=? WHERE id=?',(action,id))
            c.execute('INSERT INTO relationships VALUES(?,?,?,?,?,?)',(uid(),a,b,action,reason,now()))
            audit(c,'duplicate_resolved',id,{'a':a,'b':b,'action':action,'reason':reason})
        return {'canonical_id':a}

    def import_fulltext(self, id, text, origin):
        self.require('papers', id)
        if not isinstance(text,str) or not text.strip() or len(text)>4_000_000:
            raise ValueError('Provide non-empty full text, up to 4 MB.')
        with self.db.connect() as c:
            fid=uid()
            c.execute('INSERT INTO fulltexts VALUES(?,?,?,?,?,?)',(fid,id,text,digest(text),origin,now()))
            self.extract_resources(c,id,text,origin)
            audit(c,'fulltext_imported',id,{'fulltext_id':fid,'origin':origin,'sha256':digest(text)})
        return self.paper(id)

    def fetch_fulltext(self, id):
        paper=self.paper(id)
        pmcid=next((i['value'] for i in paper['identifiers'] if i['kind']=='pmcid'),None)
        if not pmcid:
            raise ValueError('No PMCID is available. Open the source links or import authorized text manually.')
        try:
            text,url=self.provider.fulltext(pmcid,lambda d:self.request_event(id,d))
            return self.import_fulltext(id,text,url)
        except Exception as exc:
            with self.db.connect() as c:
                audit(c,'fulltext_retrieval_failed',id,{'error':str(exc),'pmcid':pmcid})
            raise ValueError('Full text could not be retrieved: '+str(exc)) from exc

    def resource_action(self, id, action, note=''):
        r=self.require('resources',id)
        if action not in ('verify','confirm','reject'):
            raise ValueError('Invalid resource action.')
        status,detail='found_unverified',note
        if action=='verify':
            if not r['url']:
                raise ValueError('This is an availability statement, without a URL.')
            try:
                _, final, _=self.provider.transport.get(r['url'],limit=8_000_000)
                status='url_reachable'
                detail='HTTP retrieval succeeded at '+final+'. This does not establish downloadable data, its license, or its relationship to this paper.'
            except Exception as exc:
                status='access_failed'
                detail=str(exc)
        elif action=='confirm':
            if not note.strip():
                raise ValueError('Record what you inspected and verified.')
            status='researcher_verified'
        else:
            if not note.strip():
                raise ValueError('Explain why this link was rejected.')
            status='rejected'
        with self.db.connect() as c:
            c.execute('UPDATE resources SET status=?,access_note=?,verified=? WHERE id=?',(status,detail,now(),id))
            audit(c,'resource_'+action,id,{'before':r,'status':status,'note':detail})
        return self.require('resources',id)

    def listing(self, q='', page=1, status='', year='', source='', resource_kind=''):
        where=['p.merged_into IS NULL']; args=[]
        if q:
            where.append('(p.title LIKE ? OR p.abstract LIKE ? OR p.authors LIKE ? OR p.doi LIKE ?)'); args += ['%'+q+'%']*4
        if year:
            where.append('p.year=?'); args.append(year)
        if source:
            where.append('EXISTS(SELECT 1 FROM source_records s WHERE s.paper_id=p.id AND s.provider=?)'); args.append(source)
        if resource_kind:
            where.append('EXISTS(SELECT 1 FROM resources r WHERE r.paper_id=p.id AND r.kind=?)'); args.append(resource_kind)
        # Include prior merged record screening history in canonical view.
        status_sql="COALESCE((SELECT d.decision FROM decisions d WHERE d.paper_id=p.id OR d.paper_id IN (WITH RECURSIVE f(id) AS (SELECT p.id UNION ALL SELECT x.id FROM papers x JOIN f ON x.merged_into=f.id) SELECT id FROM f) ORDER BY d.rowid DESC LIMIT 1),'not_screened')"
        if status:
            where.append(status_sql+'=?'); args.append(status)
        condition=' AND '.join(where)
        count=self.db.one('SELECT COUNT(*) n FROM papers p WHERE '+condition,args)['n']
        page=max(1,int(page))
        rows=self.db.rows('SELECT p.*,'+status_sql+' AS screening_status,'+status_sql.replace('d.decision','d.stage')+' AS screening_stage FROM papers p WHERE '+condition+' ORDER BY p.created DESC LIMIT 25 OFFSET ?',args+[(page-1)*25])
        return {'items':rows,'total':count,'page':page,'pages':max(1,(count+24)//25)}

    def overview(self):
        counts={}
        with self.db.connect() as c:
            for name,sql in {
                'papers':'SELECT COUNT(*) FROM papers WHERE merged_into IS NULL',
                'source_records':'SELECT COUNT(*) FROM source_records',
                'searches':"SELECT COUNT(*) FROM runs WHERE provider<>'manual-import'",
                'incomplete_searches':"SELECT COUNT(*) FROM runs WHERE status IN ('partial','failed','interrupted')",
                'pending_duplicates':"SELECT COUNT(*) FROM duplicates WHERE status='pending'",
                'data_links':"SELECT COUNT(*) FROM resources WHERE kind IN ('dataset','code','supplement') AND status<>'rejected'",
                'merged_records':'SELECT COUNT(*) FROM papers WHERE merged_into IS NOT NULL',
                'screening_events':'SELECT COUNT(*) FROM decisions'
            }.items(): counts[name]=c.execute(sql).fetchone()[0]
        return {'project':self.db.one('SELECT * FROM project'), 'counts':counts,
                'criteria':self.db.one('SELECT * FROM criteria ORDER BY rowid DESC LIMIT 1'),
                'runs':self.db.rows('SELECT * FROM runs ORDER BY rowid DESC LIMIT 8'),
                'provider':{'name':'Europe PMC','configured':True,'live_verified':False,'note':'No API key required. Live access depends on your network. Check each run status.'}}

    def export(self):
        tables=('project','documents','concepts','terms','queries','criteria','runs','papers','identifiers','source_records','discoveries','duplicates','relationships','decisions','resources','fulltexts','assessments','audit')
        with self.db.connect() as c:
            eid=uid(); audit(c,'export_created',eid,{'tables':tables})
            # One read transaction for a consistent snapshot, including this export event.
            data={t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
        data['manifest']={'schema_version':1,'application_version':'0.1.0','exported_at':now(),
                          'counts':{'papers': sum(p['merged_into'] is None for p in data['papers']), 'source_records':len(data['source_records']), 'discovery_events':len(data['discoveries']), 'screening_events':len(data['decisions'])},
                          'study_count':None,'prisma_note':'Records and reports are tracked. Studies are not mapped; study counts and a complete PRISMA diagram are not available.'}
        return data

    def export_zip(self):
        import zipfile
        data=self.export(); buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('orchard-export.json',json.dumps(data,ensure_ascii=False,indent=2))
            for table,rows in data.items():
                if table=='manifest': continue
                output=io.StringIO(newline='')
                columns=list(rows[0]) if rows else [r['name'] for r in self.db.rows('PRAGMA table_info('+table+')')]
                writer=csv.DictWriter(output,fieldnames=columns); writer.writeheader()
                if rows:
                    for row in rows:
                        # Neutralize spreadsheet formulas. JSON remains lossless.
                        safe={k: ("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')) else v) for k,v in row.items()}
                        writer.writerow(safe)
                z.writestr(table+'.csv',output.getvalue())
            report=['ORCHARD SEARCH STRATEGY REPORT', 'Exported: '+data['manifest']['exported_at'],
                    'Unknown counts remain unknown. Study-level eligibility has not been inferred.', '']
            for run in data['runs']:
                report += [f"Run {run['id']} | {run['provider']} | {run['status']}",run['query'],
                           f"Parameters: {run['parameters']}",f"Started: {run['created']} | Finished: {run['finished']}",
                           f"Stored: {run['retrieved']} | Provider hit count: {run['total']} | Error: {run['error']}",'']
            z.writestr('search-strategy.txt','\n'.join(report))
        return buffer.getvalue()
