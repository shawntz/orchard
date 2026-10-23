"""EyeLink finder tests. Synthetic PDFs, repositories and files only; no network access."""
import io, json, re, tempfile, unittest, zipfile, zlib
from pathlib import Path
from orchard import eyelink
from orchard.service import Service
from test_workflow import MockProvider, record

EYELINK_EDF = b'SR_RESEARCH_1000FILE\nDATE: synthetic\nTYPE: EDF_FILE BINARY EVENT SAMPLE TAGGED\nVERSION: EYELINK II 1\n' + bytes(range(256)) * 40
EEG_EDF = b'0       ' + b'X' * 248 + bytes(2000)
EYELINK_ASC = b'** CONVERTED FROM D:\\data\\s1.edf using edfapi 4.2\n** TYPE: EDF_FILE BINARY EVENT SAMPLE TAGGED\n** VERSION: EYELINK II 1\nMSG\t1000 TRIALID 1\n'


def make_pdf(text_lines, uri):
    """A small valid-enough PDF: compressed content stream plus a compressed object stream holding a /URI annotation."""
    content = b'BT /F1 10 Tf 72 720 Td ' + b' T* '.join(b'(' + line.replace(b'(', b'\\(').replace(b')', b'\\)') + b') Tj' for line in text_lines) + b' ET'
    annot = b'<< /Type /Annot /Subtype /Link /A << /S /URI /URI (' + uri + b') >> >>'
    objects = []
    for body in (content, annot):
        packed = zlib.compress(body)
        objects.append(b'<< /Length %d /Filter /FlateDecode >>\nstream\n' % len(packed) + packed + b'\nendstream')
    return b'%PDF-1.7\n' + b'\n'.join(b'%d 0 obj\n' % (i + 1) + o + b'\nendobj' for i, o in enumerate(objects)) + b'\n%%EOF'


def make_zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


class FakeWeb:
    """Routes URLs to bytes or JSON; honours Range headers like a real file server."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, limit=8_000_000, callback=None, headers=None, truncate=False):
        self.calls.append((url, (headers or {}).get('Range')))
        if url not in self.routes:
            raise OSError('404 for ' + url)
        body = self.routes[url]
        body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        reply = {}
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', (headers or {}).get('Range', ''))
        if match:
            if match.group(1) == '':
                start = max(0, len(body) - int(match.group(2)))
                end = len(body) - 1
            else:
                start, end = int(match.group(1)), min(len(body) - 1, int(match.group(2) or len(body) - 1))
            reply['Content-Range'] = f'bytes {start}-{end}/{len(body)}'
            body = body[start:end + 1]
        if len(body) > limit:
            if not truncate:
                raise ValueError('Response exceeds safe size limit.')
            body = body[:limit]
        return body, url, reply


OSF = 'https://api.osf.io/v2'


def osf_routes(files):
    return {
        f'{OSF}/guids/abc12/?resolve=false': {'data': {'relationships': {'referent': {'data': {'type': 'nodes', 'id': 'abc12'}}}}},
        f'{OSF}/nodes/abc12/files/': {'data': [{'attributes': {'name': 'osfstorage'}, 'relationships': {'files': {'links': {'related': {'href': f'{OSF}/nodes/abc12/files/osfstorage/'}}}}}], 'links': {'next': None}},
        f'{OSF}/nodes/abc12/files/osfstorage/': {'data': [
            {'attributes': {'kind': 'folder', 'name': 'raw'}, 'relationships': {'files': {'links': {'related': {'href': f'{OSF}/nodes/abc12/files/osfstorage/raw/'}}}}, 'links': {}},
            {'attributes': {'kind': 'file', 'name': 'readme.txt', 'materialized_path': '/readme.txt', 'size': 10}, 'links': {'download': 'https://osf.io/download/readme/'}},
        ], 'links': {'next': None}},
        f'{OSF}/nodes/abc12/files/osfstorage/raw/': {'data': [
            {'attributes': {'kind': 'file', 'name': n, 'materialized_path': '/raw/' + n, 'size': len(b)}, 'links': {'download': 'https://osf.io/download/' + n}} for n, b in files.items()
        ], 'links': {'next': None}},
        f'{OSF}/nodes/abc12/children/': {'data': [], 'links': {'next': None}},
        **{'https://osf.io/download/' + n: b for n, b in files.items()},
    }


class Links(unittest.TestCase):
    def test_repository_links_normalized_and_deduplicated(self):
        text = ('Data are available at https://osf.io/ABC12/?view_only=deadbeef and doi:10.17605/OSF.IO/abc12. '
                'Recordings: https://doi.org/10.5281/zenodo.1234567; see https://zenodo.org/records/1234567. '
                'Figshare https://figshare.com/articles/dataset/Pupil_data/7654321 and Dryad 10.5061/dryad.x3ffbg7hd. '
                'Harvard 10.7910/DVN/ABC123 and https://openneuro.org/datasets/ds004010. '
                'Code at https://github.com/lab/pupil-analysis.git. See osf.io/about too.')
        links = {(l.repository, l.identifier) for l in eyelink.find_repository_links(text)}
        self.assertEqual(links, {('osf', 'abc12'), ('zenodo', '1234567'), ('figshare', '7654321'), ('dryad', '10.5061/dryad.x3ffbg7hd'),
                                 ('dataverse', 'doi:10.7910/DVN/ABC123'), ('openneuro', 'ds004010'), ('github', 'lab/pupil-analysis')})
        osf = next(l for l in eyelink.find_repository_links(text) if l.repository == 'osf')
        self.assertEqual(osf.params, {'view_only': 'deadbeef'})
        self.assertIn('Data are available', osf.evidence)

    def test_url_broken_across_pdf_lines(self):
        links = eyelink.find_repository_links('Raw data: https://osf.io/\nxy9z8/ (eye tracking).')
        self.assertEqual([(l.repository, l.identifier) for l in links], [('osf', 'xy9z8')])

    def test_unsupported_repository_reported_not_guessed(self):
        link = eyelink.find_repository_links('Data: https://data.mendeley.com/datasets/abc123xyz')[0]
        result = eyelink.check_repository(FakeWeb({}), link)
        self.assertEqual(result['status'], 'unsupported')


class Pdf(unittest.TestCase):
    def test_builtin_extracts_text_and_compressed_link_annotations(self):
        data = make_pdf([b'Data availability: raw pupil data are on Zenodo', b'(doi 10.5281/zenodo.7777777).'], b'https://osf.io/qq123/')
        text, uris, _ = eyelink.pdf_text(data)
        self.assertIn('raw pupil data are on Zenodo', text)
        self.assertIn('https://osf.io/qq123/', uris)
        found = {l.repository for l in eyelink.find_repository_links(text + '\n' + '\n'.join(uris))}
        self.assertEqual(found, {'osf', 'zenodo'})

    def test_html_access_page_rejected(self):
        with self.assertRaises(ValueError):
            eyelink.pdf_text(b'<!doctype html><title>Just a moment...</title>')


class Files(unittest.TestCase):
    def test_header_classification(self):
        self.assertEqual(eyelink.header_kind(EYELINK_EDF, 'edf'), 'eyelink')
        self.assertEqual(eyelink.header_kind(EEG_EDF, 'edf'), 'european_data_format')
        self.assertEqual(eyelink.header_kind(EYELINK_ASC, 'asc'), 'eyelink')
        self.assertEqual(eyelink.header_kind(b'ncols 10\nnrows 10\n', 'asc'), 'unrecognized')
        self.assertEqual(eyelink.header_kind(zlib.compress(EYELINK_ASC, wbits=31), 'asc'), 'eyelink')
        self.assertEqual(eyelink.file_kind('S1.EDF'), 'edf'); self.assertEqual(eyelink.file_kind('s1.asc.gz'), 'asc')

    def test_remote_zip_listed_and_member_probed_with_ranges(self):
        archive = make_zip({'sub-01/s1.edf': EYELINK_EDF, 'sub-01/eeg.edf': EEG_EDF, 'notes.txt': b'x' * 100_000})
        web = FakeWeb({'https://files.example/data.zip': archive})
        rows = eyelink.inspect_zip(web, eyelink.Budget(), {'path': 'data.zip', 'size': len(archive), 'url': 'https://files.example/data.zip', 'probe_url': 'https://files.example/data.zip'}, {})
        by_path = {r['path']: r for r in rows}
        self.assertEqual(by_path['data.zip']['header'], 'listed')
        self.assertEqual(by_path['data.zip!/sub-01/s1.edf']['header'], 'eyelink')
        self.assertEqual(by_path['data.zip!/sub-01/eeg.edf']['header'], 'european_data_format')
        self.assertTrue(all(r for _, r in web.calls), 'every archive read must be a byte range')

    def test_budget_marks_listing_partial(self):
        link = eyelink.Link('osf', 'abc12', 'https://osf.io/abc12/', '')
        result = eyelink.check_repository(FakeWeb(osf_routes({'s1.edf': EYELINK_EDF})), link, eyelink.Budget(per_repository=(2, 60)))
        self.assertEqual(result['status'], 'partial')


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'orchard.sqlite3'

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def service(self, routes):
        self.web = FakeWeb(routes)
        self.s = Service(self.path, MockProvider(), self.web)
        return self.s

    def paper(self, pdf='https://publisher.example/paper.pdf'):
        links = {'fullTextUrlList': {'fullTextUrl': [{'documentStyle': 'pdf', 'availabilityCode': 'OA', 'url': pdf}]}} if pdf else {}
        self.s.import_records('synthetic.json', [record(**links)])
        return self.s.listing()['items'][0]

    def test_pdf_to_osf_to_eyelink_files(self):
        pdf = make_pdf([b'Data availability. Raw eye-tracking data are available on OSF.'], b'https://osf.io/abc12/')
        s = self.service({'https://publisher.example/paper.pdf': pdf, **osf_routes({'s1.edf': EYELINK_EDF, 's1.asc': EYELINK_ASC, 'eeg.edf': EEG_EDF})})
        p = self.paper()
        scan = s.scan_eyelink(p['id'], background=False)
        self.assertEqual((scan['status'], scan['result'], scan['repositories'], scan['eyelink_files']), ('completed', 'eyelink_confirmed', 1, 2))
        files = {f['path']: f['header'] for f in scan['checks'][0]['files']}
        self.assertEqual(files, {'raw/s1.edf': 'eyelink', 'raw/s1.asc': 'eyelink', 'raw/eeg.edf': 'european_data_format'})
        self.assertTrue(Path(scan['pdf_path']).is_file())
        full = s.paper(p['id'])
        self.assertTrue(any(f['origin'] == 'PDF: https://publisher.example/paper.pdf' for f in full['fulltexts']))
        self.assertTrue(any(r['url'] == 'https://osf.io/abc12/' for r in full['resources']))
        self.assertEqual(full['eyelink_scans'][0]['id'], scan['id'])
        self.assertEqual(s.overview()['counts']['eyelink_papers'], 1)
        export = s.export()
        for table in ('eyelink_scans', 'repository_checks', 'repository_files'):
            self.assertTrue(export[table])
        # A rescan reuses the stored PDF text instead of downloading again.
        downloads = sum(u == 'https://publisher.example/paper.pdf' for u, _ in self.web.calls)
        s.scan_eyelink(p['id'], background=False)
        self.assertEqual(sum(u == 'https://publisher.example/paper.pdf' for u, _ in self.web.calls), downloads)

    def test_access_wall_and_no_links_are_recorded(self):
        s = self.service({'https://publisher.example/paper.pdf': b'<html>Sign in</html>'})
        p = self.paper()
        scan = s.scan_eyelink(p['id'], background=False)
        self.assertEqual((scan['status'], scan['result']), ('completed', 'no_repository_links'))
        steps = json.loads(scan['steps'])
        self.assertTrue(any(x['step'] == 'pdf' and x['status'] == 'failed' and 'not a PDF' in x['detail'] for x in steps))

    def test_supplied_pdf_url_and_repository_failure(self):
        pdf = make_pdf([b'Data: https://zenodo.org/records/5555555'], b'https://example.org/')
        s = self.service({'https://mirror.example/p.pdf': pdf})
        p = self.paper(pdf=None)
        with self.assertRaises(ValueError):
            s.scan_eyelink(p['id'], pdf_url='file:///etc/passwd', background=False)
        scan = s.scan_eyelink(p['id'], pdf_url='https://mirror.example/p.pdf', background=False)
        self.assertEqual(scan['result'], 'incomplete')
        self.assertEqual(scan['checks'][0]['status'], 'failed')

    def test_batch_queues_unscanned_papers_once(self):
        s = self.service({})
        self.paper(pdf=None)
        s.scan_eyelink(s.listing()['items'][0]['id'], background=False)
        self.assertEqual(s.scan_eyelink_batch('unscanned')['queued'], 0)
        with self.assertRaises(ValueError):
            s.scan_eyelink_batch('everything')

    def test_http_routes(self):
        import threading, urllib.request
        from orchard.server import make_server
        s = self.service({})
        p = self.paper(pdf=None)
        server = make_server(s, 0); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/'
        try:
            req = urllib.request.Request(base + 'eyelink-scan', data=json.dumps({'id': p['id']}).encode(), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req) as r: scan = json.load(r)
            s.close(); s.pool = __import__('concurrent.futures').futures.ThreadPoolExecutor(1)
            with urllib.request.urlopen(base + 'eyelink') as r: rows = json.load(r)
            with urllib.request.urlopen(base + 'eyelink/' + scan['id']) as r: detail = json.load(r)
            self.assertEqual(rows[0]['id'], scan['id']); self.assertEqual(detail['result'], 'no_repository_links')
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_interrupted_scan_marked_on_restart(self):
        s = self.service({})
        p = self.paper(pdf=None)
        with s.db.connect() as c:
            c.execute("INSERT INTO eyelink_scans(id,paper_id,status,created) VALUES('x',?,'running','now')", (p['id'],))
        s.close(); self.s = Service(self.path, MockProvider(), self.web)
        self.assertEqual(self.s.eyelink_scan('x')['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
