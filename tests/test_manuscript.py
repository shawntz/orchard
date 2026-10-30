"""Manuscript supplementary tables, from synthetic fixtures only."""
import csv, io, json, tempfile, threading, unittest, urllib.request, zipfile
from pathlib import Path
from orchard.service import Service
from orchard.providers import Page
from orchard.server import make_server
from orchard.manuscript import TABLES
from test_workflow import record, MockProvider, MockPdfSource

JOURNAL={'journalInfo':{'journal':{'title':'Journal of Synthetic Fixtures'},'volume':'12','issue':'3'},'pageInfo':'45-67'}


class Manuscript(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'orchard.sqlite3'
        self.provider=MockProvider(); self.s=Service(self.path,self.provider,pdf_source=MockPdfSource())
    def tearDown(self): self.s.close(); self.tmp.cleanup()

    def tables(self):
        z=zipfile.ZipFile(io.BytesIO(self.s.manuscript_zip()))
        out={}
        for name in z.namelist():
            if name.endswith('.csv'):
                text=z.read(name).decode('utf-8')
                self.assertTrue(text.startswith('﻿'),name)
                out[name]=list(csv.DictReader(io.StringIO(text[1:])))
        return z,out

    def by_title(self):
        return {p['title']:p['id'] for p in self.s.listing()['items']}

    def build_review(self):
        doc=self.s.document(self.s.import_document('terms.txt','Pupil\n\tpupillometry; pupil size\nMemory\n\tworking memory')['id'])
        q=self.s.generate_query([[t['id'] for t in c['terms']] for c in doc['concepts']],doc['id'],'Broad search')
        q=self.s.save_query('Broad search',q['query'],json.loads(q['term_ids']),doc['id'],q['id'],approved=True)
        self.provider.pages=[Page([
            record('1','10.1234/inc','Included pupil study',authorString='Zeta Z, Alpha A.',**JOURNAL,
                   abstractText='Data are available at https://osf.io/abcde/ for the dataset.'),
            record('2','10.1234/ftx','Excluded at full text',authorString='Beta B.'),
            record('3','10.1234/tax','Off-topic title',authorString='Gamma G.'),
            record('4','10.1234/na','Paywalled report',authorString='Delta D.'),
            record('5','10.1234/ns','Unscreened record',authorString='Epsilon E.'),
        ],None,5)]
        self.s.search(q['id'],background=False)
        self.s.import_records('more.json',[record('2b','10.1234/inc',source='PMC'),record('6',None,'Included pupil study',authorString='Zeta Z, Alpha A.')])
        self.s.save_criteria('Pupil recording during a memory task','No pupil data')
        self.s.save_criteria('Pupil recording during a working memory task','No pupil data; review articles')
        ids=self.by_title()
        dupe=next(d for d in self.s.db.rows('SELECT * FROM duplicates') if ids['Included pupil study'] in (d['a'],d['b']))
        self.s.resolve_duplicate(dupe['id'],'merge','Same report')
        ids=self.by_title(); inc=ids['Included pupil study']
        def screen(pid,stage,decision,reason=''):
            self.s.screen(pid,stage,decision,reason,'note '+decision,'Reviewer R',self.s.require('papers',pid)['revision'])
        screen(inc,'title_abstract','include'); screen(inc,'fulltext','include')
        screen(ids['Excluded at full text'],'title_abstract','include'); screen(ids['Excluded at full text'],'fulltext','exclude','No pupil data')
        screen(ids['Off-topic title'],'title_abstract','exclude','Not about pupils')
        screen(ids['Paywalled report'],'title_abstract','include'); screen(ids['Paywalled report'],'fulltext','inaccessible')
        osf=next(r for r in self.s.paper(inc)['resources'] if r['kind']=='dataset')
        self.s.resource_action(osf['id'],'confirm','Inspected OSF project; raw EDF files')
        self.s.assess(inc,{'reuse_feasibility':'High: raw samples shared'})
        return ids

    def test_tables_and_dictionary(self):
        ids=self.build_review(); z,t=self.tables()
        self.assertIn('README.txt',z.namelist())
        for name,_,_,columns in TABLES:
            self.assertEqual(next(csv.reader(io.StringIO(z.read(name).decode('utf-8-sig')))),[c for c,_ in columns])
        documented={(r['file'],r['column']) for r in t['data_dictionary.csv']}
        for name,_,_,columns in TABLES:
            for col,_ in columns: self.assertIn((name,col),documented)

        runs=t['table_s1_search_strategy.csv']
        self.assertEqual([r['database'] for r in runs],['Europe PMC','Manual import of provider records'])
        self.assertEqual(runs[0]['search_label'],'Broad search'); self.assertEqual(runs[0]['status'],'completed')
        self.assertEqual(runs[0]['records_retrieved'],'5'); self.assertEqual(runs[0]['retrieval_cap'],'300')

        terms=t['table_s2_search_terms.csv']
        self.assertEqual([r['term'] for r in terms],['pupillometry','pupil size','working memory'])
        self.assertEqual(terms[0]['concept_group'],'Pupil'); self.assertEqual(terms[0]['searches_run'],'1')

        crit=t['table_s3_eligibility_criteria.csv']
        self.assertEqual([r['current'] for r in crit],['no','yes']); self.assertEqual(crit[1]['screening_decisions_recorded'],'7')

        flow={r['item']:r['n'] for r in t['table_s4_prisma_flow.csv']}
        self.assertEqual(flow['Records identified: Europe PMC'],'5'); self.assertEqual(flow['Records identified: total'],'7')
        self.assertEqual(flow['Duplicate records removed: identical identifiers'],'1')
        self.assertEqual(flow['Duplicate records removed: researcher-confirmed'],'1')
        self.assertEqual(flow['Records screened'],'4'); self.assertEqual(flow['Records excluded at title/abstract'],'1')
        self.assertEqual(flow['Reports sought for retrieval'],'3'); self.assertEqual(flow['Reports not retrieved'],'1')
        self.assertEqual(flow['Reports assessed for eligibility'],'2'); self.assertEqual(flow['Reports excluded: No pupil data'],'1')
        self.assertEqual(flow['Reports included'],'1'); self.assertEqual(flow['Studies included'],'')
        self.assertEqual(flow['Records not yet screened'],'1')

        inc=t['table_s5_included_studies.csv']; self.assertEqual(len(inc),1); row=inc[0]
        self.assertEqual(row['journal'],'Journal of Synthetic Fixtures'); self.assertEqual(row['pages'],'45-67')
        self.assertEqual(row['citation'],'Zeta Z, Alpha A (2024). Included pupil study. Journal of Synthetic Fixtures, 12(3), 45-67. https://doi.org/10.1234/inc')
        self.assertEqual(row['verified_data_links'],'https://osf.io/abcde/'); self.assertEqual(row['criteria_version'],'2')
        self.assertEqual(row['reuse_feasibility'],'High: raw samples shared'); self.assertEqual(row['eyelink_verdict'],'not_scanned')
        self.assertEqual(row['orchard_id'],ids['Included pupil study'])

        exc=t['table_s6_excluded_full_text.csv']
        self.assertEqual([(r['title'],r['exclusion_reason']) for r in exc],[('Excluded at full text','No pupil data')])

        log=t['table_s7_screening_log.csv']
        self.assertEqual([r['authors'] for r in log],['Beta B.','Delta D.','Epsilon E.','Gamma G.','Zeta Z, Alpha A.'])
        status={r['title']:r['final_status'] for r in log}
        self.assertEqual(status,{'Included pupil study':'included','Excluded at full text':'excluded_full_text','Off-topic title':'excluded_title_abstract','Paywalled report':'not_retrieved','Unscreened record':'not_screened'})
        merged=next(r for r in log if r['title']=='Included pupil study')
        self.assertEqual(merged['found_in_searches'],'1; 2'); self.assertEqual(merged['merged_duplicates'],'1')

        data=t['table_s8_data_availability.csv']
        self.assertEqual([(r['resource_type'],r['verification_status']) for r in data],[('dataset','researcher_verified')])
        self.assertEqual(data[0]['record_number'],'5')
        self.assertEqual([r['reuse_feasibility'] for r in t['table_s9_eyelink_and_reuse.csv']],['High: raw samples shared'])
        self.assertEqual(self.s.db.rows("SELECT COUNT(*) n FROM audit WHERE event='manuscript_export_created'")[0]['n'],1)

    def test_empty_workspace_has_headers_and_formula_guard(self):
        _,t=self.tables()
        flow={r['item']:r['n'] for r in t['table_s4_prisma_flow.csv']}
        self.assertEqual(flow['Records identified: total'],'0')
        self.s.import_records('x.json',[record(title='=HYPERLINK("bad")')])
        z=zipfile.ZipFile(io.BytesIO(self.s.manuscript_zip()))
        self.assertIn("'=HYPERLINK",z.read('table_s7_screening_log.csv').decode())

    def test_http_download(self):
        server=make_server(self.s,0); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/export/manuscript') as r:
                self.assertEqual(r.headers['Content-Type'],'application/zip')
                self.assertIn('orchard-manuscript-tables.zip',r.headers['Content-Disposition'])
                self.assertIn('table_s4_prisma_flow.csv',zipfile.ZipFile(io.BytesIO(r.read())).namelist())
        finally: server.shutdown(); server.server_close(); thread.join()


if __name__=='__main__': unittest.main()
