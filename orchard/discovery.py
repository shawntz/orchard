"""Repository documentation, publication discovery, local reader and review history."""
import base64
import hashlib
import json
import re
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin, urlsplit
from .db import uid, now, dump, audit
from .repository_sweep import DEFAULT_TERMS, classify
from .eyelink import pdf_text, header_kind

SCHEMA = '''
CREATE TABLE IF NOT EXISTS discovery_jobs(id TEXT PRIMARY KEY,status TEXT,parameters TEXT,created TEXT,finished TEXT,sweep_id TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS discovery_observations(id TEXT PRIMARY KEY,job_id TEXT,repository TEXT,external_id TEXT,title TEXT,has_files INTEGER,created TEXT);
CREATE TABLE IF NOT EXISTS discovery_documents(id TEXT PRIMARY KEY,project_id TEXT,kind TEXT,url TEXT,title TEXT,content TEXT,pdf_path TEXT,sha256 TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS discovery_candidates(id TEXT PRIMARY KEY,project_id TEXT,doi TEXT,title TEXT,score REAL,origin TEXT,raw TEXT,created TEXT,UNIQUE(project_id,doi));
CREATE TABLE IF NOT EXISTS discovery_events(id TEXT PRIMARY KEY,project_id TEXT,stage TEXT,decision TEXT,reason TEXT,evidence TEXT,terms TEXT,created TEXT);
'''

class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.links=[]; self.meta={}; self.hidden=0
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in ('script','style'): self.hidden+=1
        if tag=='a' and a.get('href'): self.links.append(a['href'])
        if tag=='meta': self.meta[a.get('name',a.get('property','')).lower()]=a.get('content','')
    def handle_endtag(self,tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
        if tag in ('p','div','br','h1','h2','li'): self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden: self.parts.append(data)

def setup(s):
    with s.db.connect() as c: c.executescript(SCHEMA)

def save_document(s, project, kind, url, text, title='', pdf=None):
    ident=uid(); path=None
    if pdf:
        folder=s.db.path.parent/'pdfs'; folder.mkdir(exist_ok=True)
        path=ident+'.pdf'; (folder/path).write_bytes(pdf)
    with s.db.connect() as c:
        c.execute('INSERT INTO discovery_documents VALUES(?,?,?,?,?,?,?,?,?)',(ident,project,kind,url,title,text[:4_000_000],path,hashlib.sha256(pdf or text.encode()).hexdigest(),now()))
        audit(c,'discovery_document_saved',project,{'document_id':ident,'kind':kind,'url':url})
    return ident

def get_json(s,url):
    return json.loads(s.web.get(url,limit=5_000_000)[0])

def fetch_document(s,project,url,kind='article'):
    data,final,headers=s.web.get(url,limit=30_000_000)
    if data.startswith(b'%PDF-'):
        text,_,engine=pdf_text(data)
        return save_document(s,project,'article_pdf',final,text,'PDF ('+engine+')',pdf=data)
    raw=data.decode('utf-8',errors='replace')
    parser=HTMLText(); parser.feed(raw)
    text=' '.join(parser.parts) if '<html' in raw.lower() or '<body' in raw.lower() else raw
    # Publishers often expose their PDF through a citation meta tag.
    pdf_url=parser.meta.get('citation_pdf_url')
    if kind=='article' and pdf_url:
        try: return fetch_document(s,project,urljoin(final,pdf_url),'article_pdf')
        except Exception: pass
    if kind.startswith('article') and len(text.strip())<500:
        raise ValueError('The server returned an empty webpage, access page or short text, not a readable article. No article was saved.')
    return save_document(s,project,kind,final,text,parser.meta.get('citation_title',''))

def documentation(s,p):
    """Fetch READMEs, OSF wiki text and data-file descriptions without downloading recordings."""
    repo=p['repository']; ext=p['external_id']; targets=[]
    if repo=='github':
        result=get_json(s,'https://api.github.com/repos/'+quote(ext,safe='/')+'/readme')
        if result.get('content'):
            text=base64.b64decode(result['content']).decode('utf-8',errors='replace')
            save_document(s,p['id'],'readme',result.get('html_url',p['url']),text)
    elif repo=='osf':
        result=get_json(s,'https://api.osf.io/v2/nodes/'+quote(ext,safe='')+'/wikis/')
        for wiki in result.get('data',[])[:10]:
            links=wiki.get('relationships',{}).get('content',{}).get('links',{}).get('related',{})
            url=links.get('href') if isinstance(links,dict) else links
            if url: targets.append((url,'readme'))
        root='https://api.osf.io/v2/nodes/'+quote(ext,safe='')+'/files/osfstorage/'
        queue=[root]; visited=set()
        # Bounded recursive README discovery, including folder pagination.
        while queue and len(visited)<30:
            url=queue.pop(0)
            if url in visited: continue
            visited.add(url); listing=get_json(s,url)
            if listing.get('links',{}).get('next'): queue.append(listing['links']['next'])
            for item in listing.get('data',[]):
                a=item.get('attributes',{}); name=a.get('name','')
                if a.get('kind')=='folder':
                    rel=item.get('relationships',{}).get('files',{}).get('links',{}).get('related',{})
                    sub=rel.get('href') if isinstance(rel,dict) else rel
                    if sub: queue.append(sub)
                elif re.search(r'(?i)readme|citation',name) and item.get('links',{}).get('download'):
                    targets.append((item['links']['download'],'readme'))
    else:
        targets.append((p['url'],'repository_page'))
    for url,kind in targets[:12]:
        try: fetch_document(s,p['id'],url,kind)
        except Exception as exc:
            with s.db.connect() as c: audit(c,'documentation_fetch_failed',p['id'],{'url':url,'error':str(exc)})

def evidence_event(s,project,text,terms,stage='repository'):
    result=classify(text,terms,stage)
    with s.db.connect() as c:
        c.execute('INSERT INTO discovery_events VALUES(?,?,?,?,?,?,?,?)',(uid(),project,stage,result['status'],result['reason'],dump(result['evidence']),dump(terms),now()))
    return result

def discover(s,project_id,terms=None):
    setup(s); terms=terms or list(DEFAULT_TERMS)
    p=s.db.one('SELECT * FROM sweep_projects WHERE id=?',(project_id,))
    if not p: raise KeyError('Project not found')
    files=s.db.rows('SELECT * FROM sweep_files WHERE project_id=?',(project_id,))
    probes=[]
    for file in files[:3]:
        url=file['url']
        if not url: continue
        if 'github.com/' in url and '/blob/' in url:
            url=url.replace('github.com/','raw.githubusercontent.com/').replace('/blob/','/')
        try:
            data,_,_=s.web.get(url,limit=8192,headers={'Range':'bytes=0-8191'},truncate=True)
            probes.append(file['path']+': '+header_kind(data,file['extension'].lstrip('.')))
        except Exception as exc: probes.append(file['path']+': unavailable ('+str(exc)[:120]+')')
    if probes:
        with s.db.connect() as c:
            c.execute('INSERT INTO discovery_events VALUES(?,?,?,?,?,?,?,?)',(uid(),project_id,'file_probe','review','Checked headers of up to three files; this does not validate all recordings.',dump(probes),'[]',now()))
    try: documentation(s,p)
    except Exception as exc:
        with s.db.connect() as c: audit(c,'documentation_unavailable',project_id,{'error':str(exc)})
    docs=s.db.rows('SELECT * FROM discovery_documents WHERE project_id=?',(project_id,))
    text='\n'.join([p['title'] or '',p['description'] or '']+[d['content'] for d in docs])
    evidence_event(s,project_id,text,terms)
    # Preserve DOI proposals, including ones found in README citation sections.
    dois=list(dict.fromkeys(re.findall(r'10\.\d{4,9}/[^\s<>"\]\)]+',text)))
    if p.get('doi'): dois.insert(0,p['doi'])
    for doi in dois[:8]:
        doi=doi.removeprefix('https://doi.org/').rstrip('.,;')
        try:
            raw=get_json(s,'https://api.crossref.org/works/'+quote(doi,safe=''))['message']
            add_candidate(s,p,raw,1.0,'DOI in repository documentation')
        except Exception: pass
    # Missing DOI is not a dead end: search by repository title; never silently accept a fuzzy match.
    title=re.sub(r'(?i)^(data(?:set)?(?: for| from)?|supplementary materials? for)\s*[:–-]?\s*','',p['title'] or '')
    try:
        result=get_json(s,'https://api.crossref.org/works?'+urlencode({'query.bibliographic':title,'rows':5}))
        for raw in result['message']['items']:
            candidate=' '.join(raw.get('title',[]))
            score=SequenceMatcher(None,re.sub(r'\W+',' ',title.lower()),re.sub(r'\W+',' ',candidate.lower())).ratio()
            if score>=0.72: add_candidate(s,p,raw,score,'Close title match — confirm the paper before retrieving')
    except Exception as exc:
        with s.db.connect() as c: audit(c,'publication_search_failed',project_id,{'error':str(exc)})
    # Retrieve PDFs explicitly linked from documentation; retain source and do not claim a confirmed paper association.
    for url in list(dict.fromkeys(re.findall(r'https?://[^\s<>"\)]+\.pdf(?:\?[^\s<>"\)]*)?',text)))[:2]:
        try: fetch_document(s,project_id,url)
        except Exception: pass
    return detail(s,project_id)

def add_candidate(s,p,raw,score,origin):
    if raw.get('type') not in ('journal-article','proceedings-article','posted-content','book-chapter'): return
    doi=raw.get('DOI')
    if not doi: return
    with s.db.connect() as c:
        c.execute('INSERT OR IGNORE INTO discovery_candidates VALUES(?,?,?,?,?,?,?,?)',(uid(),p['id'],doi,' '.join(raw.get('title',[])),score,origin,dump(raw),now()))

def retrieve(s,project_id,candidate_id=None,url=None,upload=None,terms=None):
    setup(s); terms=terms or list(DEFAULT_TERMS); document=None; failures=[]
    if not s.db.one('SELECT id FROM sweep_projects WHERE id=?',(project_id,)): raise KeyError('Project not found')
    if upload:
        data=base64.b64decode(upload,validate=True)
        if len(data)>8_000_000 or not data.startswith(b'%PDF-'): raise ValueError('Upload a PDF smaller than 8 MB.')
        text,_,engine=pdf_text(data)
        document=save_document(s,project_id,'article_pdf','Researcher upload',text,'Uploaded PDF ('+engine+')',data)
    elif url:
        document=fetch_document(s,project_id,url)
    else:
        candidate=s.db.one('SELECT * FROM discovery_candidates WHERE id=? AND project_id=?',(candidate_id,project_id))
        if not candidate: raise ValueError('Choose a proposed article or supply its URL/PDF.')
        if candidate['score']<0.72:
            raise ValueError('This earlier suggestion is too weak a match. Recheck the project; do not treat this paper as its publication.')
        raw=json.loads(candidate['raw']); doi=candidate['doi']
        with s.db.connect() as c: audit(c,'publication_association_confirmed',project_id,{'doi':doi,'candidate':candidate_id})
        # Independent fallbacks: PMC failure must not prevent PDF or HTML retrieval.
        try:
            page=s.provider.page('DOI:"'+doi.replace('"','')+'"',size=10)
            record=next((r for r in page.records if r.get('doi','').lower()==doi.lower()),{})
            if record.get('pmcid'):
                try:
                    text,origin=s.provider.fulltext(record['pmcid'])
                    document=save_document(s,project_id,'article_xml',origin,text,candidate['title'])
                except Exception as exc: failures.append(str(exc))
            for link in record.get('fullTextUrlList',{}).get('fullTextUrl',[]):
                if link.get('url'): raw.setdefault('link',[]).append({'URL':link['url']})
        except Exception as exc: failures.append(str(exc))
        for link in raw.get('link',[])[:5]:
            if document: break
            try: document=fetch_document(s,project_id,link['URL'])
            except Exception as exc: failures.append(str(exc))
        if not document:
            try: document=fetch_document(s,project_id,'https://doi.org/'+doi)
            except Exception as exc: failures.append(str(exc))
    if document:
        d=s.db.one('SELECT * FROM discovery_documents WHERE id=?',(document,))
        if len(d['content'].strip())>=500:
            evidence_event(s,project_id,d['content'],terms,'article')
        else:
            with s.db.connect() as c:
                c.execute('INSERT INTO discovery_events VALUES(?,?,?,?,?,?,?,?)',(uid(),project_id,'access','insufficient_text','Text too short to assess; inspect the PDF or upload a readable copy.','[]',dump(terms),now()))
    else:
        with s.db.connect() as c:
            c.execute('INSERT INTO discovery_events VALUES(?,?,?,?,?,?,?,?)',(uid(),project_id,'access','insufficient_text','Article unavailable: '+'; '.join(failures)[:1000],'[]',dump(terms),now()))
    return detail(s,project_id)

def detail(s,project_id):
    setup(s)
    p=s.db.one('SELECT * FROM sweep_projects WHERE id=?',(project_id,))
    if not p: raise KeyError('Project not found')
    return {'project':p,'documents':s.db.rows("SELECT id,CASE WHEN kind LIKE 'article%' AND pdf_path IS NULL AND length(trim(content))<500 THEN 'unavailable_article_page' ELSE kind END AS kind,url,title,pdf_path,created FROM discovery_documents WHERE project_id=? ORDER BY rowid DESC",(project_id,)),
            'candidates':s.db.rows('SELECT id,title,doi,score,origin FROM discovery_candidates WHERE project_id=? AND score>=0.72 ORDER BY score DESC',(project_id,)),
            'events':s.db.rows('SELECT * FROM discovery_events WHERE project_id=? ORDER BY rowid DESC',(project_id,)),
            'warnings':s.db.rows("SELECT event,detail,created FROM audit WHERE entity_id=? AND event IN ('documentation_unavailable','documentation_fetch_failed','publication_search_failed') ORDER BY id DESC LIMIT 20",(project_id,))}

def decision(s,project_id,decision,reason):
    if decision not in ('include','exclude','review') or not reason.strip(): raise ValueError('Choose a decision and explain the evidence.')
    detail(s,project_id)
    with s.db.connect() as c:
        c.execute('INSERT INTO discovery_events VALUES(?,?,?,?,?,?,?,?)',(uid(),project_id,'human',decision,reason,'[]','[]',now()))
        audit(c,'repository_human_decision',project_id,{'decision':decision,'reason':reason})
    return detail(s,project_id)

def start(s,parameters):
    setup(s); ident=uid()
    # Validate before queuing and store a copy of every submitted search parameter.
    if not isinstance(parameters.get('query'),str) or not parameters['query'].strip(): raise ValueError('Enter a search phrase.')
    with s.db.connect() as c: c.execute('INSERT INTO discovery_jobs VALUES(?,?,?,?,?,?,?)',(ident,'queued',dump(parameters),now(),None,None,None))
    def work():
        try:
            with s.db.connect() as c: c.execute("UPDATE discovery_jobs SET status='searching' WHERE id=?",(ident,))
            from .repository_sweep import RepositorySweep
            adapter=RepositorySweep(s.web)
            original=adapter.search
            def observed(repo,query,max_pages):
                for p in original(repo,query,max_pages):
                    with s.db.connect() as c:
                        c.execute('INSERT INTO discovery_observations VALUES(?,?,?,?,?,?,?)',(uid(),ident,repo,p['id'],p.get('title'),bool(p['files']),now()))
                    yield p
            adapter.search=observed
            result=s.repository_sweep(adapter=adapter,**parameters)
            with s.db.connect() as c: c.execute("UPDATE discovery_jobs SET status='reading_documentation',sweep_id=? WHERE id=?",(result['sweep']['id'],ident))
            for p in result['projects']:
                try: discover(s,p['id'],parameters.get('terms'))
                except Exception as exc:
                    with s.db.connect() as c: audit(c,'project_discovery_failed',p['id'],{'error':str(exc)})
            with s.db.connect() as c: c.execute('UPDATE discovery_jobs SET status=?,finished=? WHERE id=?',(result['sweep']['status'],now(),ident))
        except Exception as exc:
            with s.db.connect() as c: c.execute("UPDATE discovery_jobs SET status='failed',finished=?,error=? WHERE id=?",(now(),str(exc),ident))
    s.pool.submit(work)
    return {'id':ident}

def report(s):
    setup(s); jobs=s.db.rows('SELECT * FROM discovery_jobs ORDER BY created DESC LIMIT 30')
    for j in jobs:
        j['observed']=s.db.one('SELECT COUNT(*) n FROM discovery_observations WHERE job_id=?',(j['id'],))['n']
        j['results']=s.repository_results(j['sweep_id']) if j['sweep_id'] else None
        if j['results']:
            for p in j['results']['projects']:
                p['workflow']=detail(s,p['id'])
            projects=j['results']['projects']
            j['stages']={'projects_with_documentation':sum(any(d['kind'] in ('readme','repository_page') for d in p['workflow']['documents']) for p in projects),
                         'projects_with_proposed_papers':sum(bool(p['workflow']['candidates']) for p in projects),
                         'projects_with_saved_article':sum(any(d['kind'].startswith('article') for d in p['workflow']['documents']) for p in projects)}
    return jobs
