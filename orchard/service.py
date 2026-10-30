"""Research workflow independent of HTTP and UI."""
import csv
import hashlib
import io
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit
from .db import Database, now, uid, dump, audit
from .terms import parse_document, normalize, build_query
from .providers import EuropePMC, PMCCloud, PDF_LIMIT, Transport, is_pdf, normalize_epmc
from . import eyelink, manuscript


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
SCAN_SCOPES = ('unscanned', 'all', 'included')
MAX_REPOSITORIES = 15
DATA_CONTEXT = re.compile(r'\b(data|dataset|datasets|available|availability|deposited|repository|raw|eye[- ]?tracking|eyelink|pupil)', re.I)
FIELDS = ('target_finding', 'dataset_identity', 'data_level', 'timing_metadata', 'acquisition',
          'original_workflow', 'confounds', 'reuse_feasibility')


class Service:
    def __init__(self, path, provider=None, transport=None, pdf_source=None):
        self.db = Database(path)
        self.provider = provider or EuropePMC()
        # Shared, rate-limited client for PDFs and repository APIs.
        self.web = transport or getattr(self.provider, 'transport', None) or Transport()
        self.pdf_source = pdf_source or PMCCloud(self.web)
        self.pdf_dir = Path(path).parent / 'pdfs'
        self.scan_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='orchard')
        self.jobs = set()
        self.jobs_lock = threading.Lock()
        self.search_lock = threading.Lock()
        with self.db.connect() as c:
            for row in c.execute("SELECT id FROM runs WHERE status IN ('queued','running')").fetchall():
                c.execute("UPDATE runs SET status='interrupted',error='Application stopped during search; resume explicitly.' WHERE id=?", (row['id'],))
                audit(c, 'search_interrupted', row['id'])
            for row in c.execute("SELECT id FROM eyelink_scans WHERE status IN ('queued','running')").fetchall():
                c.execute("UPDATE eyelink_scans SET status='interrupted',error='Application stopped during the scan; start a new scan.' WHERE id=?", (row['id'],))
                audit(c, 'eyelink_scan_interrupted', row['id'])

    def close(self):
        self.pool.shutdown(wait=True)

    def require(self, table, id):
        if table not in ('documents', 'papers', 'queries', 'runs', 'duplicates', 'resources', 'pdfs', 'eyelink_scans'):
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

    def submit(self, id, target=None):
        with self.jobs_lock:
            if id in self.jobs:
                raise Conflict('This search is already running.')
            self.jobs.add(id)
        def job():
            try:
                (target or self.execute)(id)
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
        for key, table in [('sources','source_records'),('resources','resources'),('decisions','decisions'),('assessments','assessments'),('fulltexts','fulltexts'),('pdfs','pdfs')]:
            paper[key] = self.db.rows(f'SELECT * FROM {table} WHERE paper_id IN ({marks}) ORDER BY rowid DESC', ids)
        paper['discoveries'] = self.db.rows(f'SELECT d.*,r.query,r.provider,r.status FROM discoveries d JOIN source_records s ON s.id=d.source_record_id JOIN runs r ON r.id=d.run_id WHERE s.paper_id IN ({marks})', ids)
        paper['identifiers'] = self.db.rows(f'SELECT * FROM identifiers WHERE paper_id IN ({marks})', ids)
        paper['relationships'] = self.db.rows(f'SELECT * FROM relationships WHERE a IN ({marks}) OR b IN ({marks})', ids+ids)
        paper['eyelink_scans'] = [self.eyelink_scan(r['id']) for r in self.db.rows(f'SELECT id FROM eyelink_scans WHERE paper_id IN ({marks}) ORDER BY rowid DESC LIMIT 3', ids)]
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

    def store_pdf(self, id, data, name, origin, license=None, detail=None):
        self.require('papers', id)
        if not is_pdf(data):
            raise ValueError('The file is not a PDF.')
        if len(data) > PDF_LIMIT:
            raise ValueError('PDFs are limited to 60 MB.')
        sha = hashlib.sha256(data).hexdigest()
        ids = self.paper_ids(id)
        marks = ','.join('?' for _ in ids)
        existing = self.db.one(f'SELECT * FROM pdfs WHERE sha256=? AND paper_id IN ({marks})', [sha]+ids)
        if existing:
            return {'pdf': existing, 'reused': True}
        # Content-addressed files: identical bytes are stored once; rows keep per-paper provenance.
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        file = self.pdf_dir / (sha + '.pdf')
        if not file.exists():
            tmp = file.with_suffix('.tmp-' + uid())
            tmp.write_bytes(data)
            os.replace(tmp, file)
        name = re.sub(r'[\x00-\x1f/\\]+', '_', Path(str(name or '')).name).strip()[:200] or 'article.pdf'
        pid = uid()
        with self.db.connect() as c:
            c.execute('INSERT INTO pdfs VALUES(?,?,?,?,?,?,?,?)', (pid, id, name, origin, sha, len(data), license, now()))
            audit(c, 'pdf_stored', id, {'pdf_id': pid, 'origin': origin, 'sha256': sha, 'size': len(data), 'license': license, **(detail or {})})
        return {'pdf': self.require('pdfs', pid), 'reused': False}

    def pdf_file(self, id):
        row = self.require('pdfs', id)
        file = self.pdf_dir / (row['sha256'] + '.pdf')
        if not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) or not file.is_file():
            raise KeyError('The PDF file is missing from the data folder.')
        return row, file.read_bytes()

    def pdf_routes(self, paper):
        """Open-access routes in order of reliability: PMC dataset, then provider-listed PDF links."""
        found = [('pmc', i['value']) for i in paper['identifiers'] if i['kind'] == 'pmcid' and re.fullmatch(r'PMC\d+', i['value'])]
        for source in paper['sources']:
            links = json.loads(source['raw']).get('fullTextUrlList', {}).get('fullTextUrl', [])
            for link in links if isinstance(links, list) else []:
                if isinstance(link, dict) and link.get('documentStyle') == 'pdf' and isinstance(link.get('url'), str) and link['url'].startswith(('https://', 'http://')):
                    found.append(('url', link['url']))
        return list(dict.fromkeys(found))[:6]

    def fetch_pdf(self, id):
        paper = self.paper(id)
        candidates = self.pdf_routes(paper)
        if not candidates:
            raise ValueError('No PMCID or open-access PDF link is recorded for this paper. Download it through your library and upload the PDF.')
        attempts = []
        for kind, value in candidates:
            try:
                if kind == 'pmc':
                    data, url, info = self.pdf_source.pdf(value, lambda d: self.request_event(id, d))
                    return self.store_pdf(id, data, info['version'] + '.pdf', url, info.get('license'), {'source': 'PMC article dataset', **info})
                data, url = self.pdf_source.download(value, lambda d: self.request_event(id, d))
                return self.store_pdf(id, data, Path(urlsplit(url).path).name or 'article.pdf', url, None, {'source': 'Provider full-text link', 'listed_url': value})
            except Exception as exc:
                attempts.append({'source': value, 'error': str(exc)})
        with self.db.connect() as c:
            audit(c, 'pdf_retrieval_failed', id, {'attempts': attempts})
        raise ValueError('No open-access PDF could be retrieved (' + '; '.join(a['source'] + ': ' + a['error'] for a in attempts) + '). Download it through your library and upload the PDF.')

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

    # ---- EyeLink data finder: PDF -> repository links -> repository file listings ----

    def scan_eyelink(self, id, pdf_url=None, background=True):
        paper = self.require('papers', id)
        id = paper['merged_into'] or id
        if pdf_url is not None and not isinstance(pdf_url, str):
            raise ValueError('PDF URL must be text.')
        pdf_url = (pdf_url or '').strip() or None
        if pdf_url and urlsplit(pdf_url).scheme not in ('http', 'https'):
            raise ValueError('PDF URL must start with http:// or https://.')
        with self.scan_lock:
            if self.db.one("SELECT id FROM eyelink_scans WHERE paper_id=? AND status IN ('queued','running')", (id,)):
                raise Conflict('An EyeLink scan for this paper is already queued or running.')
            sid = uid()
            with self.db.connect() as c:
                c.execute('INSERT INTO eyelink_scans(id,paper_id,status,created) VALUES(?,?,?,?)', (sid, id, 'queued', now()))
                audit(c, 'eyelink_scan_queued', sid, {'paper_id': id, 'pdf_url': pdf_url})
        if background:
            self.submit(sid, lambda scan_id: self.run_eyelink_scan(scan_id, pdf_url))
        else:
            self.run_eyelink_scan(sid, pdf_url)
        return self.eyelink_scan(sid)

    def scan_eyelink_batch(self, scope='unscanned', limit=200):
        if scope not in SCAN_SCOPES or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Choose scope unscanned, all, or included, and a limit from 1 to 1,000.')
        where = {'unscanned': "NOT EXISTS(SELECT 1 FROM eyelink_scans s WHERE s.paper_id=p.id AND s.status IN ('queued','running','completed'))",
                 'all': "NOT EXISTS(SELECT 1 FROM eyelink_scans s WHERE s.paper_id=p.id AND s.status IN ('queued','running'))",
                 'included': "(SELECT d.decision FROM decisions d WHERE d.paper_id=p.id ORDER BY d.rowid DESC LIMIT 1)='include' AND NOT EXISTS(SELECT 1 FROM eyelink_scans s WHERE s.paper_id=p.id AND s.status IN ('queued','running'))"}[scope]
        ids = [r['id'] for r in self.db.rows(f'SELECT p.id FROM papers p WHERE p.merged_into IS NULL AND {where} ORDER BY p.created LIMIT ?', (limit,))]
        queued = 0
        for pid in ids:
            try:
                self.scan_eyelink(pid)
                queued += 1
            except Conflict:
                pass
        return {'queued': queued, 'scope': scope}

    def eyelink_scan(self, id):
        scan = self.require('eyelink_scans', id)
        scan['checks'] = self.db.rows('SELECT * FROM repository_checks WHERE scan_id=? ORDER BY rowid', (id,))
        for check in scan['checks']:
            check['files'] = self.db.rows('SELECT * FROM repository_files WHERE check_id=? ORDER BY rowid', (check['id'],))
        return scan

    def eyelink_scans(self):
        return self.db.rows('SELECT s.*,p.title,p.year,p.doi FROM eyelink_scans s JOIN papers p ON p.id=s.paper_id ORDER BY s.rowid DESC LIMIT 500')

    def run_eyelink_scan(self, sid, pdf_url=None):
        scan = self.require('eyelink_scans', sid)
        pid, steps = scan['paper_id'], []

        def step(name, status, detail=''):
            steps.append({'step': name, 'status': status, 'detail': str(detail)[:2000], 'at': now()})
            with self.db.connect() as c:
                c.execute('UPDATE eyelink_scans SET steps=? WHERE id=?', (dump(steps), sid))

        try:
            with self.db.connect() as c:
                c.execute("UPDATE eyelink_scans SET status='running' WHERE id=?", (sid,))
            paper = self.paper(pid)
            pdf = self.download_pdf(paper, pdf_url, step)
            if pdf:
                with self.db.connect() as c:
                    c.execute('UPDATE eyelink_scans SET pdf_url=?,pdf_path=?,pdf_sha256=? WHERE id=?', (pdf['url'], pdf['path'], pdf['sha256'], sid))
            pmcid = next((i['value'] for i in paper['identifiers'] if i['kind'] == 'pmcid'), None)
            if pmcid and not any('fullTextXML' in f['origin'] for f in paper['fulltexts']):
                try:
                    self.fetch_fulltext(pid)
                    step('pmc_fulltext', 'retrieved', pmcid)
                except ValueError as exc:
                    step('pmc_fulltext', 'failed', exc)
            paper = self.paper(pid)
            corpus = '\n'.join([plain(paper['abstract'])] + [f['content'] for f in paper['fulltexts']] +
                               [r['url'] + ' ' + r['evidence'] for r in paper['resources'] if r['url']])
            links = eyelink.find_repository_links(corpus)
            # Links described as data come first; reference-list software links are checked last, if at all.
            links.sort(key=lambda link: not DATA_CONTEXT.search(link.evidence))
            if not links:
                step('links', 'none', 'No supported data-repository links in the PDF, full text, abstract, or stored resources.')
            elif len(links) > MAX_REPOSITORIES:
                step('links', 'capped', f'{len(links)} repository links found; checking the first {MAX_REPOSITORIES}.')
            budget, token = eyelink.Budget(), os.environ.get('ORCHARD_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN')
            checks = []
            for link in links[:MAX_REPOSITORIES]:
                result = eyelink.check_repository(self.web, link, budget, token)
                checks.append(result)
                found = sum(eyelink.is_eyelink(f) for f in result['files'])
                self.store_check(sid, pid, link, result, found)
                step('repository', result['status'], f"{link.repository} {link.url}: {result['files_listed']} files listed, {found} .edf/.asc candidates")
            verdict = eyelink.summarize(checks)
            total = sum(eyelink.is_eyelink(f) for c in checks for f in c['files'])
            with self.db.connect() as c:
                c.execute("UPDATE eyelink_scans SET status='completed',result=?,repositories=?,eyelink_files=?,finished=? WHERE id=?", (verdict, len(checks), total, now(), sid))
                audit(c, 'eyelink_scan_completed', sid, {'paper_id': pid, 'result': verdict, 'repositories': len(checks), 'eyelink_files': total})
        except Exception as exc:
            with self.db.connect() as c:
                c.execute("UPDATE eyelink_scans SET status='failed',error=?,finished=? WHERE id=?", (f'{type(exc).__name__}: {exc}', now(), sid))
                audit(c, 'eyelink_scan_failed', sid, {'paper_id': pid, 'error': str(exc)})

    def store_check(self, sid, pid, link, result, found):
        with self.db.connect() as c:
            cid = uid()
            c.execute('INSERT INTO repository_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (cid, sid, pid, link.repository, link.identifier, link.url, link.evidence[:2000], result['status'], result['files_listed'], found, dump(result['notes']), now()))
            for f in result['files']:
                c.execute('INSERT INTO repository_files VALUES(?,?,?,?,?,?,?,?,?)', (uid(), cid, f['path'], f['size'], f['url'], f['kind'], f['header'], f.get('note'), now()))
            self.add_resource(c, pid, 'code' if link.repository in ('github', 'gitlab') else 'dataset', link.url, link.evidence, 'EyeLink scan; repository recognized')

    def pdf_candidates(self, paper, pdf_url, step):
        urls = [pdf_url] if pdf_url else []
        email = os.environ.get('ORCHARD_UNPAYWALL_EMAIL')
        if paper['doi'] and email:
            try:
                urls += eyelink.unpaywall_pdfs(self.web, paper['doi'], email)
            except Exception as exc:
                step('unpaywall', 'failed', f'{type(exc).__name__}: {exc}')
        for source in paper['sources']:
            links = json.loads(source['raw']).get('fullTextUrlList', {}).get('fullTextUrl', [])
            pdfs = [l for l in links if isinstance(l, dict) and l.get('documentStyle') == 'pdf' and isinstance(l.get('url'), str)] if isinstance(links, list) else []
            urls += [l['url'] for l in sorted(pdfs, key=lambda l: l.get('availabilityCode') not in ('OA', 'F'))]
        pmcid = next((i['value'] for i in paper['identifiers'] if i['kind'] == 'pmcid'), None)
        if pmcid and re.fullmatch(r'PMC\d+', pmcid):
            urls.append(f'https://europepmc.org/articles/{pmcid}?pdf=render')
        return list(dict.fromkeys(u for u in urls if urlsplit(u).scheme in ('http', 'https')))

    def download_pdf(self, paper, pdf_url, step):
        stored = next((f for f in paper['fulltexts'] if f['origin'].startswith('PDF: ')), None)
        if stored and not pdf_url:
            step('pdf', 'reused', 'PDF text already stored from ' + stored['origin'][5:])
            return None
        candidates = self.pdf_candidates(paper, pdf_url, step)
        if not candidates:
            step('pdf', 'unavailable', 'No PDF link is known for this paper. Supply a PDF URL to scan it.')
        for url in candidates[:6]:
            try:
                data, final, _ = self.web.get(url, limit=eyelink.PDF_LIMIT, headers={'Accept': 'application/pdf,*/*;q=0.5'})
                text, uris, engine = eyelink.pdf_text(data)
            except Exception as exc:
                step('pdf', 'failed', f'{url}: {type(exc).__name__}: {exc}')
                continue
            sha = hashlib.sha256(data).hexdigest()
            folder = self.db.path.parent / 'pdfs'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{paper['id']}-{sha[:12]}.pdf"
            path.write_bytes(data)
            content = text + ('\n\nLinks embedded in the PDF:\n' + '\n'.join(uris) if uris else '')
            if content.strip():
                self.import_fulltext(paper['id'], content[:4_000_000], 'PDF: ' + final)
            step('pdf', 'downloaded', f'{final} ({len(data):,} bytes; {engine} text extraction; {len(uris)} embedded links' + ('' if text.strip() else '; no extractable text') + ')')
            return {'url': final, 'path': str(path), 'sha256': sha}
        return None

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
                'screening_events':'SELECT COUNT(*) FROM decisions',
                'eyelink_papers':"SELECT COUNT(DISTINCT paper_id) FROM eyelink_scans WHERE result IN ('eyelink_confirmed','eyelink_candidates')"
            }.items(): counts[name]=c.execute(sql).fetchone()[0]
        return {'project':self.db.one('SELECT * FROM project'), 'counts':counts,
                'criteria':self.db.one('SELECT * FROM criteria ORDER BY rowid DESC LIMIT 1'),
                'runs':self.db.rows('SELECT * FROM runs ORDER BY rowid DESC LIMIT 8'),
                'provider':{'name':'Europe PMC','configured':True,'live_verified':False,'note':'No API key required. Live access depends on your network. Check each run status.'}}

    def export(self):
        tables=('project','documents','concepts','terms','queries','criteria','runs','papers','identifiers','source_records','discoveries','duplicates','relationships','decisions','resources','fulltexts','pdfs','assessments','eyelink_scans','repository_checks','repository_files','audit')
        with self.db.connect() as c:
            eid=uid(); audit(c,'export_created',eid,{'tables':tables})
            # One read transaction for a consistent snapshot, including this export event.
            data={t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
        data['manifest']={'schema_version':2,'application_version':'0.1.0','exported_at':now(),
                          'counts':{'papers': sum(p['merged_into'] is None for p in data['papers']), 'source_records':len(data['source_records']), 'discovery_events':len(data['discoveries']), 'screening_events':len(data['decisions'])},
                          'study_count':None,'prisma_note':'Records and reports are tracked. Studies are not mapped; study counts and a complete PRISMA diagram are not available.',
                          'pdf_note':'PDF provenance and SHA-256 hashes are exported. PDF files stay in the local data/pdfs folder and are not included.'}
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

    def manuscript_zip(self):
        """Supplementary tables for a manuscript; see orchard/manuscript.py."""
        exported_at=now()
        with self.db.connect() as c:
            audit(c,'manuscript_export_created',uid(),{'tables':[t[0] for t in manuscript.TABLES]})
            return manuscript.write_zip(c,exported_at)
