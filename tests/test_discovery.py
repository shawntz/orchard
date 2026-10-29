import base64
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from orchard.service import Service
from orchard.server import make_server
from orchard import discovery
from orchard.repository_sweep import classify

class Web:
    def get(self,url,**kwargs):
        if url.endswith('/readme'):
            payload={'content':base64.b64encode(b'We recorded pupilsize during an attention task. Publication: 10.1234/paper').decode()}
        elif '/works?' in url: payload={'message':{'items':[]}}
        elif '/works/' in url: payload={'message':{'DOI':'10.1234/paper','type':'journal-article','title':['Attention and pupil size']}}
        else: raise ValueError('Fixture has no such resource')
        return json.dumps(payload).encode(),url,{}

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Service(Path(self.tmp.name)/'db',transport=Web())
        with self.s.db.connect() as c:
            c.execute('INSERT INTO sweep_projects VALUES(?,?,?,?,?,?,?,?)',('p','github','lab/data','https://github.com/lab/data','Attention task','',None,None))
    def tearDown(self): self.s.close();self.tmp.cleanup()
    def test_readme_and_missing_doi_discovery(self):
        d=discovery.discover(self.s,'p')
        self.assertTrue(d['documents']);self.assertEqual(d['candidates'][0]['doi'],'10.1234/paper')
        self.assertIn('candidate',[e['decision'] for e in d['events']])
    def test_pupillometry_not_dependent_on_default_term_list(self):
        self.assertEqual(classify('Attention measured using pupillometry',['attention'])['status'],'candidate')
    def test_history_and_export(self):
        discovery.decision(self.s,'p','exclude','Only example data, verified in README')
        discovery.decision(self.s,'p','review','Need article confirmation')
        self.assertEqual(len(discovery.detail(self.s,'p')['events']),2)
        self.assertEqual(len(self.s.export()['discovery_events']),2)
    def test_reader_and_http_security(self):
        doc=discovery.save_document(self.s,'p','article','fixture','Attention and pupil experiment')
        pdf=discovery.save_document(self.s,'p','article_pdf','fixture','Pupil recording',pdf=b'%PDF-1.4\n% fixture')
        server=make_server(self.s,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            root='http://127.0.0.1:'+str(server.server_port)
            with urllib.request.urlopen(root+'/api/reader/text/'+doc) as r:
                self.assertIn('Attention',json.load(r)['content'])
            with urllib.request.urlopen(root+'/') as r:
                self.assertIn(b'/discovery.js',r.read())
            with urllib.request.urlopen(root+'/discovery.js') as r:
                self.assertIn(b'discoveryView',r.read())
            with urllib.request.urlopen(root+'/api/reader/pdf/'+pdf) as r:
                self.assertEqual(r.headers.get_content_type(),'application/pdf')
                self.assertTrue(r.read().startswith(b'%PDF-'))
        finally: server.shutdown();server.server_close();thread.join()
    def test_bad_upload_and_empty_reason(self):
        with self.assertRaises(ValueError): discovery.retrieve(self.s,'p',upload=base64.b64encode(b'not a PDF').decode())
        with self.assertRaises(ValueError): discovery.decision(self.s,'p','exclude','')

if __name__=='__main__': unittest.main()
