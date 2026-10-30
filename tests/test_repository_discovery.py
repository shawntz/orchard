"""Repository discovery: search strategies, pagination, cancellation, resume, rate limits and partial failures.

Offline: every API response comes from tests/repository_fixtures.py.
"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakeweb import Reply
from repository_fixtures import build_web, TITLE_CTD34
from orchard import discovery as D, repositories as R
from orchard.downloads import Downloader
from orchard.providers import Crossref, EuropePMC
from orchard.service import Service

TOKENS = ('ORCHARD_GITHUB_TOKEN', 'GITHUB_TOKEN', 'ORCHARD_ZENODO_TOKEN', 'ORCHARD_DRYAD_TOKEN', 'ORCHARD_CONTACT_EMAIL')


class Response:
    def __init__(self, reply, offset=0):
        body = reply.body[offset:] if offset else reply.body
        self.status = 206 if offset else 200
        self.headers, self.stream = reply.headers, io.BytesIO(body)

    def read(self, n=-1):
        return self.stream.read(n)

    def close(self):
        pass


def opener(web):
    def open_url(url, headers):
        web.calls.append((url, headers.get('Range')))
        reply = web.match(url, headers)
        if reply is None:
            raise OSError('404 ' + url)
        offset = int(headers['Range'].split('=')[1].rstrip('-')) if headers.get('Range') and 'ignore-range' not in reply.headers else 0
        return Response(reply, offset)
    return open_url


def make_service(tmp, web=None):
    web = web or build_web()
    s = Service(Path(tmp) / 'orchard.sqlite3', provider=EuropePMC(web), transport=web, locators=[Crossref(web)])
    s.repo_client = R.Client(web, sleeper=lambda t: None)
    s.sleeper = lambda t: None
    s.downloader = Downloader(opener(web))
    return s, web


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {k: '' for k in TOKENS})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.s, self.web = make_service(self.tmp.name)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()
        self.env.stop()

    def run_search(self, **kw):
        params = {'sources': ['osf', 'zenodo', 'dryad', 'github'], 'phrases': ['pupillometry'], 'max_pages': 5, 'max_projects': 50, **kw}
        return D.start(self.s, params, background=False)

    def project(self, repository, external_id):
        return self.s.db.one('SELECT * FROM sweep_projects WHERE repository=? AND external_id=?', (repository, external_id))


class Strategies(Base):
    def test_plan_states_what_each_source_searches(self):
        plans = {k: a.plan(['pupillometry']) for k, a in D.adapters(self.s).items()}
        self.assertEqual([p.kind for p in plans['osf']], ['file_index', 'file_index', 'metadata'])
        self.assertEqual(plans['zenodo'][0].query, 'filetype:asc OR filetype:edf')
        self.assertTrue(all(p.kind == 'metadata' for p in plans['dryad']))
        self.assertIn('no file-name search', plans['dryad'][0].coverage)
        code = [p for p in plans['github'] if p.kind == 'code_index']
        self.assertTrue(code and all(p.skipped and 'token' in p.skipped for p in code))
        self.assertIn('Binary .edf files are never indexed', code[0].coverage)

    def test_metadata_only_hits_are_not_counted_as_file_matches(self):
        job = self.run_search(max_projects=0)
        hits = self.s.db.rows("SELECT * FROM discovery_observations WHERE external_id='ctd34'")
        self.assertTrue(hits and all(h['has_files'] == 0 for h in hits))
        self.assertEqual(self.project('osf', 'ctd34')['match_status'], 'not_inspected')
        self.assertEqual(job['project_status'].get('not_inspected'), job['unique_projects'])


class Jobs(Base):
    def test_full_search_counts_pages_and_skips_honestly(self):
        job = self.run_search()
        tasks = {(t['source'], t['kind'], t['query']): t for t in job['tasks']}
        edf = tasks[('osf', 'file_index', 'edf')]
        self.assertEqual((edf['status'], edf['pages'], edf['records'], edf['reported_total']), ('completed', 2, 2, 2))
        zen = tasks[('zenodo', 'file_index', 'filetype:asc OR filetype:edf')]
        self.assertEqual((zen['pages'], zen['records'], zen['reported_total']), (2, 2, 2))
        self.assertEqual(tasks[('github', 'code_index', 'EYELINK extension:asc')]['status'], 'skipped')
        self.assertEqual(job['records_examined'], sum(t['records'] for t in job['tasks']))
        # Counts come from stored observations, never from fixed numbers.
        self.assertEqual(job['records_examined'], self.s.db.one('SELECT COUNT(*) n FROM discovery_observations WHERE job_id=?', (job['id'],))['n'])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['unique_projects'], 6)

    def test_page_cap_leaves_cursor_and_resume_continues(self):
        job = self.run_search(sources=['osf'], max_pages=1, max_projects=0)
        task = next(t for t in job['tasks'] if t['query'] == 'edf')
        self.assertEqual((task['status'], task['pages']), ('partial', 1))
        self.assertIn('resume', task['error'])
        self.assertEqual(job['status'], 'partial')
        saved = self.s.db.one('SELECT cursor FROM discovery_tasks WHERE id=?', (task['id'],))['cursor']
        self.assertIn('page%5Bcursor%5D', json.loads(saved))
        job = D.resume(self.s, job['id'], background=False)
        task = next(t for t in job['tasks'] if t['query'] == 'edf')
        self.assertEqual((task['status'], task['pages'], task['records']), ('completed', 2, 2))
        self.assertEqual(len(self.web.requested('page%5Bcursor%5D=abc')), 1)  # the cursor page was fetched once

    def test_one_source_failure_does_not_stop_others(self):
        self.web.add(r'^https://zenodo\.org/api/records\?', Reply({'error': 'boom'}, 500))
        job = self.run_search(max_projects=0)
        failed = [t for t in job['tasks'] if t['source'] == 'zenodo']
        self.assertTrue(all(t['status'] == 'failed' and 'HTTP Error 500' in t['error'] for t in failed))
        self.assertTrue(all(t['status'] in ('completed', 'skipped') for t in job['tasks'] if t['source'] != 'zenodo'))
        self.assertEqual(job['status'], 'partial')
        self.assertIsNotNone(self.project('dryad', 'doi:10.5061/dryad.abc123'))
        funnel = D.funnel(self.s)
        self.assertTrue(any('partial, rate-limited, failed' in u for u in funnel['unknowns']))

    def test_malformed_response_is_a_failure_not_zero_results(self):
        self.web.add(r'^https://datadryad\.org/api/v2/search', {'unexpected': True})
        job = self.run_search(sources=['dryad'], max_projects=0)
        self.assertEqual(job['tasks'][0]['status'], 'failed')
        self.assertIn('Unexpected Dryad search response', job['tasks'][0]['error'])

    def test_long_rate_limit_pauses_task_for_resume(self):
        self.web.add(r'^https://datadryad\.org/api/v2/search', Reply({'message': 'slow down'}, 429, {'Retry-After': '3600'}))
        job = self.run_search(sources=['dryad'], max_projects=0)
        self.assertEqual(job['tasks'][0]['status'], 'rate_limited')
        self.assertTrue(job['tasks'][0]['retry_after'])
        self.web.routes.pop(0)
        job = D.resume(self.s, job['id'], background=False)
        self.assertEqual(job['tasks'][0]['status'], 'completed')

    def test_short_rate_limit_waits_and_continues(self):
        state = {'n': 0}

        def flaky(url, headers):
            state['n'] += 1
            return Reply({'message': 'wait'}, 429, {'Retry-After': '2'}) if state['n'] == 1 else {'total': 0, '_embedded': {'stash:datasets': []}}
        self.web.add(r'^https://datadryad\.org/api/v2/search', flaky)
        waits = []
        self.s.sleeper = waits.append
        job = self.run_search(sources=['dryad'], max_projects=0)
        self.assertEqual(job['tasks'][0]['status'], 'completed')
        self.assertIn(2.0, waits)

    def test_cancel_stops_between_pages_and_keeps_progress(self):
        calls = {'n': 0}
        original = D.record_page

        def record_then_cancel(s, job, task, page, cursor):
            original(s, job, task, page, cursor)
            calls['n'] += 1
            if calls['n'] == 1:
                D.cancel(s, job)
        with mock.patch.object(D, 'record_page', record_then_cancel):
            job = self.run_search(sources=['osf'])
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(sum(t['pages'] for t in job['tasks']), 1)
        self.assertTrue(any(t['status'] == 'cancelled' for t in job['tasks']))
        job = D.resume(self.s, job['id'], background=False)
        self.assertEqual(job['status'], 'completed')

    def test_restart_marks_running_jobs_interrupted_and_resumable(self):
        job = self.run_search(sources=['dryad'], max_projects=0)
        with self.s.db.connect() as c:
            c.execute("UPDATE discovery_jobs SET status='running' WHERE id=?", (job['id'],))
            c.execute("UPDATE discovery_tasks SET status='running' WHERE job_id=?", (job['id'],))
        self.s.close()
        self.s, _ = make_service(self.tmp.name, self.web)
        row = D.job_detail(self.s, job['id'])
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['tasks'][0]['status'], 'interrupted')
        self.assertEqual(D.resume(self.s, job['id'], background=False)['status'], 'completed')

    def test_parameters_and_requests_are_preserved(self):
        job = self.run_search(sources=['zenodo'], phrases=['pupil size'], max_pages=3, max_projects=0)
        stored = self.s.db.one('SELECT parameters FROM discovery_jobs WHERE id=?', (job['id'],))
        self.assertEqual(json.loads(stored['parameters'])['phrases'], ['pupil size'])
        log = json.loads(self.s.db.one('SELECT requests FROM discovery_tasks WHERE job_id=? AND kind=?', (job['id'], 'metadata'))['requests'])
        self.assertTrue(log and 'q=pupil+size' in log[0]['url'] and log[0]['reported_total'] == 1)

    def test_invalid_parameters_rejected(self):
        for bad in ({'sources': ['figshare']}, {'phrases': []}, {'extensions': ['csv']}, {'max_pages': 0}):
            with self.assertRaises(ValueError):
                D.start(self.s, {'sources': ['osf'], 'phrases': ['x'], **bad}, background=False)


class Client(unittest.TestCase):
    def test_paces_requests_per_host(self):
        clock = {'t': 0.0}
        sleeps = []

        class Web:
            def get(self, url, **kw):
                return b'{}', url, {}
        c = R.Client(Web(), intervals={'a.example': 2.0, 'b.example': 0}, sleeper=lambda t: (sleeps.append(t), clock.__setitem__('t', clock['t'] + t)), clock=lambda: clock['t'])
        c.get('https://a.example/1'); c.get('https://a.example/2'); c.get('https://b.example/1')
        self.assertEqual(sleeps, [2.0])

    def test_github_exhausted_quota_raises_rate_limited(self):
        from fakeweb import FakeWeb
        web = FakeWeb({'.': Reply({'message': 'API rate limit exceeded'}, 403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '9999999999'})})
        with self.assertRaises(R.RateLimited) as ctx:
            R.Client(web, sleeper=lambda t: None).get('https://api.github.com/search/repositories?q=x')
        self.assertGreater(ctx.exception.retry_after, 60)

    def test_plain_403_is_not_a_rate_limit(self):
        from fakeweb import FakeWeb
        from urllib.error import HTTPError
        web = FakeWeb({'.': Reply(b'forbidden', 403)})
        with self.assertRaises(HTTPError):
            R.Client(web, sleeper=lambda t: None).get('https://example.org/x')


if __name__ == '__main__':
    unittest.main()
