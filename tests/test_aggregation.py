"""Multi-provider search, cross-source consolidation and full-text fallback. Synthetic fixtures only."""
import json, re, sqlite3, tempfile, threading, unittest, urllib.request, zlib
from pathlib import Path
from orchard import pdftext
from orchard.db import SCHEMA, Database
from orchard.providers import (OpenAlex, Page, SemanticScholar, normalize_core, normalize_openalex, normalize_s2,
                               normalize_pubmed, parse_article_xml, pubmed_record, require_portable, semantic_scholar_query)
from orchard.server import make_server
from orchard.service import Service, Conflict


class Provider:
    def __init__(self, name, pages=None, version='test'):
        self.name, self.label, self.version = name, name.title(), version
        self.pages = list(pages or [Page([], None, 0)]); self.calls = []
    def page(self, query, cursor, size, callback):
        self.calls.append((query, cursor, size)); item = self.pages.pop(0)
        if isinstance(item, Exception): raise item
        return item


class EPMC(Provider):
    def __init__(self, pages=None, fulltext_error=None):
        super().__init__('europepmc', pages); self.fulltext_error = fulltext_error; self.fulltext_calls = []
    def fulltext(self, pmcid, callback):
        self.fulltext_calls.append(pmcid)
        if self.fulltext_error: raise self.fulltext_error
        return 'Methods\nData are available at https://osf.io/abcde/.', 'https://example.org/' + pmcid


class Locator:
    def __init__(self, name, links=(), found=None, error=None, configured=True):
        self.name, self.links, self.found, self.error, self.configured = name, list(links), found or {}, error, configured
        self.note = 'Needs configuration.'; self.calls = []
    def locate(self, ids, callback):
        self.calls.append(dict(ids))
        if self.error: raise self.error
        return self.links, self.found


class Fetcher:
    def __init__(self, responses): self.responses = responses; self.calls = []
    def get(self, url, limit=None, callback=None, headers=None):
        self.calls.append(url); value = self.responses.get(url, OSError('HTTP Error 404: Not Found'))
        if isinstance(value, Exception): raise value
        return value


def make_pdf(words=80):
    lines = [' '.join(f'pupil{i} diameter measured during memory task' for i in range(n, n + 4)) for n in range(0, words, 4)]
    content = b'BT /F1 11 Tf 72 720 Td ' + b' T* '.join(b'(' + l.encode() + b') Tj' for l in lines) + b' ET'
    stream = zlib.compress(content)
    return (b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n4 0 obj\n<< /Length ' + str(len(stream)).encode() +
            b' /Filter /FlateDecode >>\nstream\n' + stream + b'\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n')


def epmc(id='1', doi='10.1234/shared', title='Synthetic pupil memory experiment', **extra):
    return {'id': id, 'source': 'MED', 'pmid': id, 'title': title, 'doi': doi, 'pubYear': '2024', **extra}


def openalex(id='W1', doi='https://doi.org/10.1234/SHARED', title='Synthetic pupil memory experiment', **extra):
    return {'id': 'https://openalex.org/' + id, 'doi': doi, 'display_name': title, 'publication_year': 2024,
            'ids': {'pmid': 'https://pubmed.ncbi.nlm.nih.gov/1'}, 'abstract_inverted_index': {'Pupil': [0], 'dilated.': [1]},
            'authorships': [{'author': {'display_name': 'Researcher A'}}], **extra}


class Aggregation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / 'orchard.sqlite3'
    def tearDown(self):
        self.s.close(); self.tmp.cleanup()
    def service(self, providers, locators=(), fetcher=None):
        self.s = Service(self.path, providers={p.name: p for p in providers}, locators=list(locators))
        if fetcher: self.s.fetcher = fetcher
        return self.s
    def query(self, text='pupillometry AND "working memory"'):
        return self.s.save_query('Test', text, approved=True)

    def test_one_run_per_provider_and_cross_source_consolidation(self):
        a = EPMC([Page([epmc()], None, 1)]); b = Provider('openalex', [Page([openalex()], None, 1)])
        self.service([a, b]); result = self.s.search(self.query()['id'], background=False)
        runs = result['runs']; self.assertEqual([r['provider'] for r in runs], ['europepmc', 'openalex'])
        self.assertEqual(len({r['batch_id'] for r in runs}), 1); self.assertTrue(all(r['status'] == 'completed' for r in runs))
        self.assertEqual(self.s.listing()['total'], 1)
        paper = self.s.paper(self.s.listing()['items'][0]['id'])
        self.assertEqual(sorted(s['provider'] for s in paper['sources']), ['europepmc', 'openalex'])
        self.assertEqual(paper['abstract'], 'Pupil dilated.')  # missing in Europe PMC, filled from OpenAlex
        self.assertEqual(len(paper['discoveries']), 2)
        self.assertEqual(self.s.listing(source='openalex')['total'], 1)

    def test_identifier_bridge_across_providers_without_doi(self):
        # PubMed lacks the DOI but shares the PMID with OpenAlex.
        pm = {'pmid': '1', 'title': 'Synthetic pupil memory experiment', 'year': '2024'}
        self.service([Provider('openalex', [Page([openalex()], None, 1)]), Provider('pubmed', [Page([pm], None, 1)])])
        self.s.search(self.query()['id'], background=False)
        self.assertEqual(self.s.listing()['total'], 1)

    def test_selected_providers_and_strict_reuse(self):
        a = EPMC([Page([], None, 0), Page([], None, 0)]); b = Provider('openalex', [Page([], None, 0)])
        self.service([a, b]); q = self.query()
        only = self.s.search(q['id'], providers=['europepmc'], background=False)
        self.assertEqual([r['provider'] for r in only['runs']], ['europepmc']); self.assertEqual(b.calls, [])
        with self.assertRaises(Conflict): self.s.search(q['id'], action='reuse', background=False)
        with self.assertRaises(Conflict): self.s.search(q['id'], background=False)
        self.s.search(q['id'], providers=['openalex'], background=False)
        reused = self.s.search(q['id'], action='reuse', background=False)
        self.assertTrue(reused['reused']); self.assertEqual(len(reused['runs']), 2)
        with self.assertRaises(ValueError): self.s.search(q['id'], providers=['nowhere'], background=False)
        with self.assertRaises(ValueError): self.s.search(q['id'], providers=[], background=False)

    def test_unconfigured_provider_rejected(self):
        b = Provider('core'); b.configured = False; b.note = 'Set ORCHARD_CORE_API_KEY.'
        self.service([EPMC(), b])
        with self.assertRaises(ValueError) as ctx: self.s.search(self.query()['id'], providers=['core'], background=False)
        self.assertIn('ORCHARD_CORE_API_KEY', str(ctx.exception))
        self.assertEqual([r['provider'] for r in self.s.search(self.query()['id'], background=False)['runs']], ['europepmc'])

    def test_fielded_query_only_runs_on_europe_pmc(self):
        self.service([EPMC([Page([], None, 0)]), Provider('pubmed')])
        q = self.query('TITLE:"pupil" AND memory')
        with self.assertRaises(ValueError): self.s.search(q['id'], background=False)
        self.assertEqual(self.s.db.rows('SELECT * FROM runs'), [])
        self.assertEqual(len(self.s.search(q['id'], providers=['europepmc'], background=False)['runs']), 1)

    def test_one_provider_failure_does_not_block_others(self):
        self.service([EPMC([OSError('network down')]), Provider('openalex', [Page([openalex()], None, 1)])])
        runs = self.s.search(self.query()['id'], background=False)['runs']
        self.assertEqual({r['provider']: r['status'] for r in runs}, {'europepmc': 'failed', 'openalex': 'completed'})
        self.assertEqual(self.s.listing()['total'], 1)

    def test_resume_any_registered_provider(self):
        b = Provider('openalex', [Page([openalex()], 'offset:1', 2), Page([openalex('W2', None, 'Another report')], None, 2)])
        self.service([EPMC(), b])
        run = self.s.search(self.query()['id'], providers=['openalex'], max_records=1, background=False)['run']
        self.assertEqual(run['status'], 'partial'); self.s.execute(run['id'])
        self.assertEqual(self.s.require('runs', run['id'])['status'], 'completed'); self.assertEqual(b.calls[1][1], 'offset:1')

    def test_provider_links_become_resources(self):
        oa = openalex(best_oa_location={'is_oa': True, 'pdf_url': 'https://repo.example/paper.pdf', 'landing_page_url': 'https://repo.example/paper'})
        self.service([Provider('openalex', [Page([oa], None, 1)])]); self.s.search(self.query()['id'], background=False)
        resources = self.s.paper(self.s.listing()['items'][0]['id'])['resources']
        self.assertIn(('pdf', 'https://repo.example/paper.pdf', 'openalex metadata'), [(r['kind'], r['url'], r['origin']) for r in resources])

    def test_fulltext_falls_through_sources_until_one_succeeds(self):
        pdf = make_pdf()
        fetcher = Fetcher({'https://publisher.example/paper.pdf': (b'<html>Sign in</html>', 'https://publisher.example/login', {'Content-Type': 'text/html'}),
                           'https://repo.example/accepted.pdf': (pdf, 'https://repo.example/accepted.pdf', {'Content-Type': 'application/pdf'})})
        locators = [Locator('broken', error=OSError('HTTP Error 429')), Locator('needs-key', configured=False),
                    Locator('publisher', [{'url': 'https://publisher.example/paper.pdf', 'kind': 'pdf', 'evidence': 'publisher'}]),
                    Locator('repository', [{'url': 'https://repo.example/accepted.pdf', 'kind': 'pdf', 'evidence': 'green OA'}]),
                    Locator('never-reached')]
        self.service([EPMC([Page([epmc(pmcid='PMC9')], None, 1)], fulltext_error=OSError('HTTP Error 404'))], locators, fetcher)
        self.s.search(self.query()['id'], background=False); pid = self.s.listing()['items'][0]['id']
        paper = self.s.fetch_fulltext(pid); r = paper['retrieval']
        self.assertEqual((r['source'], r['format'], r['text_extracted']), ('repository', 'pdf', True))
        outcomes = [(a['source'], a['outcome']) for a in r['attempts']]
        self.assertEqual(outcomes[0], ('europepmc', 'failed'))
        for expected in [('broken', 'failed'), ('needs-key', 'skipped'), ('publisher', 'not_pdf'), ('repository', 'retrieved')]:
            self.assertIn(expected, outcomes)
        self.assertEqual(locators[-1].calls, [])
        self.assertEqual(locators[0].calls[0]['doi'], '10.1234/shared')
        self.assertIn('https://europepmc.org/articles/PMC9?pdf=render', fetcher.calls)  # identifier route tried before lookups
        self.assertEqual(len(paper['files']), 1); self.assertIn('pupil0 diameter', paper['fulltexts'][0]['content'])
        row, data = self.s.file(paper['files'][0]['id']); self.assertEqual(data, pdf)
        self.assertTrue(any(x['url'] == 'https://repo.example/accepted.pdf' and x['status'] == 'url_reachable' for x in paper['resources']))
        self.assertEqual(json.loads(self.s.db.rows("SELECT detail FROM audit WHERE event='fulltext_retrieved'")[0]['detail'])['result']['source'], 'repository')

    def test_xml_preferred_when_available(self):
        locator = Locator('lookup')
        self.service([EPMC([Page([epmc(pmcid='PMC9')], None, 1)])], [locator], Fetcher({}))
        self.s.search(self.query()['id'], background=False)
        paper = self.s.fetch_fulltext(self.s.listing()['items'][0]['id'])
        self.assertEqual(paper['retrieval']['format'], 'xml'); self.assertEqual(locator.calls, [])
        self.assertTrue(any(r['kind'] == 'dataset' for r in paper['resources']))

    def test_lookup_discovered_pmcid_enables_xml(self):
        epmc_provider = EPMC([Page([epmc()], None, 1)])
        self.service([epmc_provider], [Locator('ids', found={'pmcid': '4879132'})], Fetcher({}))
        self.s.search(self.query()['id'], background=False)
        paper = self.s.fetch_fulltext(self.s.listing()['items'][0]['id'])
        self.assertEqual(paper['retrieval']['format'], 'xml'); self.assertEqual(epmc_provider.fulltext_calls, ['PMC4879132'])

    def test_all_sources_fail_records_attempts_and_links(self):
        locators = [Locator('lookup', [{'url': 'https://publisher.example/article', 'kind': 'fulltext', 'evidence': 'landing'}])]
        self.service([EPMC([Page([epmc()], None, 1)])], locators, Fetcher({}))
        self.s.search(self.query()['id'], background=False); pid = self.s.listing()['items'][0]['id']
        with self.assertRaises(ValueError) as ctx: self.s.fetch_fulltext(pid)
        self.assertIn('lookup: located', str(ctx.exception))
        detail = json.loads(self.s.db.rows("SELECT detail FROM audit WHERE event='fulltext_retrieval_failed'")[0]['detail'])
        self.assertIsNone(detail['result']); self.assertTrue(detail['attempts'])
        self.assertTrue(any(r['url'] == 'https://publisher.example/article' and r['origin'] == 'lookup lookup' for r in self.s.paper(pid)['resources']))
        self.assertEqual(self.s.paper(pid)['files'], [])

    def test_unreadable_pdf_still_stored(self):
        blob = b'%PDF-1.7\n' + b'\x00' * 500
        fetcher = Fetcher({'https://repo.example/scan.pdf': (blob, 'https://repo.example/scan.pdf', {})})
        self.service([EPMC([Page([epmc()], None, 1)])], [Locator('repo', [{'url': 'https://repo.example/scan.pdf', 'kind': 'pdf', 'evidence': ''}])], fetcher)
        self.s.search(self.query()['id'], background=False)
        paper = self.s.fetch_fulltext(self.s.listing()['items'][0]['id'])
        self.assertFalse(paper['retrieval']['text_extracted']); self.assertEqual(paper['fulltexts'], [])
        self.assertIsNone(paper['files'][0]['text_method'])

    def test_no_identifiers_or_links(self):
        self.service([EPMC()], [Locator('lookup')])
        self.s.import_records('x', [{'source': 'PPR', 'id': '7', 'title': 'No identifiers'}])
        with self.assertRaises(ValueError): self.s.fetch_fulltext(self.s.listing()['items'][0]['id'])

    def test_pdf_served_over_http_and_listed_in_export(self):
        fetcher = Fetcher({'https://repo.example/a.pdf': (make_pdf(), 'https://repo.example/a.pdf', {'Content-Type': 'application/pdf'})})
        self.service([EPMC([Page([epmc()], None, 1)])], [Locator('repo', [{'url': 'https://repo.example/a.pdf', 'kind': 'pdf', 'evidence': ''}])], fetcher)
        self.s.search(self.query()['id'], background=False)
        fid = self.s.fetch_fulltext(self.s.listing()['items'][0]['id'])['files'][0]['id']
        self.assertEqual(len(self.s.export()['files']), 1)
        server = make_server(self.s, 0); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/files/{fid}') as r:
                self.assertEqual(r.headers['Content-Type'], 'application/pdf'); self.assertTrue(r.read().startswith(b'%PDF'))
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_version_one_database_migrates(self):
        with sqlite3.connect(self.path) as c: c.executescript(SCHEMA)
        Database(self.path)
        with sqlite3.connect(self.path) as c:
            self.assertIn('batch_id', [r[1] for r in c.execute('PRAGMA table_info(runs)')])
            self.assertEqual(max(r[0] for r in c.execute('SELECT version FROM schema_version')), 2)
            c.execute('SELECT * FROM files')
        self.service([EPMC()])


class Adapters(unittest.TestCase):
    def test_semantic_scholar_translation(self):
        self.assertEqual(semantic_scholar_query('("pupil size" OR pupillometry) AND (memory OR recall) AND NOT animal'),
                         '("pupil size" | pupillometry) +(memory | recall) -animal')

    def test_portable_check(self):
        require_portable('"TITLE: words" AND pupil*', 'X')
        for q in ('TITLE:pupil', 'pupil[tiab]'):
            with self.assertRaises(ValueError): require_portable(q, 'X')

    def test_pubmed_record_ignores_reference_ids(self):
        xml = b'''<?xml version="1.0"?><!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle//EN" "https://dtd.nlm.nih.gov/x.dtd">
        <PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>42</PMID><Article><Journal><Title>J</Title><JournalIssue><PubDate><MedlineDate>2019 Jan-Feb</MedlineDate></PubDate></JournalIssue></Journal>
        <ArticleTitle>Pupil &amp; memory.</ArticleTitle><Abstract><AbstractText Label="METHODS">We measured <i>pupils</i>.</AbstractText></Abstract>
        <AuthorList><Author><LastName>Doe</LastName><Initials>J</Initials></Author><Author><CollectiveName>Group</CollectiveName></Author></AuthorList>
        <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation>
        <PubmedData><ArticleIdList><ArticleId IdType="pubmed">42</ArticleId><ArticleId IdType="pmc">PMC7</ArticleId></ArticleIdList>
        <ReferenceList><Reference><ArticleIdList><ArticleId IdType="doi">10.9/other</ArticleId></ArticleIdList></Reference></ReferenceList></PubmedData></PubmedArticle></PubmedArticleSet>'''
        rec = pubmed_record(next(parse_article_xml(xml).iter('PubmedArticle')))
        norm = normalize_pubmed(rec)
        self.assertEqual((norm['source_id'], norm['year'], norm['doi'], norm['pmcid']), ('PMID:42', '2019', None, 'PMC7'))
        self.assertEqual((norm['title'], norm['authors'], norm['abstract']), ('Pupil & memory.', 'Doe J, Group', 'METHODS: We measured pupils .'))

    def test_normalizers(self):
        oa = normalize_openalex(openalex(ids={'pmcid': 'https://www.ncbi.nlm.nih.gov/pmc/articles/4879132'},
                                         locations=[{'is_oa': True, 'pdf_url': 'https://arxiv.org/pdf/2101.00001v2'}]))
        self.assertEqual((oa['source_id'], oa['abstract'], oa['pmcid'], oa['arxiv'], oa['year']), ('W1', 'Pupil dilated.', 'PMC4879132', '2101.00001v2', '2024'))
        s2 = normalize_s2({'paperId': 'abc', 'externalIds': {'PubMedCentral': '55', 'PubMed': '9', 'DOI': '10.1/x'}, 'year': 2020,
                           'openAccessPdf': {'url': '', 'status': None}, 'authors': [{'name': 'A'}]})
        self.assertEqual((s2['pmcid'], s2['pmid'], s2['year'], s2['links']), ('PMC55', '9', '2020', []))
        core = normalize_core({'id': 5, 'downloadUrl': 'https://core.ac.uk/download/5.pdf', 'sourceFulltextUrls': ['https://repo.example/x'], 'yearPublished': 2021})
        self.assertEqual([l['kind'] for l in core['links']], ['pdf', 'fulltext']); self.assertEqual(core['source_id'], '5')

    def test_openalex_keyless_offset_paging(self):
        class T:
            def __init__(self): self.urls = []
            def get(self, url, callback=None, **_):
                self.urls.append(url); n = 100 if re.search(r'[?&]page=1(&|$)', url) else 20
                return json.dumps({'meta': {'count': 120}, 'results': [{'id': str(i)} for i in range(n)]}).encode(), url, {}
        t = T(); p = OpenAlex(t)
        first = p.page('pupil', '*', 60); second = p.page('pupil', first.cursor, 60); third = p.page('pupil', second.cursor, 60)
        self.assertEqual((len(first.records), first.cursor, len(second.records), second.cursor), (60, 'offset:60', 40, 'offset:100'))
        self.assertEqual((len(third.records), third.cursor), (20, None)); self.assertEqual(len(t.urls), 2)

    def test_semantic_scholar_token_paging_respects_size(self):
        class T:
            def __init__(self): self.urls = []
            def get(self, url, callback=None, **_):
                self.urls.append(url); nxt = None if 'token=' in url else 'TOKEN'
                return json.dumps({'total': 5, 'token': nxt, 'data': [{'paperId': url[-3:] + str(i)} for i in range(3 if nxt else 2)]}).encode(), url, {}
        t = T(); p = SemanticScholar(t)
        a = p.page('pupil', '*', 2); b = p.page('pupil', a.cursor, 2); c = p.page('pupil', b.cursor, 2)
        self.assertEqual([len(a.records), len(b.records), len(c.records)], [2, 1, 2])
        self.assertEqual((a.cursor, b.cursor, c.cursor), ('#2', 'TOKEN#0', None)); self.assertEqual(len(t.urls), 2)

    def test_pdf_text(self):
        text, method = pdftext.extract(make_pdf()); self.assertIn('pupil3 diameter', text); self.assertIn(method, ('pypdf', 'stdlib'))
        self.assertFalse(pdftext.is_pdf(b'<html>')); self.assertEqual(pdftext.extract(b'%PDF-1.4 nothing')[0], None)


if __name__ == '__main__': unittest.main()
