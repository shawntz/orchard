"""Regression checks for misleading publication suggestions and OSF app shells."""
import tempfile
import unittest
from pathlib import Path
from orchard.service import Service
from orchard import discovery

class EmptyOSF:
    def get(self,url,**kwargs):
        return b'<html><body>OSF</body></html>',url,{'Content-Type':'text/html'}

class ArticleMatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Path(self.tmp.name)/'db',transport=EmptyOSF())
        with self.s.db.connect() as c:
            c.execute('INSERT INTO sweep_projects VALUES(?,?,?,?,?,?,?,?)',('p','osf','abc12','https://osf.io/abc12/','Revealing the hidden spatial distribution of attention','',None,None))
    def tearDown(self):
        self.s.close();self.tmp.cleanup()
    def test_shell_not_saved_as_article(self):
        with self.assertRaisesRegex(ValueError,'not a readable article'):
            discovery.fetch_document(self.s,'p','https://osf.io/preprints/example/download')
        self.assertEqual(self.s.db.rows('SELECT * FROM discovery_documents'),[])
    def test_prior_weak_suggestions_hidden_but_preserved(self):
        with self.s.db.connect() as c:
            c.execute('INSERT INTO discovery_candidates VALUES(?,?,?,?,?,?,?,?)',('weak','p','10.1234/weak','Revealing the hidden',0.4,'Title search','{}','2026'))
        self.assertEqual(discovery.detail(self.s,'p')['candidates'],[])
        self.assertEqual(len(self.s.db.rows('SELECT * FROM discovery_candidates')),1)
        with self.assertRaisesRegex(ValueError,'too weak'):
            discovery.retrieve(self.s,'p',candidate_id='weak')
    def test_prior_shell_label_is_honest(self):
        discovery.save_document(self.s,'p','article','https://osf.io/','OSF')
        self.assertEqual(discovery.detail(self.s,'p')['documents'][0]['kind'],'unavailable_article_page')

if __name__=='__main__': unittest.main()
