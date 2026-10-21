"""Synthetic fixtures only; these are never loaded into the live workspace."""
import io, json, sqlite3, tempfile, threading, unittest, urllib.request, urllib.error, zipfile
from pathlib import Path
from orchard.service import Service, Conflict, doi_normalize
from orchard.terms import build_query
from orchard.providers import EuropePMC, Page, public_url
from orchard.server import make_server


def record(id='1',doi='10.1234/test',title='Synthetic pupil memory experiment',**extra):
    return {'id':id,'source':'MED','pmid':id,'title':title,'doi':doi,'authorString':'Researcher A','pubYear':'2024','abstractText':'Synthetic fixture for testing only.',**extra}

class MockProvider:
    name='europepmc'; version='test-core'
    def __init__(self): self.pages=[Page([],None,0)]; self.calls=[]
    def page(self,query,cursor,size,callback):
        self.calls.append((query,cursor,size)); item=self.pages.pop(0)
        if isinstance(item,Exception): raise item
        return item

class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'orchard.sqlite3'
        self.provider=MockProvider(); self.s=Service(self.path,self.provider)
    def tearDown(self): self.s.close(); self.tmp.cleanup()
    def run_records(self,records): return self.s.import_records('synthetic-test.json',records)
    def paper(self,r=None):
        self.run_records([r or record()]); return self.s.listing()['items'][0]
    def query(self,text='pupillometry AND memory'): return self.s.save_query('Test',text,approved=True)
    def criteria(self): return self.s.save_criteria('Human cognitive task with pupil recording','No relevant pupil result')
    def test_valid_import_keeps_verbatim_and_hash(self):
        text='Memory\n\tWorking memory; working memory; short-term memory\n'
        d=self.s.document(self.s.import_document('terms.txt',text)['id'])
        self.assertEqual(d['content'],text); self.assertEqual(len(d['sha256']),64)
        terms=d['concepts'][0]['terms']; self.assertEqual(terms[0]['original'],'Working memory')
        self.assertEqual(terms[0]['normalized'],terms[1]['normalized']); self.assertEqual(len(terms),3)
        self.assertTrue(self.s.import_document('renamed.txt',text)['reused'])
    def test_empty_and_invalid_import(self):
        for text in ('','   ','\x00','x'*2_000_001):
            with self.assertRaises(ValueError): self.s.import_document('bad.txt',text)
    def test_query_deduplicates_only_exact_normalized_terms(self):
        self.assertEqual(build_query([['Working memory','working memory','short-term memory'],['pupillometry']]), '("Working memory" OR "short-term memory") AND (pupillometry)')
    def test_generated_plan_reproducible_with_provenance(self):
        doc=self.s.document(self.s.import_document('t','Pupil\n\tpupillometry; pupil size\nMemory\n\tmemory; retrieval')['id'])
        ids=[[t['id'] for t in c['terms']] for c in doc['concepts']]
        a=self.s.generate_query(ids,doc['id'],'a'); b=self.s.generate_query(ids,doc['id'],'b')
        self.assertEqual(a['query'],b['query']); self.assertEqual(a['term_ids'],b['term_ids']); self.assertFalse(a['approved'])
    def test_search_requires_approval(self):
        q=self.s.save_query('draft','pupil')
        with self.assertRaises(ValueError): self.s.search(q['id'],background=False)
    def test_identical_search_and_intentional_refresh(self):
        self.provider.pages=[Page([],None,0),Page([],None,0)]
        q=self.query(); a=self.s.search(q['id'],background=False)['run']
        with self.assertRaises(Conflict): self.s.search(q['id'],background=False)
        b=self.s.search(q['id'],action='reuse',background=False)
        self.assertTrue(b['reused']); self.assertEqual(b['run']['id'],a['id']); self.assertEqual(len(self.provider.calls),1)
        fresh=self.s.search(q['id'],action='refresh',background=False)['run']
        self.assertNotEqual(a['id'],fresh['id']); self.assertEqual(fresh['refresh_of'],a['id'])
    def test_quoted_spacing_not_collapsed(self):
        self.provider.pages=[Page([],None,0),Page([],None,0)]
        a=self.s.search(self.query('"pupil  size"')['id'],background=False)['run']
        b=self.s.search(self.query('"pupil size"')['id'],background=False)['run']
        self.assertNotEqual(a['fingerprint'],b['fingerprint'])
    def test_failed_search_retained(self):
        self.provider.pages=[OSError('network down')]
        r=self.s.search(self.query()['id'],background=False)['run']; self.assertEqual(r['status'],'failed'); self.assertIn('network down',r['error'])
    def test_partial_page_failure_retains_results(self):
        self.provider.pages=[Page([record()],'next',2),OSError('timeout')]
        r=self.s.search(self.query()['id'],background=False)['run']; self.assertEqual((r['status'],r['retrieved'],r['cursor']),('partial',1,'next'))
    def test_cap_partial_and_resume_checkpoint(self):
        self.provider.pages=[Page([record()],'next',2),Page([record('2','10.1234/other','Different synthetic report')],None,2)]
        r=self.s.search(self.query()['id'],max_records=1,background=False)['run']
        self.assertEqual(r['status'],'partial'); self.s.execute(r['id'])
        final=self.s.require('runs',r['id']); self.assertEqual((final['status'],final['retrieved']),('completed',2)); self.assertEqual(self.provider.calls[1][1],'next')
    def test_pagination_missing_is_not_completed(self):
        self.provider.pages=[Page([record()],None,20)]
        r=self.s.search(self.query()['id'],background=False)['run']; self.assertEqual(r['status'],'partial')
    def test_exact_doi_dedup_all_discoveries(self):
        self.run_records([record()]); self.run_records([record('2','https://doi.org/10.1234/TEST',source='PMC')])
        self.assertEqual(self.s.listing()['total'],1)
        p=self.s.paper(self.s.listing()['items'][0]['id']); self.assertEqual(len(p['sources']),2); self.assertEqual(len(p['discoveries']),2)
    def test_identifier_without_doi_and_missing_metadata(self):
        self.run_records([{'source':'MED','id':'88','pmid':'88'}]); p=self.s.listing()['items'][0]
        self.assertIsNone(p['title']); self.assertIsNone(p['doi']); self.assertIsNone(p['year'])
        self.run_records([{'source':'MED','id':'88','pmid':'88','title':'Now supplied'}]); self.assertEqual(self.s.listing()['total'],1)
    def test_malformed_record_partial_not_fake_success(self):
        r=self.run_records([record(),{'title':'No source id'}]); self.assertEqual(r['status'],'partial'); self.assertEqual(r['retrieved'],1)
    def test_ambiguous_titles_not_merged(self):
        self.run_records([record(),record('2','10.1234/different')]); self.assertEqual(self.s.listing()['total'],2)
        self.assertEqual(self.s.db.rows('SELECT * FROM duplicates')[0]['status'],'pending')
    def test_merge_preserves_screening_and_raw_records(self):
        self.run_records([record(),record('2','10.1234/different')]); self.criteria()
        for p in self.s.listing()['items']: self.s.screen(p['id'],'title_abstract','uncertain')
        d=self.s.db.rows('SELECT * FROM duplicates')[0]; self.s.resolve_duplicate(d['id'],'merge','Same report, researcher verified')
        self.assertEqual(self.s.listing()['total'],1)
        p=self.s.paper(d['a']); self.assertEqual(len(p['sources']),2); self.assertEqual(len(p['decisions']),2); self.assertEqual(len(p['discoveries']),2)
    def test_related_preprint_keeps_records(self):
        self.run_records([record(),record('2','10.1234/preprint',pubTypeList={'pubType':['Preprint']})])
        d=self.s.db.rows('SELECT * FROM duplicates')[0]; self.s.resolve_duplicate(d['id'],'related','Preprint and journal report')
        self.assertEqual(self.s.listing()['total'],2)
    def test_repeated_ingestion_idempotent(self):
        r=self.run_records([record(),record()]); self.assertEqual(r['retrieved'],1); self.assertEqual(len(self.s.db.rows('SELECT * FROM source_records')),1)
    def test_decision_revisions_preserve_criteria(self):
        p=self.paper(); c=self.criteria(); p=self.s.screen(p['id'],'title_abstract','uncertain')
        self.s.save_criteria('Revised criteria','Revised exclusions'); self.s.screen(p['id'],'title_abstract','exclude',reason='Wrong task',revision=p['revision'])
        ds=self.s.paper(p['id'])['decisions']; self.assertEqual(len(ds),2); self.assertEqual(ds[1]['criteria_id'],c['id']); self.assertNotEqual(ds[0]['criteria_id'],c['id'])
    def test_exclusion_requires_reason_and_criteria(self):
        p=self.paper()
        with self.assertRaises(ValueError): self.s.screen(p['id'],'title_abstract','include')
        self.criteria()
        with self.assertRaises(ValueError): self.s.screen(p['id'],'fulltext','exclude')
    def test_stale_screening_write_rejected(self):
        p=self.paper(); self.criteria(); self.s.screen(p['id'],'title_abstract','include')
        with self.assertRaises(Conflict): self.s.screen(p['id'],'title_abstract','exclude',reason='Stale',revision=0)
    def test_inaccessible_not_exclusion(self):
        p=self.paper(); self.criteria(); self.s.screen(p['id'],'fulltext','inaccessible',notes='No access')
        self.assertEqual(self.s.paper(p['id'])['decisions'][0]['decision'],'inaccessible')
    def test_multiple_contextual_data_links(self):
        p=self.paper(); self.s.import_fulltext(p['id'],'Data are available at https://osf.io/test/.\nAnalysis code: https://github.com/example/project\nSee unrelated https://example.com\nData available upon reasonable request.','authorized test text')
        resources=self.s.paper(p['id'])['resources']; kinds=[r['kind'] for r in resources]
        self.assertIn('dataset',kinds); self.assertIn('code',kinds); self.assertIn('data_statement',kinds)
        self.assertFalse(any(r['url']=='https://example.com' for r in resources)); self.assertTrue(all(r['origin'] for r in resources))
    def test_link_found_not_access_verified(self):
        p=self.paper(record(abstractText='Data: https://osf.io/test/')); r=next(r for r in self.s.paper(p['id'])['resources'] if r['kind']=='dataset')
        self.assertEqual(r['status'],'found_unverified'); self.assertIsNone(r['verified'])
    def test_url_failure_recorded(self):
        p=self.paper(); r=self.s.paper(p['id'])['resources'][0]
        self.provider.transport=type('T',(),{'get':lambda *_a,**_k:(_ for _ in ()).throw(OSError('blocked'))})()
        checked=self.s.resource_action(r['id'],'verify'); self.assertEqual(checked['status'],'access_failed'); self.assertIn('blocked',checked['access_note'])
    def test_export_history_and_csv_formula_guard(self):
        p=self.paper(record(title='=HYPERLINK("bad")')); self.criteria(); self.s.screen(p['id'],'title_abstract','include')
        archive=zipfile.ZipFile(io.BytesIO(self.s.export_zip())); data=json.loads(archive.read('orchard-export.json'))
        self.assertEqual(data['papers'][0]['title'],'=HYPERLINK("bad")'); self.assertIn("'=HYPERLINK",archive.read('papers.csv').decode())
        for t in ('documents','runs','source_records','discoveries','decisions','criteria','resources','audit'): self.assertIn(t,data)
        self.assertIsNone(data['manifest']['study_count']); self.assertIn('search-strategy.txt',archive.namelist())
    def test_persistence_interruption_recovery(self):
        self.paper()
        with self.s.db.connect() as c: c.execute("UPDATE runs SET status='running'")
        self.s.close(); self.s=Service(self.path,self.provider)
        self.assertEqual(self.s.listing()['total'],1); self.assertEqual(self.s.db.rows('SELECT status FROM runs')[0]['status'],'interrupted')
    def test_audit_append_only(self):
        self.paper()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.db.connect() as c: c.execute("UPDATE audit SET event='fake'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.db.connect() as c: c.execute('DELETE FROM audit')
    def test_dashboard_uses_db_counts(self):
        self.assertEqual(self.s.overview()['counts']['papers'],0); self.paper(); self.assertEqual(self.s.overview()['counts']['papers'],1)
    def test_manual_metadata_correction_preserves_raw(self):
        p=self.paper(); self.s.update_metadata(p['id'],{'title':'Corrected display'})
        full=self.s.paper(p['id']); self.assertEqual(full['title'],'Corrected display'); self.assertEqual(json.loads(full['sources'][0]['raw'])['title'],'Synthetic pupil memory experiment')
    def test_feasibility_assessments_versioned(self):
        p=self.paper(); self.s.assess(p['id'],{'data_level':'Unknown'}); self.s.assess(p['id'],{'data_level':'Raw continuous samples'})
        self.assertEqual(len(self.s.paper(p['id'])['assessments']),2)
    def test_query_snapshots_immutable(self):
        q=self.query()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.db.connect() as c: c.execute('UPDATE queries SET query=? WHERE id=?',('other',q['id']))
    def test_malformed_provider_response(self):
        class Transport:
            def get(self,*args,**kwargs): return b'{"error":"invalid"}', '', {}
        with self.assertRaises(ValueError): EuropePMC(Transport()).page('pupil')
    def test_doi_normalization(self):
        self.assertEqual(doi_normalize(' DOI:10.1234/TEST '),'10.1234/test'); self.assertIsNone(doi_normalize('not a doi'))
    def test_private_url_rejected(self):
        with self.assertRaises(ValueError): public_url('file:///etc/passwd')
        with self.assertRaises(ValueError): public_url('http://127.0.0.1/')
    def test_reuse_without_prior_does_not_launch_search(self):
        with self.assertRaises(Conflict): self.s.search(self.query()['id'],action='reuse',background=False)
        self.assertEqual(self.provider.calls,[])
    def test_stale_criteria_rejected(self):
        p=self.paper(); old=self.criteria(); self.s.save_criteria('New criteria','New exclusions')
        with self.assertRaises(Conflict): self.s.screen(p['id'],'title_abstract','include',criteria_id=old['id'])
    def test_rejected_record_evidence_retained(self):
        self.run_records([{'title':'Malformed fixture'}])
        rejected=self.s.db.rows("SELECT * FROM audit WHERE event='provider_record_rejected'")
        self.assertEqual(json.loads(rejected[0]['detail'])['raw']['title'],'Malformed fixture')
    def test_export_empty_tables_have_csv_headers(self):
        z=zipfile.ZipFile(io.BytesIO(self.s.export_zip()))
        self.assertTrue(z.read('papers.csv').decode().startswith('id,title,'))
    def test_http_workflow_and_cross_origin_rejection(self):
        server=make_server(self.s,0); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start(); base=f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(base+'/') as r: self.assertIn(b'orchard',r.read())
            payload=json.dumps({'name':'test','text':'pupillometry\nmemory'}).encode()
            req=urllib.request.Request(base+'/api/documents',data=payload,headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as r: self.assertIn('id',json.load(r))
            req=urllib.request.Request(base+'/api/documents',data=payload,headers={'Content-Type':'application/json','Origin':'https://evil.example'})
            with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code,403)
        finally: server.shutdown(); server.server_close(); thread.join()

if __name__=='__main__': unittest.main()
