"""Offline tests for conservative repository-first screening."""
import tempfile
import unittest
from pathlib import Path

from orchard.repository_sweep import RepositorySweep, classify, _edfs
from orchard.service import Service
from orchard.providers import Page


class FakeAdapter:
    def search(self, repo, query, max_pages):
        if repo == 'osf':
            for _ in range(2):
                yield {'repository': repo, 'id': 'abc12', 'url': 'https://osf.io/abc12/',
                       'title': 'Memory and pupil experiment', 'description': 'Pupillometry attention', 'doi': None,
                       'files': [{'path':'raw/session.ASC','extension':'.asc','size':10,'url':'https://osf.io/file'}]}
        elif repo == 'dryad':
            raise RuntimeError('provider unavailable')


class RepositoryTests(unittest.TestCase):
    def test_exact_extension_and_conservative_classification(self):
        self.assertEqual([x['extension'] for x in _edfs([{'name':'file.EDF'}, {'name':'file.asc.zip'}, {'name':'a/one.asc'}])], ['.edf','.asc'])
        self.assertEqual(classify('Memory pupil diameter')['status'], 'candidate')
        self.assertEqual(classify('Eyetracking study')['status'], 'review')
        self.assertEqual(classify('Nothing relevant')['status'], 'review')

    def test_persists_deduped_projects_partial_failure_and_export(self):
        with tempfile.TemporaryDirectory() as path:
            s=Service(Path(path)/'db.sqlite3')
            try:
                result=s.repository_sweep('pupil',repositories=['osf','dryad'],adapter=FakeAdapter())
                self.assertEqual(result['sweep']['status'],'partial')
                self.assertEqual(result['counts']['projects_with_files'],1)
                self.assertEqual(result['counts']['candidate'],1)
                self.assertEqual(len(result['projects'][0]['files']),1)
                self.assertEqual(len(s.export()['sweep_findings']),1)
            finally:
                s.close()

    def test_zenodo_file_listing(self):
        class Zenodo(RepositorySweep):
            def get(self,url):
                return {'hits': {'hits':[{'id':45,'metadata':{'title':'A pupil memory dataset'},'files':[{'key':'data/test.edf','links':{'self':'https://zenodo.org/a'}}]}]}}
        results=list(Zenodo().search('zenodo','pupil',1))
        self.assertEqual(results[0]['files'][0]['extension'],'.edf')
        self.assertEqual(results[0]['id'],'45')

    def test_article_review_is_conservative_without_publication_doi(self):
        with tempfile.TemporaryDirectory() as path:
            s=Service(Path(path)/'db.sqlite3')
            try:
                project=s.repository_sweep('pupil',['osf'],adapter=FakeAdapter())['projects'][0]
                review=s.review_repository_article(project['id'])
                self.assertEqual(review['status'],'insufficient_text')
                self.assertIn('publication DOI',review['reason'])
            finally:
                s.close()

    def test_linked_pmc_fulltext_screening(self):
        class Provider:
            def page(self, query, cursor='*', size=10, callback=None):
                return Page([{'doi':'10.1234/article','pmcid':'PMC123','fullTextUrlList':{'fullTextUrl':[]}}],None,1)
            def fulltext(self, pmcid):
                return 'This memory task recorded pupil diameter with EyeLink.', 'https://europepmc.org/articles/PMC123'
        with tempfile.TemporaryDirectory() as path:
            s=Service(Path(path)/'db.sqlite3',Provider())
            try:
                with s.db.connect() as c:
                    c.execute('INSERT INTO sweep_projects(id,repository,external_id,url,title,description,doi,paper_id) VALUES(?,?,?,?,?,?,?,?)',('p','dryad','d','https://datadryad.org/d','Study','Abstract','10.1234/article',None))
                review=s.review_repository_article('p')
                self.assertEqual(review['status'],'candidate')
                self.assertIn('europepmc.org',review['text_source'])
            finally:
                s.close()


if __name__ == '__main__':
    unittest.main()
