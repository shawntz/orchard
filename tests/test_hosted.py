"""Hosted mode: proxy-authenticated usernames are recorded on every audit event."""
import json, sqlite3, tempfile, threading, time, unittest, urllib.request, urllib.error
from pathlib import Path
from orchard.db import ACTOR, Database
from orchard import discovery
from orchard.service import Service
from orchard.providers import Page
from orchard.server import make_server
from tests.test_workflow import MockProvider, MockPdfSource, record

SITE='https://orchard.example.org'


class Hosted(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'orchard.sqlite3'
        self.provider=MockProvider(); self.s=Service(self.path,self.provider,pdf_source=MockPdfSource())
        self.server=make_server(self.s,0,SITE,'X-Remote-User')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}/api/'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.s.close(); self.tmp.cleanup()

    def call(self,path,body=None,user='alice',headers=None):
        h={'Host':'orchard.example.org','Origin':SITE,**({'X-Remote-User':user} if user else {}),**(headers or {})}
        if body is not None: h['Content-Type']='application/json'
        req=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers=h)
        with urllib.request.urlopen(req) as r: return json.load(r)

    def status(self,*args,**kwargs):
        with self.assertRaises(urllib.error.HTTPError) as ctx: self.call(*args,**kwargs)
        return ctx.exception.code

    def test_requests_require_proxy_username(self):
        self.assertEqual(self.status('overview',user=None),401)
        self.assertEqual(self.status('overview',user=''),401)
        self.assertEqual(self.call('session'),{'user':'alice','authenticated':True})

    def test_public_host_and_origin_only(self):
        self.assertEqual(self.status('overview',headers={'Host':'attacker.example'}),403)
        self.assertEqual(self.status('documents',{'name':'x','text':'pupil'},headers={'Origin':'https://evil.example'}),403)

    def test_actions_attributed_by_username(self):
        doc=self.call('documents',{'name':'terms','text':'pupillometry\nmemory'},user='alice')
        self.call('criteria',{'inclusion':'Pupil study','exclusion':'None'},user='bob')
        rows={r['event']:r['actor'] for r in self.call('audit')}
        self.assertEqual(rows['document_imported'],'alice')
        self.assertEqual(rows['criteria_version_created'],'bob')
        self.assertEqual([r['event'] for r in self.call('audit?q=bob')],['criteria_version_created'])
        self.assertTrue(doc['id'])

    def test_screening_reviewer_is_signed_in_user(self):
        self.s.import_records('synthetic.json',[record()]); p=self.s.listing()['items'][0]
        self.call('criteria',{'inclusion':'Pupil study','exclusion':'None'})
        paper=self.call('screen',{'id':p['id'],'stage':'title_abstract','decision':'include','reviewer':'someone else','revision':p['revision']},user='carol')
        self.assertEqual(paper['decisions'][0]['reviewer'],'carol')

    def test_background_job_events_carry_requester(self):
        self.provider.pages=[Page([record()],None,1)]
        q=self.call('queries',{'label':'t','query':'pupil AND memory','approved':True},user='dana')
        run=self.call('search',{'query_id':q['id']},user='erin')['run']
        for _ in range(100):
            if self.call(f'runs')[0]['status'] not in ('queued','running'): break
            time.sleep(0.05)
        events={r['event']:r['actor'] for r in self.call('audit') if r['entity_id']==run['id']}
        self.assertEqual(events['search_queued'],'erin')
        self.assertEqual(events['search_started'],'erin')
        self.assertEqual(events['search_completed'],'erin')
        # Any signed-in user can watch the same job.
        self.assertEqual(self.call('runs',user='frank')[0]['status'],'completed')


class Configuration(unittest.TestCase):
    def test_local_mode_unattributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Service(Path(tmp)/'orchard.sqlite3',MockProvider(),pdf_source=MockPdfSource())
            try:
                s.save_criteria('a','b')
                self.assertIsNone(s.db.one("SELECT actor FROM audit WHERE event='criteria_version_created'")['actor'])
            finally: s.close()

    def test_invalid_settings_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Service(Path(tmp)/'orchard.sqlite3',MockProvider(),pdf_source=MockPdfSource())
            try:
                for url in ('orchard.example.org','https://orchard.example.org/app','ftp://x'):
                    with self.assertRaises(ValueError): make_server(s,0,url,'X-Remote-User')
                with self.assertRaises(ValueError): make_server(s,0,SITE,'X-Remote User')
            finally: s.close()

    def test_existing_database_gains_actor_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'orchard.sqlite3'
            c=sqlite3.connect(path)
            c.executescript("CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, entity_id TEXT, detail TEXT NOT NULL, created TEXT NOT NULL);"
                            "INSERT INTO audit(event,entity_id,detail,created) VALUES('old_event',NULL,'{}','2025-01-01');")
            c.commit(); c.close()
            db=Database(path)
            self.assertIsNone(db.one("SELECT actor FROM audit WHERE event='old_event'")['actor'])
            self.assertIn(3,[r['version'] for r in db.rows('SELECT version FROM schema_version')])


    def test_database_from_either_version_3_build_gains_the_other_columns(self):
        # Hosted mode and multi-provider search each shipped a "version 3"; both halves must apply.
        for existing in ("CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, entity_id TEXT, detail TEXT NOT NULL, created TEXT NOT NULL, actor TEXT);",
                         "CREATE TABLE runs(id TEXT PRIMARY KEY, query_id TEXT, query TEXT NOT NULL, provider TEXT NOT NULL, fingerprint TEXT NOT NULL, parameters TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, finished TEXT, cursor TEXT, retrieved INTEGER NOT NULL DEFAULT 0, total INTEGER, error TEXT, refresh_of TEXT, batch_id TEXT);"):
            with self.subTest(existing=existing[:30]), tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'orchard.sqlite3'
                c=sqlite3.connect(path); c.executescript(existing+'CREATE TABLE schema_version(version INTEGER PRIMARY KEY); INSERT INTO schema_version VALUES(3);'); c.commit(); c.close()
                db=Database(path)
                self.assertIn('actor',[r['name'] for r in db.rows('PRAGMA table_info(audit)')])
                self.assertIn('batch_id',[r['name'] for r in db.rows('PRAGMA table_info(runs)')])
                self.assertTrue(db.one("SELECT name FROM sqlite_master WHERE name='files'"))


class RepositoryDiscoveryHosted(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Path(self.tmp.name)/'orchard.sqlite3',MockProvider(),pdf_source=MockPdfSource())

    def tearDown(self):
        if hasattr(self.s,'discovery_pool'): self.s.discovery_pool.shutdown(wait=True)
        self.s.close(); self.tmp.cleanup()

    def test_signed_in_user_is_the_repository_reviewer(self):
        token=ACTOR.set('carol')
        try: self.assertEqual(discovery.reviewer_name('someone else'),'carol')
        finally: ACTOR.reset(token)
        self.assertEqual(discovery.reviewer_name(' Local researcher '),'Local researcher')
        with self.assertRaises(ValueError): discovery.reviewer_name('')

    def test_discovery_background_work_carries_requester(self):
        token=ACTOR.set('erin')
        try: future=discovery.submit(self.s,ACTOR.get)
        finally: ACTOR.reset(token)
        self.assertEqual(future.result(timeout=10),'erin')

if __name__=='__main__': unittest.main()
