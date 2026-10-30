"""Repository-first discovery: search jobs, project inspection, publications, screening and reporting.

Records produced by the earlier prototype (discovery_*, sweep_* tables) are kept and migrated
additively; nothing is deleted. A copy of the database is written before the first migration.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import uid, now, dump, audit
from . import articles, downloads as dl, eyelink, inventory as inv, publications as pubs, repositories as repos, screening

SCHEMA_VERSION = 4
LEGACY = '''
CREATE TABLE IF NOT EXISTS discovery_jobs(id TEXT PRIMARY KEY,status TEXT,parameters TEXT,created TEXT,finished TEXT,sweep_id TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS discovery_observations(id TEXT PRIMARY KEY,job_id TEXT,repository TEXT,external_id TEXT,title TEXT,has_files INTEGER,created TEXT);
CREATE TABLE IF NOT EXISTS discovery_documents(id TEXT PRIMARY KEY,project_id TEXT,kind TEXT,url TEXT,title TEXT,content TEXT,pdf_path TEXT,sha256 TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS discovery_candidates(id TEXT PRIMARY KEY,project_id TEXT,doi TEXT,title TEXT,score REAL,origin TEXT,raw TEXT,created TEXT,UNIQUE(project_id,doi));
CREATE TABLE IF NOT EXISTS discovery_events(id TEXT PRIMARY KEY,project_id TEXT,stage TEXT,decision TEXT,reason TEXT,evidence TEXT,terms TEXT,created TEXT);
'''
TABLES = '''
CREATE TABLE IF NOT EXISTS discovery_tasks(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, position INTEGER, source TEXT NOT NULL, kind TEXT NOT NULL, query TEXT NOT NULL, label TEXT, coverage TEXT, parameters TEXT, status TEXT NOT NULL, cursor TEXT, pages INTEGER NOT NULL DEFAULT 0, records INTEGER NOT NULL DEFAULT 0, reported_total INTEGER, projects INTEGER NOT NULL DEFAULT 0, retry_after TEXT, error TEXT, requests TEXT NOT NULL DEFAULT '[]', started TEXT, finished TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS discovery_tasks_job ON discovery_tasks(job_id);
CREATE INDEX IF NOT EXISTS discovery_observations_job ON discovery_observations(job_id);
CREATE TABLE IF NOT EXISTS project_inventories(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT, status TEXT NOT NULL, files_listed INTEGER, complete INTEGER, notes TEXT, requests INTEGER, created TEXT NOT NULL, finished TEXT);
CREATE TABLE IF NOT EXISTS publication_searches(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created TEXT NOT NULL, searches TEXT, rejected TEXT, non_publications TEXT, candidates INTEGER);
CREATE TABLE IF NOT EXISTS publication_decisions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, candidate_id TEXT, paper_id TEXT, action TEXT NOT NULL, reason TEXT, reviewer TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_papers(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers(id), candidate_id TEXT, relation TEXT, status TEXT NOT NULL DEFAULT 'confirmed', created TEXT NOT NULL, UNIQUE(project_id,paper_id));
CREATE TABLE IF NOT EXISTS retrieval_attempts(id TEXT PRIMARY KEY, paper_id TEXT, project_id TEXT, source TEXT, url TEXT, final_url TEXT, outcome TEXT NOT NULL, reason TEXT, http_status INTEGER, content_type TEXT, bytes INTEGER, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS screening_configs(id TEXT PRIMARY KEY, concepts TEXT NOT NULL, sha256 TEXT NOT NULL, origin TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relevance_assessments(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, config_id TEXT, status TEXT NOT NULL, reason TEXT, notes TEXT, counts TEXT, evidence TEXT, sources TEXT, reproducibility TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_decisions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version INTEGER NOT NULL, decision TEXT NOT NULL, exclusion_reason TEXT, reason TEXT NOT NULL, evidence TEXT, reviewer TEXT NOT NULL, criteria_id TEXT, criteria TEXT, config_id TEXT, assessment_id TEXT, migrated_from TEXT UNIQUE, created TEXT NOT NULL, UNIQUE(project_id,version));
CREATE TRIGGER IF NOT EXISTS immutable_project_decisions BEFORE UPDATE ON project_decisions BEGIN SELECT RAISE(ABORT,'Project decisions are versioned; save a new version'); END;
CREATE TRIGGER IF NOT EXISTS undeletable_project_decisions BEFORE DELETE ON project_decisions BEGIN SELECT RAISE(ABORT,'Project decisions are versioned; save a new version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_decisions BEFORE UPDATE ON publication_decisions BEGIN SELECT RAISE(ABORT,'Publication decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS undeletable_publication_decisions BEFORE DELETE ON publication_decisions BEGIN SELECT RAISE(ABORT,'Publication decisions are append-only'); END;
CREATE TABLE IF NOT EXISTS download_batches(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dest TEXT NOT NULL, files INTEGER, total_bytes INTEGER, status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, manifest TEXT, manifest_path TEXT, reviewer TEXT, created TEXT NOT NULL, finished TEXT);
CREATE TABLE IF NOT EXISTS downloads(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, project_id TEXT NOT NULL, file_id TEXT, path TEXT, url TEXT, dest TEXT, expected_size INTEGER, expected_checksum TEXT, bytes_done INTEGER NOT NULL DEFAULT 0, sha256 TEXT, status TEXT NOT NULL, detail TEXT, created TEXT NOT NULL, finished TEXT);
CREATE TABLE IF NOT EXISTS studies(id TEXT PRIMARY KEY, label TEXT NOT NULL, notes TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS study_links(id TEXT PRIMARY KEY, study_id TEXT NOT NULL, kind TEXT NOT NULL, ref_id TEXT NOT NULL, reviewer TEXT, note TEXT, created TEXT NOT NULL, removed TEXT);
CREATE TABLE IF NOT EXISTS project_relations(id TEXT PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL, basis TEXT, status TEXT NOT NULL, reviewer TEXT, reason TEXT, created TEXT NOT NULL, decided TEXT, UNIQUE(a,b));
'''
COLUMNS = {
    'discovery_jobs': ['updated TEXT', 'cancel_requested INTEGER NOT NULL DEFAULT 0', 'progress TEXT', 'version INTEGER NOT NULL DEFAULT 1'],
    'discovery_observations': ['task_id TEXT', 'project_id TEXT', 'matched_files TEXT', 'evidence TEXT'],
    'sweep_projects': ['kind TEXT', 'repo_created TEXT', 'license TEXT', 'creators TEXT', 'keywords TEXT', 'related TEXT', 'metadata TEXT',
                       'first_seen TEXT', 'last_inspected TEXT', 'inspection_status TEXT', 'inspection_steps TEXT', 'match_status TEXT',
                       'eyelink_files INTEGER', 'confirmed_files INTEGER', 'inventory_id TEXT'],
    'sweep_files': ['role TEXT', 'role_reason TEXT', 'header TEXT', 'header_details TEXT', 'exclusion_group TEXT', 'participant TEXT',
                    'download TEXT', 'checksum TEXT', 'probe_url TEXT', 'inventory_id TEXT', 'in_archive TEXT', 'listed TEXT'],
    'discovery_documents': ['status TEXT', 'method TEXT', 'words INTEGER', 'quality_reason TEXT'],
    'discovery_candidates': ['url TEXT', 'authors TEXT', 'year TEXT', 'venue TEXT', 'kind TEXT', 'strength TEXT', 'evidence TEXT', 'search_id TEXT'],
}
ACTIVE = ('queued', 'running', 'inspecting')
MATCHING = ('eyelink_confirmed', 'candidate_files', 'no_study_recordings')
RESUMABLE = ('partial', 'failed', 'rate_limited', 'cancelled', 'interrupted', 'pending')
DECISIONS = ('include', 'exclude', 'needs_review', 'awaiting_information')
EXCLUSION_REASONS = ('No pupil measurement', 'Not a psychological or cognitive task', 'Non-human participants',
                     'Not EyeLink data (other EDF/ASC format)', 'Only example, test or placeholder files',
                     'Duplicate of another project', 'Data not reusable (licence or access restrictions)', 'Other (explain in reason)')
UNKNOWN_REVIEWER = 'Not recorded (saved before reviewer tracking)'


# --------------------------------------------------------------------------- setup and migration

def columns(c, table):
    return {r[1] for r in c.execute(f'PRAGMA table_info({table})')}


def setup(s):
    """Create and migrate tables. Idempotent; called on every start."""
    if getattr(s, '_discovery_ready', False):
        return
    db_path = Path(s.db.path)
    with s.db.connect() as c:
        c.executescript(LEGACY)
        version = c.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] or 0
        has_records = any(c.execute(f'SELECT 1 FROM {t} LIMIT 1').fetchone() for t in ('discovery_jobs', 'discovery_documents', 'discovery_events', 'sweep_projects'))
    if version < SCHEMA_VERSION and has_records:
        backup(db_path)
    with s.db.connect() as c:
        for table, cols in COLUMNS.items():
            existing = columns(c, table)
            for col in cols:
                if col.split()[0] not in existing:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {col}')
        c.executescript(TABLES)
        migrate_legacy(c)
        c.execute('INSERT OR IGNORE INTO schema_version VALUES(?)', (SCHEMA_VERSION,))
        c.execute("UPDATE discovery_jobs SET status='interrupted',error='Orchard stopped while this job ran. Resume continues from the saved page cursors.',updated=? WHERE status IN ('queued','running','inspecting','searching','reading_documentation')", (now(),))
        c.execute("UPDATE discovery_tasks SET status='interrupted',updated=? WHERE status IN ('running','queued')", (now(),))
        c.execute("UPDATE download_batches SET status='interrupted' WHERE status IN ('queued','running')")
        c.execute("UPDATE downloads SET status='interrupted' WHERE status IN ('queued','running')")
    if not hasattr(s, 'discovery_pool'):
        s.discovery_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='orchard-discovery')
        s.discovery_lock = threading.Lock()
    s._discovery_ready = True


def backup(path):
    """Copy the database (consistent snapshot) before the first schema migration."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = path.with_name(f'{path.stem}.before-v{SCHEMA_VERSION}-{stamp}{path.suffix}')
    source = sqlite3.connect(path)
    try:
        dest = sqlite3.connect(target)
        with dest:
            source.backup(dest)
        dest.close()
    finally:
        source.close()
    return target


def migrate_legacy(c):
    """Bring earlier prototype records into the versioned model without changing or deleting them."""
    # Human decisions saved by the prototype become version history, marked as lacking reviewer and criteria.
    for row in c.execute("SELECT * FROM discovery_events WHERE stage='human' ORDER BY rowid").fetchall():
        if c.execute('SELECT 1 FROM project_decisions WHERE migrated_from=?', (row['id'],)).fetchone():
            continue
        version = (c.execute('SELECT MAX(version) FROM project_decisions WHERE project_id=?', (row['project_id'],)).fetchone()[0] or 0) + 1
        decision = {'review': 'needs_review'}.get(row['decision'], row['decision'])
        c.execute('INSERT INTO project_decisions(id,project_id,version,decision,exclusion_reason,reason,evidence,reviewer,criteria_id,criteria,config_id,assessment_id,migrated_from,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (uid(), row['project_id'], version, decision, 'Other (explain in reason)' if decision == 'exclude' else None, row['reason'] or '',
                   row['evidence'], UNKNOWN_REVIEWER, None, dump({'note': 'Saved by the earlier prototype; criteria were not recorded.', 'terms': json.loads(row['terms'] or '[]')}),
                   None, None, row['id'], row['created']))
    # Saved "articles" are checked again: an OSF page that only says "OSF" is not article text.
    for row in c.execute('SELECT id,kind,content,pdf_path FROM discovery_documents WHERE status IS NULL').fetchall():
        text = row['content'] or ''
        words = len(re.findall(r'\w+', text))
        if row['kind'] and row['kind'].startswith('article') and not row['pdf_path'] and words < 500:
            status, reason = 'not_article_text', f'Only {words} word(s) ("{text.strip()[:40]}"); this was a web page, login or app shell, not the article.'
        elif row['kind'] == 'repository_page' and words < 100:
            status, reason = 'not_article_text', f'Repository web page with {words} word(s) of text (JavaScript shell).'
        elif words == 0:
            status, reason = 'empty', 'No text.'
        else:
            status, reason = 'readable', None
        c.execute('UPDATE discovery_documents SET status=?,words=?,quality_reason=?,method=COALESCE(method,?) WHERE id=?',
                  (status, words, reason, 'saved by earlier prototype', row['id']))
    c.execute("UPDATE discovery_candidates SET strength='legacy' WHERE strength IS NULL")
    c.execute("UPDATE discovery_jobs SET version=1 WHERE version IS NULL")


def client(s):
    if not hasattr(s, 'repo_client'):
        s.repo_client = repos.Client(s.web)
    return s.repo_client


def adapters(s):
    if not hasattr(s, 'repo_adapters'):
        s.repo_adapters = repos.adapters(client(s))
    return s.repo_adapters


def lookup(s):
    if not hasattr(s, 'publication_lookup'):
        s.publication_lookup = pubs.Lookup(client(s), repos.env('ORCHARD_CONTACT_EMAIL'))
    return s.publication_lookup


def submit(s, fn, *args):
    setup(s)
    return s.discovery_pool.submit(fn, *args)


# --------------------------------------------------------------------------- search jobs

def start(s, b, background=True):
    setup(s)
    sources = b['sources'] if b.get('sources') is not None else list(repos.SOURCES)
    phrases = b['phrases'] if b.get('phrases') is not None else list(repos.DEFAULT_PHRASES)
    extensions = b['extensions'] if b.get('extensions') is not None else ['asc', 'edf']
    if isinstance(phrases, str):
        phrases = [p.strip() for p in phrases.splitlines() if p.strip()]
    if not isinstance(sources, list) or not sources or any(x not in repos.SOURCES for x in sources) or len(set(sources)) != len(sources):
        raise ValueError('Choose one or more of: ' + ', '.join(repos.SOURCES) + '.')
    if not isinstance(phrases, list) or not 1 <= len(phrases) <= 20 or any(not isinstance(p, str) or not p.strip() or len(p) > 80 for p in phrases):
        raise ValueError('Provide 1–20 search phrases of at most 80 characters.')
    if not isinstance(extensions, list) or not extensions or any(e not in ('asc', 'edf') for e in extensions):
        raise ValueError('Extensions must be asc and/or edf.')
    max_pages, max_projects = int(b.get('max_pages', 5)), int(b.get('max_projects', 100))
    if not 1 <= max_pages <= 100 or not 0 <= max_projects <= 2000:
        raise ValueError('Pages per search: 1–100. Projects to inspect per run: 0–2,000.')
    params = {'sources': sources, 'phrases': [p.strip() for p in phrases], 'extensions': extensions, 'max_pages': max_pages,
              'max_projects': max_projects, 'refresh_inspected': bool(b.get('refresh_inspected')), 'started_by': (b.get('reviewer') or '').strip() or None}
    job = uid()
    plans = []
    for name in sources:
        plans += adapters(s)[name].plan(params['phrases'], tuple(extensions))
    with s.db.connect() as c:
        c.execute('INSERT INTO discovery_jobs(id,status,parameters,created,version,updated,progress) VALUES(?,?,?,?,?,?,?)',
                  (job, 'queued', dump(params), now(), 2, now(), dump({'phase': 'queued'})))
        for i, st in enumerate(plans):
            c.execute('INSERT INTO discovery_tasks(id,job_id,position,source,kind,query,label,coverage,parameters,status,error,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (uid(), job, i, st.source, st.kind, st.query, st.label, st.coverage, dump(st.parameters), 'skipped' if st.skipped else 'pending', st.skipped, now()))
        audit(c, 'discovery_job_started', job, {'parameters': params, 'tasks': [st.label for st in plans]})
    if background:
        submit(s, run_job, s, job)
    else:
        run_job(s, job)
    return job_detail(s, job)


def cancelled(s, job):
    return bool(s.db.one('SELECT cancel_requested FROM discovery_jobs WHERE id=?', (job,))['cancel_requested'])


def cancel(s, job):
    setup(s)
    row = s.db.one('SELECT * FROM discovery_jobs WHERE id=?', (job,))
    if not row:
        raise KeyError('Job not found.')
    with s.db.connect() as c:
        c.execute('UPDATE discovery_jobs SET cancel_requested=1,updated=? WHERE id=?', (now(), job))
        if row['status'] not in ACTIVE:
            c.execute("UPDATE discovery_jobs SET status='cancelled' WHERE id=? AND status IN ('pending','partial','rate_limited','interrupted')", (job,))
        audit(c, 'discovery_job_cancel_requested', job, {})
    return job_detail(s, job)


def resume(s, job, background=True):
    setup(s)
    row = s.db.one('SELECT * FROM discovery_jobs WHERE id=?', (job,))
    if not row or row['version'] != 2:
        raise KeyError('Only searches created by this version can be resumed.')
    if row['status'] in ACTIVE:
        raise ValueError('This search is already running.')
    with s.db.connect() as c:
        c.execute("UPDATE discovery_tasks SET status='pending',error=CASE WHEN status='skipped' THEN error ELSE NULL END,updated=? WHERE job_id=? AND status IN ('partial','failed','rate_limited','cancelled','interrupted')", (now(), job))
        c.execute("UPDATE discovery_jobs SET status='queued',cancel_requested=0,error=NULL,finished=NULL,updated=? WHERE id=?", (now(), job))
        audit(c, 'discovery_job_resumed', job, {})
    if background:
        submit(s, run_job, s, job)
    else:
        run_job(s, job)
    return job_detail(s, job)


def set_job(s, job, **fields):
    fields['updated'] = now()
    with s.db.connect() as c:
        c.execute('UPDATE discovery_jobs SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', [*fields.values(), job])


def set_task(s, task, **fields):
    fields['updated'] = now()
    with s.db.connect() as c:
        c.execute('UPDATE discovery_tasks SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', [*fields.values(), task])


def run_job(s, job):
    try:
        params = json.loads(s.db.one('SELECT parameters FROM discovery_jobs WHERE id=?', (job,))['parameters'])
        set_job(s, job, status='running', progress=dump({'phase': 'searching'}))
        for task in s.db.rows("SELECT * FROM discovery_tasks WHERE job_id=? AND status IN ('pending','queued') ORDER BY position", (job,)):
            if cancelled(s, job):
                break
            run_task(s, job, task, params)
        if cancelled(s, job):
            with s.db.connect() as c:
                c.execute("UPDATE discovery_tasks SET status='cancelled',updated=? WHERE job_id=? AND status IN ('pending','queued')", (now(), job))
        if not cancelled(s, job) and params.get('max_projects', 0):
            inspect_job(s, job, params)
        finish_job(s, job)
    except Exception as exc:
        set_job(s, job, status='failed', error=f'{type(exc).__name__}: {exc}'[:1000], finished=now())
        with s.db.connect() as c:
            audit(c, 'discovery_job_failed', job, {'error': str(exc)})


def finish_job(s, job):
    tasks = s.db.rows('SELECT status FROM discovery_tasks WHERE job_id=?', (job,))
    states = {t['status'] for t in tasks}
    params = json.loads(s.db.one('SELECT parameters FROM discovery_jobs WHERE id=?', (job,))['parameters'])
    # A search-only run (no inspection requested) is complete once its searches are.
    pending = len(queue(s, job, refresh=False)) if params.get('max_projects') else 0
    if cancelled(s, job):
        status = 'cancelled'
    elif states & {'failed', 'rate_limited', 'partial', 'interrupted'} or pending:
        status = 'partial'
    else:
        status = 'completed'
    note = []
    if pending:
        note.append(f'{pending} project(s) still to inspect.')
    set_job(s, job, status=status, finished=now(), error=' '.join(note) or None, progress=dump({'phase': status}))
    with s.db.connect() as c:
        audit(c, 'discovery_job_' + status, job, {'tasks': sorted(states), 'uninspected': pending})


def run_task(s, job, task, params):
    adapter = adapters(s)[task['source']]
    strategy = repos.Strategy(task['source'], task['kind'], task['query'], task['label'], task['coverage'], json.loads(task['parameters'] or '{}'))
    cursor = json.loads(task['cursor']) if task['cursor'] else None
    if task['pages'] and cursor is None:
        set_task(s, task['id'], status='completed', finished=now())
        return
    set_task(s, task['id'], status='running', started=task['started'] or now())
    pages_this_run, waits = 0, 0
    while True:
        if cancelled(s, job):
            set_task(s, task['id'], status='cancelled')
            return
        try:
            page = adapter.search(strategy, cursor)
        except repos.RateLimited as exc:
            if exc.retry_after <= 90 and waits < 3:
                waits += 1
                set_task(s, task['id'], retry_after=(datetime.now(timezone.utc) + timedelta(seconds=exc.retry_after)).isoformat())
                getattr(s, 'sleeper', time.sleep)(exc.retry_after)
                continue
            set_task(s, task['id'], status='rate_limited', error=str(exc), retry_after=(datetime.now(timezone.utc) + timedelta(seconds=exc.retry_after)).isoformat())
            return
        except Exception as exc:
            set_task(s, task['id'], status='failed', error=f'{type(exc).__name__}: {exc}'[:1000])
            with s.db.connect() as c:
                audit(c, 'discovery_task_failed', task['id'], {'job_id': job, 'cursor': cursor, 'error': str(exc)})
            return
        record_page(s, job, task, page, cursor)
        pages_this_run += 1
        cursor = page.cursor
        if cursor is None:
            set_task(s, task['id'], status='completed', finished=now())
            return
        if pages_this_run >= params['max_pages']:
            set_task(s, task['id'], status='partial', error=f'Page limit for this run ({params["max_pages"]}) reached; resume to continue from the saved cursor.')
            return


def record_page(s, job, task, page, cursor):
    with s.db.connect() as c:
        new_projects = 0
        for hit in page.hits:
            row = c.execute('SELECT id FROM sweep_projects WHERE repository=? AND external_id=?', (task['source'], hit['external_id'])).fetchone()
            if row:
                pid = row['id']
            else:
                pid = uid()
                new_projects += 1
                c.execute('INSERT INTO sweep_projects(id,repository,external_id,url,title,first_seen,match_status) VALUES(?,?,?,?,?,?,?)',
                          (pid, task['source'], hit['external_id'], hit['url'], hit.get('title'), now(), 'not_inspected'))
            c.execute('INSERT INTO discovery_observations(id,job_id,repository,external_id,title,has_files,created,task_id,project_id,matched_files,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (uid(), job, task['source'], hit['external_id'], hit.get('title'), int(bool(hit.get('matched_files'))), now(), task['id'], pid,
                       dump(hit.get('matched_files') or []), dump(hit.get('evidence') or {})))
        requests = json.loads(c.execute('SELECT requests FROM discovery_tasks WHERE id=?', (task['id'],)).fetchone()['requests'] or '[]')
        requests.append({'url': page.request, 'cursor': cursor, 'hits': len(page.hits), 'reported_total': page.total, 'at': now()})
        projects = c.execute('SELECT COUNT(DISTINCT project_id) FROM discovery_observations WHERE task_id=?', (task['id'],)).fetchone()[0]
        c.execute('UPDATE discovery_tasks SET pages=pages+1,records=records+?,reported_total=?,projects=?,cursor=?,requests=?,updated=? WHERE id=?',
                  (len(page.hits), page.total, projects, dump(page.cursor) if page.cursor is not None else None, dump(requests[-200:]), now(), task['id']))


def queue(s, job, refresh=False):
    """Projects observed by this job that still need inspecting."""
    rows = s.db.rows('SELECT DISTINCT p.* FROM discovery_observations o JOIN sweep_projects p ON p.id=o.project_id WHERE o.job_id=? ORDER BY p.first_seen', (job,))
    created = s.db.one('SELECT created FROM discovery_jobs WHERE id=?', (job,))['created']
    if refresh:
        return [r for r in rows if not r['last_inspected'] or r['last_inspected'] < created]
    return [r for r in rows if not r['last_inspected'] or r['inspection_status'] in ('failed', 'rate_limited')]


def inspect_job(s, job, params):
    todo = queue(s, job, params.get('refresh_inspected'))
    # Projects with file-name evidence first; metadata-only matches after them.
    hinted = {r['project_id'] for r in s.db.rows('SELECT DISTINCT project_id FROM discovery_observations WHERE job_id=? AND has_files=1', (job,))}
    todo.sort(key=lambda p: p['id'] not in hinted)
    total = min(len(todo), params['max_projects'])
    set_job(s, job, status='inspecting', progress=dump({'phase': 'inspecting', 'done': 0, 'total': total, 'queued': len(todo)}))
    for i, project in enumerate(todo[:params['max_projects']]):
        if cancelled(s, job):
            return
        try:
            inspect(s, project['id'], job)
        except repos.RateLimited as exc:
            set_job(s, job, error=str(exc))
            return
        except Exception:
            pass  # recorded on the project; continue with the others
        set_job(s, job, progress=dump({'phase': 'inspecting', 'done': i + 1, 'total': total, 'queued': len(todo), 'current': project.get('title')}))


# --------------------------------------------------------------------------- project inspection

def inspect(s, project_id, job=None, refresh_publications=True):
    """List files, read headers and documentation, look for publications and assess relevance."""
    setup(s)
    p = project_row(s, project_id)
    adapter = adapters(s)[p['repository']]
    steps = []

    def step(name, status, detail=''):
        steps.append({'step': name, 'status': status, 'detail': str(detail)[:1500], 'at': now()})
        with s.db.connect() as c:
            c.execute('UPDATE sweep_projects SET inspection_steps=? WHERE id=?', (dump(steps), project_id))

    with s.db.connect() as c:
        c.execute("UPDATE sweep_projects SET inspection_status='running' WHERE id=?", (project_id,))
    try:
        meta = adapter.metadata(p['external_id'])
        with s.db.connect() as c:
            c.execute('UPDATE sweep_projects SET title=?,description=?,doi=?,url=?,kind=?,repo_created=?,license=?,creators=?,keywords=?,related=?,metadata=? WHERE id=?',
                      (meta.get('title') or p['title'], meta.get('description'), meta.get('doi'), meta.get('url') or p['url'], meta.get('kind'), meta.get('created'),
                       meta.get('license'), dump(meta.get('creators') or []), dump(meta.get('keywords') or []), dump(meta.get('related') or []),
                       dump({k: v for k, v in meta.items() if k not in ('raw_files',)}), project_id))
        step('metadata', 'ok', f"{meta.get('kind')}; licence: {meta.get('license') or 'not stated'}; contributors: {len(meta.get('creators') or [])}")
    except repos.RateLimited:
        with s.db.connect() as c:
            c.execute("UPDATE sweep_projects SET inspection_status='rate_limited' WHERE id=?", (project_id,))
        raise
    except Exception as exc:
        step('metadata', 'failed', exc)
        with s.db.connect() as c:
            c.execute("UPDATE sweep_projects SET inspection_status='failed',last_inspected=?,match_status=COALESCE(NULLIF(match_status,'not_inspected'),'inspection_failed') WHERE id=?", (now(), project_id))
        raise
    budget = eyelink.Budget(requests=600, seconds=600, archives=10, probes=10, per_repository=(500, 480))
    budget.start_repository()
    inventory_id = uid()
    try:
        listing = adapter.inventory(p['external_id'], budget, meta=meta)
        step('inventory', 'complete' if listing.complete else 'partial', f'{len(listing.files)} files listed. ' + ' '.join(listing.notes))
    except repos.RateLimited:
        with s.db.connect() as c:
            c.execute("UPDATE sweep_projects SET inspection_status='rate_limited' WHERE id=?", (project_id,))
        raise
    except Exception as exc:
        listing = repos.Inventory([], False, [f'{type(exc).__name__}: {exc}'])
        step('inventory', 'failed', exc)
    archives_listed = list_archives(s, listing, budget, step)
    store_inventory(s, project_id, job, inventory_id, listing, archives_listed)
    probe_files(s, project_id, inventory_id, budget, step)
    classify_files(s, project_id, inventory_id)
    try:
        docs = adapter.documents(p['external_id'], meta, listing)
    except repos.RateLimited:
        raise
    except Exception as exc:
        docs = []
        step('documentation', 'failed', exc)
    store_documents(s, project_id, docs, step)
    if refresh_publications:
        find_publications(s, project_id, meta, step)
    assess(s, project_id)
    suggest_relations(s, project_id)
    status = update_match(s, project_id, inventory_id, listing)
    with s.db.connect() as c:
        c.execute("UPDATE sweep_projects SET inspection_status=?,last_inspected=?,inventory_id=? WHERE id=?",
                  ('partial' if not listing.complete else 'completed', now(), inventory_id, project_id))
        audit(c, 'repository_project_inspected', project_id, {'job_id': job, 'match_status': status, 'files': len(listing.files), 'requests': budget.per_repository[0] - budget.repo_requests})
    return project_detail(s, project_id)


def list_archives(s, listing, budget, step):
    """Read ZIP central directories with byte-range requests; nothing is downloaded."""
    members, read = [], 0
    for f in list(listing.files):
        if not re.search(r'\.zip$', f['path'] or '', re.I) or not f.get('probe_url'):
            continue
        if read >= budget.archives:
            step('archives', 'capped', f'Archive limit reached; {f["path"]} not opened.')
            continue
        read += 1
        try:
            entries, _ = eyelink.zip_members(client(s), budget, f['probe_url'], f.get('size'))
        except Exception as exc:
            step('archives', 'failed', f'{f["path"]}: {type(exc).__name__}: {exc}')
            continue
        for e in entries:
            members.append({**repos.file_row(f['path'] + '!/' + e['name'], e['size'], f.get('url'), None, None, 'inside_archive'),
                            'in_archive': f['path'], 'zip_member': e, 'archive_probe_url': f['probe_url']})
        step('archives', 'listed', f'{f["path"]}: {len(entries)} members.')
    return members


def store_inventory(s, project_id, job, inventory_id, listing, members):
    with s.db.connect() as c:
        c.execute('INSERT INTO project_inventories VALUES(?,?,?,?,?,?,?,?,?,?)', (inventory_id, project_id, job, 'complete' if listing.complete else 'partial',
                  len(listing.files), int(listing.complete), dump(listing.notes), None, now(), now()))
        for f in listing.files + members:
            ext = (inv.EYE_EXT.search(f['path'] or '') or re.search(r'(\.[^./]+)$', f['path'] or '') or [None, ''])
            extension = ext.group(0).lower() if hasattr(ext, 'group') else ''
            row = c.execute('SELECT id FROM sweep_files WHERE project_id=? AND path=?', (project_id, f['path'])).fetchone()
            values = (f.get('url'), f.get('size'), extension, f.get('download'), f.get('checksum'), f.get('probe_url'), inventory_id, f.get('in_archive'), now())
            if row:
                c.execute('UPDATE sweep_files SET url=?,size=?,extension=?,download=?,checksum=?,probe_url=?,inventory_id=?,in_archive=?,listed=? WHERE id=?', (*values, row['id']))
            else:
                c.execute('INSERT INTO sweep_files(id,project_id,path,url,size,extension,download,checksum,probe_url,inventory_id,in_archive,listed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (uid(), project_id, f['path'], *values))
            if f.get('zip_member'):
                c.execute('UPDATE sweep_files SET header_details=? WHERE project_id=? AND path=?', (dump({'zip_member': f['zip_member'], 'archive_url': f['archive_probe_url']}), project_id, f['path']))


def probe_files(s, project_id, inventory_id, budget, step):
    rows = s.db.rows('SELECT * FROM sweep_files WHERE project_id=? AND inventory_id=?', (project_id, inventory_id))
    plan = inv.probe_plan(rows)
    members = [r for r in rows if r['in_archive'] and inv.kind_of(r['path'])][:2]
    probed = 0
    for f in plan:
        try:
            header, details = inv.probe(client(s), f['probe_url'], inv.kind_of(f['path']), budget)
        except repos.RateLimited:
            raise
        except Exception as exc:
            header, details = 'probe_failed', {'note': f'{type(exc).__name__}: {exc}'[:300]}
        probed += 1
        update = {'header': header, 'header_details': dump(details)}
        if header == 'git_lfs_pointer':
            media = dl.lfs_media_url(f['url'])
            update.update(download='lfs_object' if media else 'unavailable', url=media or f['url'],
                          checksum='sha256:' + details['oid'] if details.get('oid') else None)
            if details.get('size'):
                details['pointer_bytes'] = f['size']
                update['header_details'] = dump(details)
        with s.db.connect() as c:
            c.execute('UPDATE sweep_files SET ' + ','.join(f'{k}=?' for k in update) + ' WHERE id=?', [*update.values(), f['id']])
    for m in members:
        info = json.loads(m['header_details'] or '{}')
        try:
            header = eyelink.probe_zip_member(client(s), budget, info['archive_url'], info['zip_member'], inv.kind_of(m['path']))
        except Exception as exc:
            header = 'probe_failed'
            info['note'] = str(exc)[:300]
        with s.db.connect() as c:
            c.execute('UPDATE sweep_files SET header=?,header_details=? WHERE id=?', (header, dump(info), m['id']))
        probed += 1
    eye = [r for r in rows if inv.kind_of(r['path'])]
    step('headers', 'ok' if probed else 'none', f'Read the first bytes of {probed} of {len(eye)} .edf/.asc file(s) (a sample, not every recording).')


def classify_files(s, project_id, inventory_id):
    rows = s.db.rows('SELECT * FROM sweep_files WHERE project_id=? AND inventory_id=?', (project_id, inventory_id))
    package = any(re.search(r'(?i)(^|/)(setup\.py|pyproject\.toml|DESCRIPTION|package\.json|CMakeLists\.txt|Cargo\.toml)$', r['path']) for r in rows)
    with s.db.connect() as c:
        for r in rows:
            header = r['header']
            role, reason = inv.classify(r['path'], r['size'], header, package)
            if r['download'] == 'lfs_object' and role == 'placeholder':
                reason += ' The LFS object can be downloaded from GitHub media storage.'
            c.execute('UPDATE sweep_files SET role=?,role_reason=?,exclusion_group=?,participant=? WHERE id=?',
                      (role, reason, inv.exclusion_group(r['path']), inv.participant_id(r['path']), r['id']))


def store_documents(s, project_id, docs, step):
    kept = 0
    with s.db.connect() as c:
        for d in docs:
            text = d.get('text') or ''
            words = len(re.findall(r'\w+', text))
            if d.get('kind') == 'error':
                status, reason = 'failed', d.get('method')
            elif words == 0:
                status, reason = 'empty', 'No text (for example an empty wiki page).'
            else:
                status, reason = 'readable', None
            sha = hashlib.sha256(text.encode()).hexdigest()
            if c.execute("SELECT 1 FROM discovery_documents WHERE project_id=? AND kind=? AND COALESCE(url,'')=COALESCE(?,'') AND sha256=?", (project_id, d.get('kind'), d.get('url'), sha)).fetchone():
                continue
            c.execute('INSERT INTO discovery_documents(id,project_id,kind,url,title,content,pdf_path,sha256,created,status,method,words,quality_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (uid(), project_id, d.get('kind'), d.get('url'), d.get('title'), text[:4_000_000], None, sha, now(), status, d.get('method'), words, reason))
            kept += 1
    readable = s.db.one("SELECT COUNT(*) n, COALESCE(SUM(words),0) w FROM discovery_documents WHERE project_id=? AND status='readable'", (project_id,))
    step('documentation', 'ok' if readable['n'] else 'none', f"{readable['n']} readable document(s), {readable['w']} words; {kept} new or changed.")


def readable_docs(s, project_id):
    return s.db.rows("SELECT id,kind,title,url,content AS text,words FROM discovery_documents WHERE project_id=? AND status='readable' AND kind NOT LIKE 'article%' ORDER BY rowid", (project_id,))


def find_publications(s, project_id, meta, step):
    docs = readable_docs(s, project_id)
    try:
        found = pubs.find(lookup(s), meta, docs)
    except repos.RateLimited:
        raise
    except Exception as exc:
        step('publications', 'failed', exc)
        return
    sid = uid()
    with s.db.connect() as c:
        for cand in found['candidates']:
            existing = c.execute('SELECT id FROM discovery_candidates WHERE project_id=? AND ((doi IS NOT NULL AND doi=?) OR (doi IS NULL AND url=?))', (project_id, cand['doi'], cand['url'])).fetchone()
            values = (cand['title'], cand['origin'], dump(cand['raw'])[:200_000], cand['url'], dump(cand['authors']), str(cand['year'] or ''), cand['venue'], cand['kind'], cand['strength'], dump(cand['evidence']), sid)
            if existing:
                c.execute('UPDATE discovery_candidates SET title=?,origin=?,raw=?,url=?,authors=?,year=?,venue=?,kind=?,strength=?,evidence=?,search_id=? WHERE id=?', (*values, existing['id']))
            else:
                c.execute('INSERT INTO discovery_candidates(id,project_id,doi,score,created,title,origin,raw,url,authors,year,venue,kind,strength,evidence,search_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (uid(), project_id, cand['doi'], None, now(), *values))
        c.execute('INSERT INTO publication_searches VALUES(?,?,?,?,?,?,?)', (sid, project_id, now(), dump(found['searches']), dump(found['rejected']), dump(found['non_publications']), len(found['candidates'])))
    step('publications', 'ok', f"{len(found['candidates'])} candidate(s); {len(found['non_publications'])} dataset/software/other identifier(s) set aside; "
         f"{len(found['rejected'])} search result(s) rejected for lack of evidence.")


def update_match(s, project_id, inventory_id, listing):
    files = s.db.rows('SELECT * FROM sweep_files WHERE project_id=? AND inventory_id=?', (project_id, inventory_id))
    eye = [f for f in files if inv.kind_of(f['path'])]
    confirmed = [f for f in eye if f['header'] == 'eyelink' and f['role'] in ('recording', 'recording_excluded_group')]
    real = [f for f in eye if f['role'] in ('recording', 'recording_excluded_group')]
    if confirmed:
        status = 'eyelink_confirmed'
    elif real:
        status = 'candidate_files'
    elif eye:
        # .edf/.asc names exist, but every one is non-EyeLink, an example/test file or a placeholder.
        status = 'no_study_recordings'
    elif not files and not listing.complete:
        status = 'inventory_failed'
    else:
        status = 'no_matching_files' if listing.complete else 'no_matching_files_incomplete'
    with s.db.connect() as c:
        c.execute('UPDATE sweep_projects SET match_status=?,eyelink_files=?,confirmed_files=? WHERE id=?', (status, len(eye), len(confirmed), project_id))
    return status


def suggest_relations(s, project_id):
    """Flag possible duplicates (same dataset in two places) for review; never merge automatically."""
    p = project_row(s, project_id)
    key = re.sub(r'[^a-z0-9]+', ' ', pubs.fold(pubs.clean_title(p['title'] or ''))).strip()
    related = {pubs.normalize_doi(r.get('identifier')) for r in json.loads(p['related'] or '[]')} - {None}
    text = ' '.join(d['text'] or '' for d in readable_docs(s, project_id)).lower()
    with s.db.connect() as c:
        for o in c.execute('SELECT * FROM sweep_projects WHERE id<>?', (project_id,)).fetchall():
            basis = []
            okey = re.sub(r'[^a-z0-9]+', ' ', pubs.fold(pubs.clean_title(o['title'] or ''))).strip()
            if key and len(key) > 20 and key == okey:
                basis.append('identical title')
            if o['doi'] and pubs.normalize_doi(o['doi']) in related:
                basis.append('listed as a related identifier')
            if o['url'] and o['url'].rstrip('/').lower() in text:
                basis.append('linked from this project\'s documentation')
            if basis:
                a, b = sorted((project_id, o['id']))
                c.execute('INSERT OR IGNORE INTO project_relations(id,a,b,basis,status,created) VALUES(?,?,?,?,?,?)', (uid(), a, b, '; '.join(basis), 'suggested', now()))


def project_row(s, project_id):
    p = s.db.one('SELECT * FROM sweep_projects WHERE id=?', (project_id,))
    if not p:
        raise KeyError('Repository project not found.')
    return p


# --------------------------------------------------------------------------- relevance

def current_config(s):
    setup(s)
    row = s.db.one('SELECT * FROM screening_configs ORDER BY rowid DESC LIMIT 1')
    if row:
        return row
    concepts = screening.DEFAULT_CONCEPTS
    cid = uid()
    with s.db.connect() as c:
        c.execute('INSERT INTO screening_configs VALUES(?,?,?,?,?)', (cid, dump(concepts), screening.concepts_hash(concepts), 'Default concepts from docs/search-terms.txt', now()))
    return s.db.one('SELECT * FROM screening_configs WHERE id=?', (cid,))


def save_config(s, text=None, document_id=None):
    setup(s)
    if document_id:
        doc = s.document(document_id)
        measurement, tasks = screening.concepts_from_document(doc['concepts'])
        if not tasks:
            raise ValueError('No task groups could be read from that document.')
        base = json.loads(current_config(s)['concepts'])
        concepts = {**base, 'measurement': sorted(set(base['measurement']) | set(measurement)), 'tasks': {**tasks, screening.GENERIC_TASK_GROUP: base['tasks'].get(screening.GENERIC_TASK_GROUP, [])}}
        origin = 'Task groups from imported document: ' + doc['name']
    else:
        concepts = screening.parse_concepts(text)
        origin = 'Edited in Orchard'
    cid = uid()
    with s.db.connect() as c:
        c.execute('INSERT INTO screening_configs VALUES(?,?,?,?,?)', (cid, dump(concepts), screening.concepts_hash(concepts), origin, now()))
        audit(c, 'screening_concepts_saved', cid, {'origin': origin, 'sha256': screening.concepts_hash(concepts)})
    return config_view(s)


def config_view(s):
    row = current_config(s)
    concepts = json.loads(row['concepts'])
    return {**row, 'text': screening.format_concepts(concepts), 'method': screening.__doc__, 'method_version': screening.METHOD_VERSION,
            'exclusion_reasons': list(EXCLUSION_REASONS), 'history': s.db.rows('SELECT id,origin,sha256,created FROM screening_configs ORDER BY rowid DESC')}


def article_texts(s, project_id):
    out = []
    for link in s.db.rows("SELECT paper_id FROM project_papers WHERE project_id=? AND status='confirmed'", (project_id,)):
        for t in s.db.rows('SELECT id,origin,content FROM fulltexts WHERE paper_id=? ORDER BY rowid DESC', (link['paper_id'],)):
            out.append({'id': 'fulltext:' + t['id'], 'kind': 'article', 'title': t['origin'][:200], 'text': t['content']})
    return out


def article_status(s, project_id):
    links = s.db.rows("SELECT paper_id FROM project_papers WHERE project_id=? AND status='confirmed'", (project_id,))
    if not links:
        none = s.db.one("SELECT action FROM publication_decisions WHERE project_id=? AND candidate_id IS NULL ORDER BY rowid DESC LIMIT 1", (project_id,))
        return 'not_identified' if none and none['action'] == 'none_identified' else 'no_publication'
    ids = [l['paper_id'] for l in links]
    marks = ','.join('?' * len(ids))
    words = [len(re.findall(r'\w+', t['content'])) for t in s.db.rows(f'SELECT content FROM fulltexts WHERE paper_id IN ({marks})', ids)]
    if any(w >= 1000 for w in words):
        return 'readable'
    if s.db.one(f'SELECT 1 FROM pdfs WHERE paper_id IN ({marks})', ids):
        return 'pdf_only'
    if s.db.one(f'SELECT 1 FROM retrieval_attempts WHERE paper_id IN ({marks})', ids):
        return 'inaccessible'
    return 'not_attempted'


def assess(s, project_id):
    setup(s)
    p = project_row(s, project_id)
    config = current_config(s)
    sources = [{'id': 'project', 'kind': 'metadata', 'title': 'Repository title, description and keywords',
                'text': '\n'.join(filter(None, [p['title'], p['description'], ', '.join(json.loads(p['keywords'] or '[]'))]))}]
    sources += [{'id': d['id'], 'kind': d['kind'], 'title': d['title'], 'text': d['text']} for d in readable_docs(s, project_id)]
    sources += article_texts(s, project_id)
    files = s.db.rows('SELECT path,role,header,header_details FROM sweep_files WHERE project_id=? AND (inventory_id=? OR ? IS NULL)', (project_id, p['inventory_id'], p['inventory_id']))
    for f in files:
        f['header_details'] = json.loads(f['header_details'] or '{}')
    status = article_status(s, project_id)
    result = screening.assess(sources, json.loads(config['concepts']), files, {'readable': 'readable', 'not_identified': 'not_identified', 'no_publication': 'not_identified', 'inaccessible': 'inaccessible'}.get(status))
    aid = uid()
    with s.db.connect() as c:
        c.execute('INSERT INTO relevance_assessments VALUES(?,?,?,?,?,?,?,?,?,?,?)', (aid, project_id, config['id'], result['status'], result['reason'], dump(result['notes']),
                  dump(result['counts']), dump(result['evidence']), dump([{'id': x['id'], 'kind': x['kind'], 'title': x['title'], 'words': len(re.findall(r'\w+', x['text'] or ''))} for x in sources]),
                  dump({**result['reproducibility'], 'article_status': status}), now()))
    return s.db.one('SELECT * FROM relevance_assessments WHERE id=?', (aid,))


# --------------------------------------------------------------------------- publications and articles

def reviewer_name(value):
    value = (value or '').strip()
    if not value:
        raise ValueError('Enter the reviewer name.')
    return value[:120]


def find_or_create_paper(s, info, origin):
    doi = pubs.normalize_doi(info.get('doi'))
    with s.db.connect() as c:
        if doi:
            row = c.execute("SELECT paper_id FROM identifiers WHERE kind='doi' AND value=?", (doi,)).fetchone()
            if row:
                pid = row['paper_id']
                while True:
                    merged = c.execute('SELECT merged_into FROM papers WHERE id=?', (pid,)).fetchone()
                    if not merged or not merged['merged_into']:
                        return pid
                    pid = merged['merged_into']
        pid = uid()
        authors = info.get('authors') or []
        c.execute('INSERT INTO papers(id,title,abstract,authors,year,doi,publication_type,created) VALUES(?,?,?,?,?,?,?,?)',
                  (pid, info.get('title'), None, ', '.join(a for a in authors if a) or None, str(info.get('year') or '') or None, doi, info.get('kind'), now()))
        source_id = 'doi:' + doi if doi else 'manual:' + pid
        raw = dump({k: v for k, v in info.items() if k != 'raw'} | {'raw': info.get('raw')})
        sid = uid()
        c.execute('INSERT INTO source_records VALUES(?,?,?,?,?,?,?)', (sid, 'repository-link', source_id, pid, raw, hashlib.sha256(raw.encode()).hexdigest(), now()))
        c.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?)', ('repository-link', source_id, pid))
        if doi:
            c.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?)', ('doi', doi, pid))
            s.add_resource(c, pid, 'landing', 'https://doi.org/' + doi, 'DOI of the publication linked to a repository project', origin)
        for kind in ('pmid', 'pmcid'):
            if info.get(kind):
                c.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?)', (kind, str(info[kind]), pid))
        if info.get('url') and not doi:
            s.add_resource(c, pid, 'landing', info['url'], 'Publication URL entered for a repository project', origin)
        audit(c, 'paper_created', pid, {'source_id': source_id, 'provider': 'repository-link', 'origin': origin})
    return pid


def publication_action(s, project_id, action, reviewer=None, reason='', candidate_id=None, doi=None, url=None, title=None, authors=None, year=None, force=False):
    setup(s)
    project_row(s, project_id)
    reviewer = reviewer_name(reviewer)
    reason = (reason or '').strip()
    if action not in ('confirm', 'reject', 'none_identified', 'manual', 'unlink'):
        raise ValueError('Unknown publication action.')
    paper_id = None
    if action == 'manual':
        info = None
        if doi:
            info = lookup(s).classify(doi)
            if not info:
                raise ValueError('That is not a valid DOI.')
            if info['kind'] in ('dataset', 'software') and not force:
                raise ValueError(f"{info['doi']} identifies a {info['kind']} ({info.get('type')}), not a paper. Confirm again to link it anyway.")
            if not info.get('title') and title:
                info['title'] = title
        elif title or url:
            info = {'doi': None, 'url': (url or '').strip() or None, 'title': (title or '').strip() or None, 'authors': [a.strip() for a in (authors or '').split(';') if a.strip()] if isinstance(authors, str) else authors or [],
                    'year': year, 'kind': 'publication', 'venue': None, 'raw': {}}
        else:
            raise ValueError('Enter a DOI, or a title and URL.')
        cid = uid()
        with s.db.connect() as c:
            c.execute('INSERT INTO discovery_candidates(id,project_id,doi,title,score,origin,raw,created,url,authors,year,venue,kind,strength,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (cid, project_id, info.get('doi'), info.get('title'), None, f'Entered by {reviewer}', dump(info.get('raw') or {})[:200_000], now(),
                       info.get('url') or (('https://doi.org/' + info['doi']) if info.get('doi') else None), dump(info.get('authors') or []), str(info.get('year') or ''),
                       info.get('venue'), info.get('kind'), 'manual', dump([reason or 'Entered manually by the reviewer.'])))
        candidate_id, action = cid, 'confirm'
    candidate = None
    if action in ('confirm', 'reject'):
        candidate = s.db.one('SELECT * FROM discovery_candidates WHERE id=? AND project_id=?', (candidate_id, project_id))
        if not candidate:
            raise KeyError('Candidate not found for this project.')
    if action == 'reject' and not reason:
        raise ValueError('Explain why this is not the project\'s publication.')
    if action == 'none_identified' and not reason:
        raise ValueError('Record what was checked before concluding that no publication is identified.')
    if action == 'confirm':
        info = {'doi': candidate['doi'], 'url': candidate['url'], 'title': candidate['title'], 'authors': json.loads(candidate['authors'] or '[]'),
                'year': candidate['year'], 'kind': candidate['kind'], 'venue': candidate['venue'], 'raw': json.loads(candidate['raw'] or '{}')}
        for key in ('pmid', 'pmcid'):
            if info['raw'].get(key):
                info[key] = info['raw'][key]
        paper_id = find_or_create_paper(s, info, f'Confirmed for repository project by {reviewer}')
        with s.db.connect() as c:
            c.execute('INSERT INTO project_papers(id,project_id,paper_id,candidate_id,relation,status,created) VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,paper_id) DO UPDATE SET status=\'confirmed\',candidate_id=excluded.candidate_id',
                      (uid(), project_id, paper_id, candidate_id, candidate['origin'], 'confirmed', now()))
    if action == 'unlink':
        link = s.db.one('SELECT * FROM project_papers WHERE project_id=? AND paper_id=?', (project_id, candidate_id))
        if not link:
            raise KeyError('No such linked paper.')
        if not reason:
            raise ValueError('Explain why the link is removed.')
        paper_id, candidate_id = link['paper_id'], link['candidate_id']
        with s.db.connect() as c:
            c.execute("UPDATE project_papers SET status='removed' WHERE id=?", (link['id'],))
    with s.db.connect() as c:
        c.execute('INSERT INTO publication_decisions VALUES(?,?,?,?,?,?,?,?)', (uid(), project_id, candidate_id, paper_id, action, reason, reviewer, now()))
        audit(c, 'repository_publication_' + action, project_id, {'candidate_id': candidate_id, 'paper_id': paper_id, 'reviewer': reviewer, 'reason': reason})
    assess(s, project_id)
    return project_detail(s, project_id)


def record_attempt(s, paper_id, project_id, source, url, outcome, reason, final=None, status=None, kind=None, size=None):
    with s.db.connect() as c:
        c.execute('INSERT INTO retrieval_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (uid(), paper_id, project_id, source, url, final, outcome, (reason or '')[:1000], status, kind, size, now()))


def retrieve(s, project_id, paper_id):
    """Retrieve readable text for a confirmed paper through every route; record each attempt."""
    setup(s)
    if not s.db.one("SELECT 1 FROM project_papers WHERE project_id=? AND paper_id=? AND status='confirmed'", (project_id, paper_id)):
        raise ValueError('Confirm the publication before retrieving it.')
    paper = s.paper(paper_id)
    success = None
    try:
        result = s.fetch_fulltext(paper_id)
        r = result.get('retrieval') or {}
        for a in r.get('attempts', []):
            record_attempt(s, paper_id, project_id, a['source'], a['target'], a['outcome'], a['detail'])
        success = r
    except ValueError as exc:
        event = s.db.one("SELECT detail FROM audit WHERE event='fulltext_retrieval_failed' AND entity_id=? ORDER BY id DESC LIMIT 1", (paper_id,))
        attempts = json.loads(event['detail']).get('attempts', []) if event else []
        for a in attempts:
            record_attempt(s, paper_id, project_id, a['source'], a['target'], a['outcome'], a['detail'])
        if not attempts:
            record_attempt(s, paper_id, project_id, 'identifier routes', None, 'not_attempted', str(exc))
    if not success or not success.get('text_extracted'):
        # Publisher landing page: accept only article HTML or a real PDF.
        doi = paper.get('doi')
        landing = next((r['url'] for r in paper['resources'] if r['kind'] == 'landing' and r['url']), None)
        url = 'https://doi.org/' + doi if doi else landing
        if url:
            page = articles.fetch_landing(s.fetcher, url)
            record_attempt(s, paper_id, project_id, 'publisher landing page', url, page['outcome'], page['reason'], page.get('final'), page.get('status'), page.get('content_type'), page.get('bytes'))
            if page['outcome'] == 'readable_html':
                s.import_fulltext(paper_id, page['text'][:4_000_000], f"Publisher HTML (extracted text) from {page['final']}")
                success = {'source': 'publisher', 'format': 'html', 'url': page['final'], 'text_extracted': True}
            elif page['outcome'] == 'pdf':
                stored = s.store_pdf(paper_id, page['data'], 'article.pdf', page['final'], None, {'source': 'publisher landing'})
                success = {'source': 'publisher', 'format': 'pdf', 'url': page['final'], 'pdf_id': stored['pdf']['id'], 'text_extracted': stored['text_extracted']}
            elif page.get('pdf_url'):
                pdf = articles.fetch_landing(s.fetcher, page['pdf_url'])
                record_attempt(s, paper_id, project_id, 'citation_pdf_url', page['pdf_url'], pdf['outcome'], pdf['reason'], pdf.get('final'), pdf.get('status'), pdf.get('content_type'), pdf.get('bytes'))
                if pdf['outcome'] == 'pdf':
                    stored = s.store_pdf(paper_id, pdf['data'], 'article.pdf', pdf['final'], None, {'source': 'citation_pdf_url'})
                    success = {'source': 'publisher', 'format': 'pdf', 'url': pdf['final'], 'pdf_id': stored['pdf']['id'], 'text_extracted': stored['text_extracted']}
    with s.db.connect() as c:
        audit(c, 'repository_article_retrieval', project_id, {'paper_id': paper_id, 'result': success})
    assess(s, project_id)
    detail = project_detail(s, project_id)
    detail['retrieval'] = success
    return detail


# --------------------------------------------------------------------------- screening decisions, studies, relations

def decide(s, project_id, decision, reason, reviewer=None, exclusion_reason=None, evidence=None, version=None, criteria_id=None):
    setup(s)
    project_row(s, project_id)
    reviewer = reviewer_name(reviewer)
    reason = (reason or '').strip()
    if decision not in DECISIONS:
        raise ValueError('Choose include, exclude, needs review or awaiting information.')
    if not reason:
        raise ValueError('Record the reason and the evidence you relied on.')
    if decision == 'exclude':
        if exclusion_reason not in EXCLUSION_REASONS:
            raise ValueError('Choose an exclusion reason. Missing article access is not an exclusion; use "awaiting information".')
    else:
        exclusion_reason = None
    criteria = s.db.one('SELECT * FROM criteria ORDER BY rowid DESC LIMIT 1')
    if decision in ('include', 'exclude') and not criteria:
        raise ValueError('Save eligibility criteria (Project & criteria) before including or excluding.')
    if criteria_id and criteria and criteria_id != criteria['id']:
        from .service import Conflict
        raise Conflict('Eligibility criteria changed since this page loaded. Reload and review them.')
    current = s.db.one('SELECT MAX(version) v FROM project_decisions WHERE project_id=?', (project_id,))['v'] or 0
    if version is not None and int(version) != current:
        from .service import Conflict
        raise Conflict('Another decision was saved for this project since the page loaded. Reload to see it before saving.')
    assessment = s.db.one('SELECT id,status FROM relevance_assessments WHERE project_id=? ORDER BY rowid DESC LIMIT 1', (project_id,))
    config = current_config(s)
    snapshot = {'criteria': {'id': criteria['id'], 'inclusion': criteria['inclusion'], 'exclusion': criteria['exclusion']} if criteria else None,
                'screening_concepts': {'id': config['id'], 'sha256': config['sha256']}, 'automated_assessment': assessment}
    did = uid()
    with s.db.connect() as c:
        c.execute('INSERT INTO project_decisions(id,project_id,version,decision,exclusion_reason,reason,evidence,reviewer,criteria_id,criteria,config_id,assessment_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (did, project_id, current + 1, decision, exclusion_reason, reason, dump(evidence or []), reviewer, criteria['id'] if criteria else None, dump(snapshot),
                   config['id'], assessment['id'] if assessment else None, now()))
        audit(c, 'repository_decision_saved', project_id, {'decision_id': did, 'version': current + 1, 'decision': decision, 'exclusion_reason': exclusion_reason, 'reviewer': reviewer})
    return project_detail(s, project_id)


def study_action(s, project_id, action, reviewer=None, study_id=None, label=None, note=''):
    setup(s)
    project_row(s, project_id)
    reviewer = reviewer_name(reviewer)
    with s.db.connect() as c:
        if action == 'new':
            if not (label or '').strip():
                raise ValueError('Name the study (for example first author, year and experiment).')
            study_id = uid()
            c.execute('INSERT INTO studies VALUES(?,?,?,?)', (study_id, label.strip()[:200], note, now()))
            action = 'assign'
        if action == 'assign':
            if not c.execute('SELECT 1 FROM studies WHERE id=?', (study_id,)).fetchone():
                raise KeyError('Study not found.')
            c.execute("UPDATE study_links SET removed=? WHERE kind='project' AND ref_id=? AND removed IS NULL", (now(), project_id))
            c.execute('INSERT INTO study_links VALUES(?,?,?,?,?,?,?,?)', (uid(), study_id, 'project', project_id, reviewer, note, now(), None))
            for link in c.execute("SELECT paper_id FROM project_papers WHERE project_id=? AND status='confirmed'", (project_id,)).fetchall():
                if not c.execute("SELECT 1 FROM study_links WHERE kind='paper' AND ref_id=? AND study_id=? AND removed IS NULL", (link['paper_id'], study_id)).fetchone():
                    c.execute('INSERT INTO study_links VALUES(?,?,?,?,?,?,?,?)', (uid(), study_id, 'paper', link['paper_id'], reviewer, 'Linked through repository project', now(), None))
        elif action == 'remove':
            c.execute("UPDATE study_links SET removed=? WHERE kind='project' AND ref_id=? AND removed IS NULL", (now(), project_id))
        else:
            raise ValueError('Unknown study action.')
        audit(c, 'repository_study_' + action, project_id, {'study_id': study_id, 'reviewer': reviewer})
    return project_detail(s, project_id)


def relation_action(s, relation_id, status, reviewer=None, reason=''):
    setup(s)
    reviewer = reviewer_name(reviewer)
    if status not in ('same_dataset', 'related', 'distinct'):
        raise ValueError('Choose same dataset, related or distinct.')
    row = s.db.one('SELECT * FROM project_relations WHERE id=?', (relation_id,))
    if not row:
        raise KeyError('Relation not found.')
    with s.db.connect() as c:
        c.execute('UPDATE project_relations SET status=?,reviewer=?,reason=?,decided=? WHERE id=?', (status, reviewer, reason, now(), relation_id))
        audit(c, 'repository_relation_decided', relation_id, {'before': row['status'], 'status': status, 'reviewer': reviewer, 'reason': reason})
    return {'ok': True}


# --------------------------------------------------------------------------- downloads

def download_plan(s, project_id, file_ids):
    setup(s)
    p = project_row(s, project_id)
    if not isinstance(file_ids, list) or not file_ids:
        raise ValueError('Select at least one file.')
    root = Path(s.db.path).parent / 'downloads'
    rows, blocked = [], []
    for fid in dict.fromkeys(file_ids):
        f = s.db.one('SELECT * FROM sweep_files WHERE id=? AND project_id=?', (fid, project_id))
        if not f:
            raise KeyError('File not found in this project.')
        url = f['url'] if f['download'] == 'lfs_object' else (f['probe_url'] or f['url'])
        if f['download'] in ('auth_required', 'restricted', 'unavailable', 'inside_archive') or not url:
            blocked.append({'path': f['path'], 'reason': {'auth_required': 'The repository requires sign-in or an API token.', 'restricted': 'Restricted or embargoed.',
                            'inside_archive': 'Inside an archive; download the archive instead.'}.get(f['download'], 'No download URL.')})
            continue
        size = f['size']
        if f['download'] == 'lfs_object':
            size = json.loads(f['header_details'] or '{}').get('size') or size
        rows.append({'file_id': f['id'], 'path': f['path'], 'url': url, 'size': size, 'checksum': f['checksum'],
                     'dest': str(dl.target_path(root, p['repository'], p['external_id'], f['path'])), 'role': f['role']})
    total = sum(r['size'] or 0 for r in rows)
    return {'project_id': project_id, 'destination': str(dl.target_path(root, p['repository'], p['external_id'], 'x').parent), 'files': rows, 'blocked': blocked,
            'total_bytes': total, 'unknown_sizes': sum(r['size'] is None for r in rows), 'needs_large_confirmation': total > dl.LARGE}


def download(s, project_id, file_ids, reviewer=None, confirm_large=False, background=True):
    plan = download_plan(s, project_id, file_ids)
    if not plan['files']:
        raise ValueError('None of the selected files can be downloaded: ' + '; '.join(b['path'] + ': ' + b['reason'] for b in plan['blocked']))
    if plan['needs_large_confirmation'] and not confirm_large:
        raise ValueError(f"The selection is {plan['total_bytes'] / 1e9:.1f} GB. Confirm the large download explicitly.")
    bid = uid()
    with s.db.connect() as c:
        c.execute('INSERT INTO download_batches(id,project_id,dest,files,total_bytes,status,reviewer,created) VALUES(?,?,?,?,?,?,?,?)',
                  (bid, project_id, plan['destination'], len(plan['files']), plan['total_bytes'], 'queued', (reviewer or '').strip() or None, now()))
        for f in plan['files']:
            c.execute('INSERT INTO downloads(id,batch_id,project_id,file_id,path,url,dest,expected_size,expected_checksum,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (uid(), bid, project_id, f['file_id'], f['path'], f['url'], f['dest'], f['size'], f['checksum'], 'queued', now()))
        audit(c, 'repository_download_started', bid, {'project_id': project_id, 'files': len(plan['files']), 'bytes': plan['total_bytes'], 'blocked': plan['blocked']})
    if background:
        submit(s, run_downloads, s, bid)
    else:
        run_downloads(s, bid)
    return batch_detail(s, bid)


def run_downloads(s, bid, downloader=None):
    downloader = downloader or getattr(s, 'downloader', None) or dl.Downloader()
    with s.db.connect() as c:
        c.execute("UPDATE download_batches SET status='running' WHERE id=?", (bid,))
    batch = s.db.one('SELECT * FROM download_batches WHERE id=?', (bid,))
    stop = lambda: bool(s.db.one('SELECT cancel_requested FROM download_batches WHERE id=?', (bid,))['cancel_requested'])
    for row in s.db.rows("SELECT * FROM downloads WHERE batch_id=? AND status NOT IN ('verified','size_verified') ORDER BY rowid", (bid,)):
        if stop():
            break
        with s.db.connect() as c:
            c.execute("UPDATE downloads SET status='running' WHERE id=?", (row['id'],))
        last = [0.0]

        def progress(done, rid=row['id']):
            if time.monotonic() - last[0] > 0.5:
                last[0] = time.monotonic()
                with s.db.connect() as c:
                    c.execute('UPDATE downloads SET bytes_done=? WHERE id=?', (done, rid))
        try:
            result = downloader.fetch(row['url'], row['dest'], row['expected_size'], progress, stop)
            if result['status'] == 'cancelled':
                with s.db.connect() as c:
                    c.execute("UPDATE downloads SET status='cancelled',bytes_done=?,detail=? WHERE id=?", (result['bytes'], 'Cancelled; the partial file is kept for resuming.', row['id']))
                break
            status, detail, sha = dl.verify(row['dest'], row['expected_size'], row['expected_checksum'])
            detail = ' '.join(result['notes'] + [detail])
        except Exception as exc:
            status, detail, sha = 'failed', f'{type(exc).__name__}: {exc}'[:800], None
            result = {'bytes': 0}
        with s.db.connect() as c:
            c.execute('UPDATE downloads SET status=?,detail=?,sha256=?,bytes_done=?,finished=? WHERE id=?', (status, detail, sha, result['bytes'], now(), row['id']))
    rows = s.db.rows('SELECT * FROM downloads WHERE batch_id=? ORDER BY rowid', (bid,))
    states = {r['status'] for r in rows}
    status = 'cancelled' if stop() else 'completed' if states <= {'verified', 'size_verified'} else 'partial'
    project = project_row(s, batch['project_id'])
    batch = {**batch, 'finished': now()}
    data = dl.manifest(batch, project, rows)
    try:
        path = dl.write_manifest(batch['dest'], data)
    except OSError as exc:
        path = None
        data['manifest_error'] = str(exc)
    with s.db.connect() as c:
        c.execute('UPDATE download_batches SET status=?,finished=?,manifest=?,manifest_path=? WHERE id=?', (status, now(), dump(data), path, bid))
        audit(c, 'repository_download_' + status, bid, {'files': len(rows), 'states': sorted(states), 'manifest': path})


def download_action(s, bid, action, background=True):
    setup(s)
    batch = s.db.one('SELECT * FROM download_batches WHERE id=?', (bid,))
    if not batch:
        raise KeyError('Download batch not found.')
    if action == 'cancel':
        with s.db.connect() as c:
            c.execute('UPDATE download_batches SET cancel_requested=1 WHERE id=?', (bid,))
    elif action == 'resume':
        if batch['status'] in ('queued', 'running'):
            raise ValueError('This download is still running.')
        with s.db.connect() as c:
            c.execute("UPDATE download_batches SET cancel_requested=0,status='queued' WHERE id=?", (bid,))
            c.execute("UPDATE downloads SET status='queued' WHERE batch_id=? AND status NOT IN ('verified','size_verified')", (bid,))
        submit(s, run_downloads, s, bid) if background else run_downloads(s, bid)
    else:
        raise ValueError('Unknown download action.')
    return batch_detail(s, bid)


def batch_detail(s, bid):
    batch = s.db.one('SELECT * FROM download_batches WHERE id=?', (bid,))
    if not batch:
        raise KeyError('Download batch not found.')
    batch['items'] = s.db.rows('SELECT * FROM downloads WHERE batch_id=? ORDER BY rowid', (bid,))
    batch.pop('manifest', None)
    return batch


# --------------------------------------------------------------------------- views

def job_detail(s, job):
    row = s.db.one('SELECT * FROM discovery_jobs WHERE id=?', (job,))
    if not row:
        raise KeyError('Job not found.')
    row['parameters'] = json.loads(row['parameters'] or '{}')
    row['progress'] = json.loads(row['progress'] or '{}')
    row['tasks'] = s.db.rows('SELECT id,position,source,kind,query,label,coverage,status,pages,records,reported_total,projects,retry_after,error,started,finished,updated FROM discovery_tasks WHERE job_id=? ORDER BY position', (job,))
    row['records_examined'] = sum(t['records'] for t in row['tasks'])
    row['unique_projects'] = s.db.one('SELECT COUNT(DISTINCT project_id) n FROM discovery_observations WHERE job_id=?', (job,))['n'] if row['version'] == 2 else s.db.one('SELECT COUNT(*) n FROM discovery_observations WHERE job_id=?', (job,))['n']
    ids = [r['project_id'] for r in s.db.rows('SELECT DISTINCT project_id FROM discovery_observations WHERE job_id=? AND project_id IS NOT NULL', (job,))]
    row['project_status'] = {}
    if ids:
        marks = ','.join('?' * len(ids))
        for r in s.db.rows(f"SELECT COALESCE(match_status,'not_inspected') m, COUNT(*) n FROM sweep_projects WHERE id IN ({marks}) GROUP BY m", ids):
            row['project_status'][r['m']] = r['n']
    return row


def overview(s):
    setup(s)
    jobs = [job_detail(s, j['id']) for j in s.db.rows('SELECT id FROM discovery_jobs ORDER BY created DESC LIMIT 30')]
    legacy = s.db.rows('SELECT * FROM sweep_runs ORDER BY started DESC LIMIT 20')
    return {'jobs': jobs, 'legacy_sweeps': legacy, 'sources': list(repos.SOURCES), 'default_phrases': list(repos.DEFAULT_PHRASES),
            'tokens': {'github': bool(repos.env('ORCHARD_GITHUB_TOKEN', 'GITHUB_TOKEN')), 'zenodo': bool(repos.env('ORCHARD_ZENODO_TOKEN')), 'dryad': bool(repos.env('ORCHARD_DRYAD_TOKEN'))},
            'counts': funnel(s)['summary']}


def latest(s, table, project_id, order='rowid'):
    return s.db.one(f'SELECT * FROM {table} WHERE project_id=? ORDER BY {order} DESC LIMIT 1', (project_id,))


def publication_state(s, project_id):
    if s.db.one("SELECT 1 FROM project_papers WHERE project_id=? AND status='confirmed'", (project_id,)):
        return 'confirmed'
    last = s.db.one("SELECT action FROM publication_decisions WHERE project_id=? AND candidate_id IS NULL ORDER BY rowid DESC LIMIT 1", (project_id,))
    if last and last['action'] == 'none_identified':
        return 'none_identified'
    open_candidates = [c for c in s.db.rows("SELECT id FROM discovery_candidates WHERE project_id=? AND strength<>'legacy'", (project_id,))
                       if not (s.db.one('SELECT action FROM publication_decisions WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1', (c['id'],)) or {}).get('action') == 'reject']
    if open_candidates:
        return 'proposed'
    if s.db.one('SELECT 1 FROM publication_searches WHERE project_id=?', (project_id,)):
        return 'not_found'
    return 'not_searched'


def summary_row(s, p):
    a = latest(s, 'relevance_assessments', p['id'])
    d = s.db.one('SELECT decision,exclusion_reason,version,reviewer,created FROM project_decisions WHERE project_id=? ORDER BY version DESC LIMIT 1', (p['id'],))
    study = s.db.one("SELECT st.id,st.label FROM study_links l JOIN studies st ON st.id=l.study_id WHERE l.kind='project' AND l.ref_id=? AND l.removed IS NULL", (p['id'],))
    return {'id': p['id'], 'repository': p['repository'], 'external_id': p['external_id'], 'url': p['url'], 'title': p['title'], 'match_status': p['match_status'] or 'not_inspected',
            'eyelink_files': p['eyelink_files'], 'confirmed_files': p['confirmed_files'], 'inspection_status': p['inspection_status'], 'last_inspected': p['last_inspected'],
            'relevance': a['status'] if a else None, 'publication': publication_state(s, p['id']), 'article': article_status(s, p['id']),
            'decision': d, 'study': study, 'first_seen': p['first_seen']}


def projects(s, q='', match='', relevance='', decision='', page=1):
    setup(s)
    rows = s.db.rows("SELECT * FROM sweep_projects ORDER BY COALESCE(first_seen,'') DESC, rowid DESC")
    out = []
    for p in rows:
        if q and q.lower() not in ' '.join(str(p.get(k) or '') for k in ('title', 'external_id', 'description', 'repository')).lower():
            continue
        row = summary_row(s, p)
        if match == 'with_files' and row['match_status'] not in MATCHING:
            continue
        if match and match != 'with_files' and row['match_status'] != match:
            continue
        if relevance and row['relevance'] != relevance:
            continue
        current = (row['decision'] or {}).get('decision') or 'undecided'
        if decision and current != decision:
            continue
        out.append(row)
    page = max(1, int(page or 1))
    return {'total': len(out), 'page': page, 'pages': max(1, (len(out) + 49) // 50), 'items': out[(page - 1) * 50:page * 50]}


def project_detail(s, project_id):
    setup(s)
    p = project_row(s, project_id)
    for key in ('creators', 'keywords', 'related', 'inspection_steps'):
        p[key] = json.loads(p.get(key) or '[]')
    p.pop('metadata', None)
    files = s.db.rows('SELECT * FROM sweep_files WHERE project_id=? ORDER BY path', (project_id,))
    current, stale = [], 0
    for f in files:
        f['header_details'] = json.loads(f['header_details'] or '{}')
        f['header_details'].pop('zip_member', None)
        if p['inventory_id'] and f['inventory_id'] != p['inventory_id']:
            stale += 1
            continue
        current.append(f)
    docs = s.db.rows('SELECT id,kind,url,title,status,method,words,quality_reason,pdf_path,created FROM discovery_documents WHERE project_id=? ORDER BY rowid', (project_id,))
    candidates = s.db.rows('SELECT id,doi,url,title,authors,year,venue,kind,strength,origin,evidence,score,created FROM discovery_candidates WHERE project_id=? ORDER BY rowid', (project_id,))
    for cand in candidates:
        cand['authors'] = json.loads(cand['authors'] or '[]')
        cand['evidence'] = json.loads(cand['evidence'] or '[]')
        last = s.db.one('SELECT action,reason,reviewer,created FROM publication_decisions WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1', (cand['id'],))
        cand['decision'] = last
    search = latest(s, 'publication_searches', project_id)
    if search:
        for key in ('searches', 'rejected', 'non_publications'):
            search[key] = json.loads(search[key] or '[]')
    papers = []
    for link in s.db.rows('SELECT * FROM project_papers WHERE project_id=? ORDER BY rowid', (project_id,)):
        paper = s.db.one('SELECT id,title,authors,year,doi,publication_type FROM papers WHERE id=?', (link['paper_id'],))
        paper['link'] = link
        paper['pdfs'] = s.db.rows('SELECT id,name,origin,size,license,sha256,created FROM pdfs WHERE paper_id=? ORDER BY rowid DESC', (link['paper_id'],))
        paper['fulltexts'] = [{'id': t['id'], 'origin': t['origin'], 'created': t['created'], 'words': len(re.findall(r'\w+', t['content']))} for t in
                              s.db.rows('SELECT id,origin,created,content FROM fulltexts WHERE paper_id=? ORDER BY rowid DESC', (link['paper_id'],))]
        paper['attempts'] = s.db.rows('SELECT source,url,final_url,outcome,reason,http_status,created FROM retrieval_attempts WHERE paper_id=? ORDER BY rowid DESC LIMIT 40', (link['paper_id'],))
        paper['other_projects'] = s.db.rows("SELECT p.id,p.title,p.repository FROM project_papers l JOIN sweep_projects p ON p.id=l.project_id WHERE l.paper_id=? AND l.project_id<>? AND l.status='confirmed'", (link['paper_id'], project_id))
        papers.append(paper)
    assessment = latest(s, 'relevance_assessments', project_id)
    if assessment:
        for key in ('notes', 'counts', 'evidence', 'sources', 'reproducibility'):
            assessment[key] = json.loads(assessment[key] or 'null')
    decisions = s.db.rows('SELECT * FROM project_decisions WHERE project_id=? ORDER BY version DESC', (project_id,))
    for d in decisions:
        d['criteria'] = json.loads(d['criteria'] or 'null')
        d['evidence'] = json.loads(d['evidence'] or '[]')
    docs_text = '\n'.join(d['text'] or '' for d in readable_docs(s, project_id))
    probes = [{'header': f['header'], 'details': f['header_details']} for f in current if f['header']]
    roles = {}
    for f in current:
        roles[f['role'] or 'unclassified'] = roles.get(f['role'] or 'unclassified', 0) + 1
    relations = s.db.rows('SELECT * FROM project_relations WHERE a=? OR b=? ORDER BY rowid', (project_id, project_id))
    for r in relations:
        other = r['b'] if r['a'] == project_id else r['a']
        r['other'] = s.db.one('SELECT id,title,repository,url FROM sweep_projects WHERE id=?', (other,))
    observations = s.db.rows('SELECT o.job_id,o.created,o.matched_files,t.label FROM discovery_observations o LEFT JOIN discovery_tasks t ON t.id=o.task_id WHERE o.project_id=? OR (o.project_id IS NULL AND o.external_id=? AND o.repository=?) ORDER BY o.rowid DESC LIMIT 50', (project_id, p['external_id'], p['repository']))
    study = s.db.one("SELECT st.* FROM study_links l JOIN studies st ON st.id=l.study_id WHERE l.kind='project' AND l.ref_id=? AND l.removed IS NULL", (project_id,))
    return {'project': p, 'files': current, 'stale_files': stale, 'roles': roles, 'role_labels': inv.ROLE_LABELS,
            'documents': docs, 'candidates': candidates, 'publication_search': search, 'publication_state': publication_state(s, project_id),
            'publication_history': s.db.rows('SELECT * FROM publication_decisions WHERE project_id=? ORDER BY rowid DESC', (project_id,)),
            'papers': papers, 'article_status': article_status(s, project_id), 'assessment': assessment, 'decisions': decisions,
            'current_version': decisions[0]['version'] if decisions else 0, 'criteria': s.db.one('SELECT * FROM criteria ORDER BY rowid DESC LIMIT 1'),
            'exclusion_reasons': list(EXCLUSION_REASONS), 'checklist': inv.checklist(current, docs_text, p['license'], probes),
            'downloads': [batch_detail(s, b['id']) for b in s.db.rows('SELECT id FROM download_batches WHERE project_id=? ORDER BY rowid DESC', (project_id,))],
            'relations': relations, 'observations': observations, 'study': study, 'studies': s.db.rows('SELECT * FROM studies ORDER BY label'),
            'legacy_events': s.db.rows("SELECT * FROM discovery_events WHERE project_id=? AND stage<>'human' ORDER BY rowid DESC", (project_id,))}


def document(s, doc_id):
    setup(s)
    doc = s.db.one('SELECT * FROM discovery_documents WHERE id=?', (doc_id,))
    if not doc:
        raise KeyError('Document not found.')
    return doc


def fulltext(s, fulltext_id):
    row = s.db.one('SELECT id,paper_id,origin,content,created FROM fulltexts WHERE id=?', (fulltext_id,))
    if not row:
        raise KeyError('Text not found.')
    return row


# --------------------------------------------------------------------------- funnel and export

BIAS_NOTE = ('Repository-first searching only finds studies whose authors deposited files in these repositories, named them '
             'recognisably and made them public; it does not remove selection or publication bias, and it misses data shared on '
             'request, in supplements or in other repositories.')


def funnel(s):
    """Counts for PRISMA-style reporting. Unknown values are None with a reason, never guessed."""
    setup(s)
    tasks = s.db.rows('SELECT t.*, j.created job_created FROM discovery_tasks t JOIN discovery_jobs j ON j.id=t.job_id ORDER BY j.created,t.position')
    obs = s.db.one('SELECT COUNT(*) n, COUNT(DISTINCT project_id) p FROM discovery_observations WHERE task_id IS NOT NULL')
    legacy_obs = s.db.one('SELECT COUNT(*) n FROM discovery_observations WHERE task_id IS NULL')['n']
    all_projects = s.db.rows('SELECT * FROM sweep_projects')
    rows = [summary_row(s, p) for p in all_projects]
    inspected = [r for r in rows if r['last_inspected']]
    with_files = [r for r in rows if r['match_status'] in MATCHING]
    legacy_with_files = [p for p in all_projects if not p['last_inspected'] and s.db.one('SELECT 1 FROM sweep_files WHERE project_id=?', (p['id'],))]
    documented = [r for r in with_files if s.db.one("SELECT 1 FROM discovery_documents WHERE project_id=? AND status='readable' AND kind NOT LIKE 'article%'", (r['id'],))]
    decisions = {}
    exclusions = {}
    for r in with_files:
        d = (r['decision'] or {}).get('decision') or 'undecided'
        decisions[d] = decisions.get(d, 0) + 1
        if d == 'exclude':
            reason = r['decision']['exclusion_reason'] or 'unspecified'
            exclusions[reason] = exclusions.get(reason, 0) + 1
    relations = s.db.rows("SELECT * FROM project_relations WHERE status='same_dataset'")
    duplicate_projects = len({max(r['a'], r['b']) for r in relations})
    included = [r for r in with_files if (r['decision'] or {}).get('decision') == 'include']
    inc_ids = [r['id'] for r in included]
    papers_included = s.db.rows("SELECT DISTINCT paper_id FROM project_papers WHERE status='confirmed' AND project_id IN (%s)" % ','.join('?' * len(inc_ids)), inc_ids) if inc_ids else []
    studies_mapped = {r['study']['id'] for r in included if r['study']}
    unmapped = [r for r in included if not r['study']]
    unknowns = []
    if any(t['status'] not in ('completed', 'skipped') for t in tasks):
        unknowns.append('Some searches are partial, rate-limited, failed or cancelled; their remaining records were not examined.')
    if any(t['status'] == 'skipped' for t in tasks):
        unknowns.append('Some search strategies were skipped (for example GitHub code search without a token).')
    if legacy_obs:
        unknowns.append(f'{legacy_obs} record(s) from earlier prototype searches have no per-strategy breakdown.')
    not_inspected = [r for r in rows if not r['last_inspected']]
    if not_inspected:
        unknowns.append(f'{len(not_inspected)} project(s) found but not yet inspected; whether they hold EyeLink files is unknown.')
    if unmapped:
        unknowns.append(f'{len(unmapped)} included project(s) are not mapped to a study, so the number of independent studies is not known.')
    summary = {
        'records_examined': obs['n'], 'legacy_records_examined': legacy_obs, 'unique_projects_found': len(rows), 'projects_inspected': len(inspected),
        'projects_with_matching_files': len(with_files), 'legacy_projects_with_files_not_reinspected': len(legacy_with_files),
        'projects_header_confirmed': sum(r['match_status'] == 'eyelink_confirmed' for r in rows),
        'projects_with_study_recording_candidates': sum(r['match_status'] in ('eyelink_confirmed', 'candidate_files') for r in rows),
        'projects_without_study_recordings': sum(r['match_status'] == 'no_study_recordings' for r in rows),
        'projects_with_documentation': len(documented),
        'relevance': {k: sum(r['relevance'] == k for r in with_files) for k in ('likely_relevant', 'needs_review', 'possible_exclusion', 'insufficient_information', None)},
        'publication': {k: sum(r['publication'] == k for r in with_files) for k in ('confirmed', 'proposed', 'none_identified', 'not_found', 'not_searched')},
        'article_text': {k: sum(r['article'] == k for r in with_files) for k in ('readable', 'pdf_only', 'inaccessible', 'not_attempted', 'no_publication', 'not_identified')},
        'decisions': decisions, 'exclusion_reasons': exclusions, 'confirmed_duplicate_projects': duplicate_projects,
        'included_projects': len(included), 'reports_linked_to_included_projects': len(papers_included),
        'independent_studies': len(studies_mapped) if not unmapped else None,
        'independent_studies_mapped_so_far': len(studies_mapped),
    }
    summary['relevance'] = {('not_assessed' if k is None else k): v for k, v in summary['relevance'].items()}
    searches = [{'job_started': t['job_created'], 'source': t['source'], 'strategy': t['kind'], 'query': t['query'], 'label': t['label'], 'status': t['status'],
                 'pages': t['pages'], 'records_examined': t['records'], 'reported_total': t['reported_total'], 'unique_projects': t['projects'],
                 'coverage': t['coverage'], 'error': t['error']} for t in tasks]
    return {'summary': summary, 'searches': searches, 'unknowns': unknowns, 'bias_note': BIAS_NOTE,
            'count_definitions': {'records_examined': 'Search results returned (file cards, records or repositories) across all strategies, including repeats.',
                                  'unique_projects_found': 'Distinct repository projects (one per repository and identifier).',
                                  'reports': 'Distinct publications (papers) linked to projects; one paper can cover several projects and one project several papers.',
                                  'independent_studies': 'Studies mapped by a reviewer; unknown (null) until every included project is mapped.'}}


def prisma_text(f):
    s = f['summary']
    lines = ['ORCHARD REPOSITORY-FIRST SEARCH: COUNTS FOR PRISMA REPORTING', '', 'IDENTIFICATION',
             f"  Records examined across repository searches: {s['records_examined']}" + (f" (+{s['legacy_records_examined']} from earlier prototype searches)" if s['legacy_records_examined'] else ''),
             f"  Unique repository projects found: {s['unique_projects_found']}", '  Searches:']
    for x in f['searches']:
        lines.append(f"    - {x['source']} / {x['strategy']} / {x['query']!r}: {x['status']}, {x['records_examined']} records over {x['pages']} page(s); source reports {x['reported_total'] if x['reported_total'] is not None else 'unknown'}; {x['unique_projects']} projects")
        lines.append(f"      Coverage: {x['coverage']}")
        if x['error']:
            lines.append(f"      Note: {x['error']}")
    lines += ['', 'SCREENING (repository projects)', f"  Inspected (full file listing): {s['projects_inspected']}",
              f"  With .edf/.asc files (names, incl. archive members): {s['projects_with_matching_files']}",
              f"  With EyeLink headers confirmed in a sample: {s['projects_header_confirmed']}",
              f"  .edf/.asc names but no study recordings (not EyeLink, examples/tests or placeholders): {s['projects_without_study_recordings']}",
              f"  With readable documentation: {s['projects_with_documentation']}",
              f"  Automated relevant-context assessment: {s['relevance']}", f"  Publication status: {s['publication']}",
              f"  Article text: {s['article_text']}", f"  Human decisions: {s['decisions']}", f"  Exclusion reasons: {s['exclusion_reasons']}",
              f"  Confirmed duplicate projects: {s['confirmed_duplicate_projects']}", '', 'INCLUDED',
              f"  Repository projects: {s['included_projects']}", f"  Reports (papers) linked to them: {s['reports_linked_to_included_projects']}",
              f"  Independent studies: {s['independent_studies'] if s['independent_studies'] is not None else 'unknown'} (mapped so far: {s['independent_studies_mapped_so_far']})",
              '', 'UNKNOWN OR INCOMPLETE'] + [f'  - {u}' for u in f['unknowns'] or ['None recorded.']] + ['', 'BIAS', '  ' + f['bias_note']]
    return '\n'.join(lines)


EXPORT_TABLES = ('discovery_jobs', 'discovery_tasks', 'discovery_observations', 'sweep_projects', 'sweep_files', 'project_inventories', 'discovery_documents',
                 'discovery_candidates', 'publication_searches', 'publication_decisions', 'project_papers', 'retrieval_attempts', 'screening_configs',
                 'relevance_assessments', 'project_decisions', 'download_batches', 'downloads', 'studies', 'study_links', 'project_relations',
                 'discovery_events', 'sweep_runs', 'sweep_findings', 'sweep_article_reviews')
