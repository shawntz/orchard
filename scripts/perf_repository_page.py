"""Measure the repository discovery pages on a large database, idle and during an active search.

Builds a synthetic workspace (projects, file inventories, documents, assessments, candidates,
observations, decisions) with the same tables the app writes, then:
  1. times the page's API requests and their response sizes while idle;
  2. starts a real search job (real code paths; simulated repository APIs with network latency)
     and repeats the measurements while it runs;
  3. optionally (--browser) times page loads in Chromium.

Usage: python scripts/perf_repository_page.py [--projects 3000] [--browser] [--keep DIR]
"""
import argparse
import json
import random
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from orchard import discovery as D, repositories as R  # noqa: E402
from orchard.db import dump, uid  # noqa: E402
from orchard.server import make_server  # noqa: E402
from orchard.service import Service  # noqa: E402

WORDS = ('pupil size attention working memory task participants trials stimulus luminance baseline eyelink recorded '
         'experiment condition response analysis data folder script behavioural preprocessing').split()


def text(n, rng):
    return ' '.join(rng.choice(WORDS) for _ in range(n))


def build(s, projects, rng):
    """Synthetic records shaped like a long-running workspace."""
    D.setup(s)
    config = D.current_config(s)
    s.save_criteria('Human cognitive task with pupil recordings', 'Non-human')
    criteria = s.db.one('SELECT id FROM criteria')['id']
    now = '2026-10-30T10:00:00'
    with s.db.connect() as c:
        jobs = []
        for j in range(6):
            jid = uid(); jobs.append(jid)
            c.execute('INSERT INTO discovery_jobs(id,status,parameters,created,version,updated,progress,finished) VALUES(?,?,?,?,?,?,?,?)',
                      (jid, 'completed', dump({'sources': ['osf', 'zenodo'], 'phrases': ['pupillometry'], 'extensions': ['asc', 'edf'], 'max_pages': 5, 'max_projects': 100}),
                       f'2026-10-{10 + j:02d}T10:00:00', 2, now, dump({'phase': 'completed'}), now))
            for t in range(9):
                requests = [{'url': 'https://example.org/search?page=%d' % i, 'cursor': i, 'hits': 100, 'reported_total': 5000, 'at': now} for i in range(200)]
                c.execute('INSERT INTO discovery_tasks(id,job_id,position,source,kind,query,label,coverage,parameters,status,pages,records,reported_total,projects,requests,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (uid(), jid, t, 'osf', 'metadata', 'q%d' % t, 'Search %d' % t, 'coverage ' * 30, '{}', 'completed', 5, 400, 5000, 300, dump(requests), now))
        for i in range(projects):
            pid = uid()
            inspected = i % 3 == 0
            c.execute('INSERT INTO sweep_projects(id,repository,external_id,url,title,description,first_seen,match_status,last_inspected,inspection_status,eyelink_files,confirmed_files,creators,keywords,related,inspection_steps,inventory_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (pid, 'osf', 'p%05d' % i, 'https://osf.io/p%05d/' % i, 'Project %d %s' % (i, text(8, rng)), text(60, rng), now,
                       'eyelink_confirmed' if inspected else 'not_inspected', now if inspected else None, 'completed' if inspected else None,
                       40 if inspected else None, 3 if inspected else None, dump(['A Person']), dump(['pupil']), '[]', dump([{'step': 'inventory', 'status': 'ok', 'detail': 'x' * 200, 'at': now}] * 6), 'inv' + pid if inspected else None))
            for jid in rng.sample(jobs, 2):
                c.execute('INSERT INTO discovery_observations(id,job_id,repository,external_id,title,has_files,created,project_id,matched_files,evidence) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (uid(), jid, 'osf', 'p%05d' % i, 'Project', 1, now, pid, dump(['raw/sub-01.asc']), '{}'))
            if not inspected:
                continue
            for f in range(120):
                c.execute('INSERT INTO sweep_files(id,project_id,path,url,size,extension,role,role_reason,header,header_details,download,checksum,probe_url,inventory_id,listed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (uid(), pid, 'Experiment %d/Pupil_Data/pp%03d.asc' % (f % 3, f), 'https://osf.io/download/x%d' % f, 80_000_000, '.asc', 'recording', 'reason ' * 10,
                           'eyelink' if f < 3 else None, dump({'pupil': 'AREA', 'sample_rate_hz': 1000.0}) if f < 3 else '{}', 'public', 'sha256:' + 'a' * 64, 'https://osf.io/download/x%d' % f, 'inv' + pid, now))
            for kind, words in (('readme', 3000), ('wiki', 800)):
                body = text(words, rng)
                c.execute('INSERT INTO discovery_documents(id,project_id,kind,url,title,content,sha256,created,status,method,words) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (uid(), pid, kind, 'https://osf.io/x', kind, body, uid(), now, 'readable', 'api', words))
            for a in range(3):
                evidence = [{'kind': 'pupil_and_task', 'passage': text(60, rng), 'concepts': {'measurement': {'Pupil measurement': ['pupil size']}}, 'source_title': 'README'}] * 40
                c.execute('INSERT INTO relevance_assessments VALUES(?,?,?,?,?,?,?,?,?,?,?)', (uid(), pid, config['id'], 'likely_relevant', 'reason', '[]', '{}', dump(evidence), '[]', '{}', now))
            for k in range(5):
                c.execute('INSERT INTO discovery_candidates(id,project_id,doi,title,score,origin,raw,created,authors,year,kind,strength,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (uid(), pid, '10.5555/%s.%d' % (pid[:6], k), text(10, rng), None, 'Crossref search', dump({'x': text(400, rng)}), now, '[]', '2024', 'article', 'moderate', '[]'))
            c.execute('INSERT INTO publication_searches VALUES(?,?,?,?,?,?,?)', (uid(), pid, now, dump([{'source': 'Crossref', 'url': 'u', 'results': 10}] * 4), dump([{'title': text(12, rng), 'reason': 'r'}] * 20), '[]', 5))
            if i % 9 == 0:
                c.execute('INSERT INTO project_decisions(id,project_id,version,decision,reason,evidence,reviewer,criteria_id,criteria,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (uid(), pid, 1, 'include', 'reason', '[]', 'Reviewer', criteria, dump({'criteria': {}}), now))


def timed_get(base, path):
    t = time.perf_counter()
    with urllib.request.urlopen(base + path, timeout=600) as r:
        body = r.read()
    return time.perf_counter() - t, len(body), body


def measure(base, project_id, label, repeats=3):
    paths = ['/api/discovery', '/api/discovery/projects?match=with_files', '/api/discovery/project/' + project_id, '/api/discovery/funnel', '/discovery.js']
    out = {}
    for path in paths:
        times, size = [], 0
        for _ in range(repeats):
            dt, size, _ = timed_get(base, path)
            times.append(dt)
        out[path] = (statistics.median(times), max(times), size)
        print(f'  {label:<7} {path[:48]:<48} median {statistics.median(times):7.3f}s  max {max(times):7.3f}s  {size / 1024:9.1f} KB', flush=True)
    return out


def fake_world(latency, n):
    """Repository APIs for an active search: n OSF projects found by metadata, each with files and a README."""
    from fakeweb import FakeWeb
    from repository_fixtures import ASC, osf_project, trove_page, project_card
    web = FakeWeb()
    for g in range(n):
        osf_project(web, 'live%d' % g, 'Live project %d pupil attention' % g, 'desc', 'A Person',
                    {'': [('README.md', ('Pupil size during an attention task. ' * 200).encode())], 'raw/': [('raw/s%d.asc' % k, ASC) for k in range(60)]})
    cards = [project_card('live%d' % g, 'Live project %d' % g) for g in range(n)]
    web.add(r'^https://share\.osf\.io/trove/index-card-search', lambda u, h: trove_page(cards, n) if 'File' not in u else trove_page([], 0))
    web.add(r'^https://(api\.crossref\.org|api\.openalex\.org|www\.ebi\.ac\.uk)/', {'message': {'items': []}, 'results': [], 'meta': {'count': 0}, 'hitCount': 0, 'resultList': {'result': []}})
    real_get = web.get

    def slow_get(url, **kw):
        time.sleep(latency)  # network round trip; releases the GIL like a real request
        return real_get(url, **kw)
    web.get = slow_get
    return web


def browser_load(base, label):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={'width': 1400, 'height': 900})
        for route, ready in (('repository-sweep', '.d-job'), ('repository-projects', 'table.d-projects')):
            api = {}
            page.on('requestfinished', lambda r: api.__setitem__(r.url.split('/api/')[-1][:40], r.timing['responseEnd']) if '/api/' in r.url else None)
            t = time.perf_counter()
            page.goto(base + '/#' + route)
            page.wait_for_selector(ready, timeout=600_000)
            total = time.perf_counter() - t
            slowest = max(api.values()) / 1000 if api else 0
            print(f'  {label:<7} browser #{route:<20} visible after {total:7.3f}s (slowest API response end {slowest:6.3f}s)', flush=True)
            page.goto('about:blank')
        b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--projects', type=int, default=3000)
    ap.add_argument('--browser', action='store_true')
    ap.add_argument('--keep')
    args = ap.parse_args()
    tmp = args.keep or tempfile.mkdtemp(prefix='orchard-perf-')
    rng = random.Random(1)
    path = Path(tmp) / 'orchard.sqlite3'
    fresh = not path.exists()
    web = fake_world(0.05, 400)
    s = Service(path, transport=web)
    s.repo_client = R.Client(web, sleeper=time.sleep, intervals={'api.osf.io': 0.05})
    if fresh:
        t = time.perf_counter()
        build(s, args.projects, rng)
        print(f'built {args.projects} projects in {time.perf_counter() - t:.1f}s; database {path.stat().st_size / 1e6:.0f} MB', flush=True)
    project = s.db.one("SELECT id FROM sweep_projects WHERE last_inspected IS NOT NULL LIMIT 1")['id']
    server = make_server(s, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        measure(base, project, 'idle')
        if args.browser:
            browser_load(base, 'idle')
        job = D.start(s, {'sources': ['osf'], 'phrases': ['pupillometry'], 'extensions': ['asc'], 'max_pages': 5, 'max_projects': 400})
        time.sleep(8)
        row = s.db.one('SELECT status,progress FROM discovery_jobs WHERE id=?', (job['id'],))
        print('active search:', row['status'], row['progress'][:120], flush=True)
        measure(base, project, 'active')
        if args.browser:
            browser_load(base, 'active')
        lat = []
        for _ in range(20):
            t = time.perf_counter()
            s.db.one('SELECT COUNT(*) n FROM sweep_projects')
            lat.append(time.perf_counter() - t)
        print(f'  active  direct database read (20x)  median {statistics.median(lat) * 1000:.1f} ms  max {max(lat) * 1000:.1f} ms', flush=True)
        row = s.db.one('SELECT status,progress FROM discovery_jobs WHERE id=?', (job['id'],))
        print('search still:', row['status'], row['progress'][:120], flush=True)
        D.cancel(s, job['id'])
    finally:
        server.shutdown()
        server.server_close()
        s.close()
        print('data kept in', tmp)


if __name__ == '__main__':
    main()
