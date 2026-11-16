"""Copying repository project files to an S3 bucket: signing, credentials, uploads and the project workflow.

Offline: S3 is an in-memory fake that checks every signed payload hash, like S3 does.
"""
import hashlib
import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from repository_fixtures import ASC
from test_repository_projects import Inspected
from orchard import discovery as D, s3

KEY, SECRET = 'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
S3_ENV = ('ORCHARD_S3_BUCKET', 'ORCHARD_S3_PREFIX', 'ORCHARD_S3_REGION', 'ORCHARD_S3_ENDPOINT', 'ORCHARD_S3_STORAGE_CLASS', 'ORCHARD_S3_KEEP_LOCAL',
          'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_REGION',
          'AWS_DEFAULT_REGION', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI')


class FakeS3:
    """Enough of S3 for PutObject, HeadObject and multipart uploads, path- or host-style."""

    def __init__(self, bucket='lab-data', region='us-west-2'):
        self.bucket, self.region = bucket, region
        self.objects, self.uploads, self.calls = {}, {}, []
        self.fail_puts = 0

    def key(self, url):
        parts = urlsplit(url)
        path = unquote(parts.path)
        if parts.netloc.startswith(self.bucket + '.'):
            return path.lstrip('/')
        return path.lstrip('/').split('/', 1)[1]

    def __call__(self, method, url, headers, body=b'', timeout=None):
        self.calls.append((method, url))
        h = {k.lower(): v for k, v in headers.items()}
        assert h['authorization'].startswith(f'AWS4-HMAC-SHA256 Credential={KEY}/'), h['authorization']
        if f'/{self.region}/s3/' not in h['authorization']:
            return 301, {'x-amz-bucket-region': self.region}, b'<Error><Code>PermanentRedirect</Code></Error>'
        if h['x-amz-content-sha256'] != hashlib.sha256(body or b'').hexdigest():
            return 400, {}, b'<Error><Code>XAmzContentSHA256Mismatch</Code></Error>'
        key, q = self.key(url), parse_qs(urlsplit(url).query, keep_blank_values=True)
        meta = {k: v for k, v in h.items() if k.startswith('x-amz-meta-') or k == 'x-amz-storage-class'}
        if method == 'HEAD':
            obj = self.objects.get(key)
            return (200, {'content-length': str(len(obj['body'])), **obj['meta']}, b'') if obj else (404, {}, b'')
        if method == 'PUT' and 'uploadId' in q:
            self.uploads[q['uploadId'][0]]['parts'][int(q['partNumber'][0])] = body
            return 200, {'etag': '"%s"' % hashlib.md5(body).hexdigest()}, b''
        if method == 'PUT':
            if self.fail_puts:
                self.fail_puts -= 1
                return 403, {}, b'<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>'
            self.objects[key] = {'body': body, 'meta': meta}
            return 200, {'etag': '"x"'}, b''
        if method == 'POST' and 'uploads' in q:
            uid = 'up%d' % len(self.uploads)
            self.uploads[uid] = {'key': key, 'parts': {}, 'meta': meta}
            return 200, {}, f'<InitiateMultipartUploadResult><UploadId>{uid}</UploadId></InitiateMultipartUploadResult>'.encode()
        if method == 'POST' and 'uploadId' in q:
            up = self.uploads.pop(q['uploadId'][0])
            numbers = [int(p.findtext('PartNumber')) for p in ET.fromstring(body).iter('Part')]
            self.objects[key] = {'body': b''.join(up['parts'][n] for n in numbers), 'meta': up['meta']}
            return 200, {}, b'<CompleteMultipartUploadResult><ETag>"m-2"</ETag></CompleteMultipartUploadResult>'
        if method == 'DELETE' and 'uploadId' in q:
            self.uploads.pop(q['uploadId'][0], None)
            return 204, {}, b''
        return 400, {}, b'<Error><Code>Unsupported</Code></Error>'


class Static:
    def __init__(self, creds):
        self.creds, self._cached = creds, None

    def get(self):
        return self.creds

    def region(self):
        return None


def client(fake, **kw):
    cfg = s3.Config(fake.bucket, kw.pop('prefix', 'repository-data/'), kw.pop('region', fake.region), **kw)
    return s3.Client(cfg, fake, Static(s3.Credentials(KEY, SECRET)), sleeper=lambda t: None)


class Signing(unittest.TestCase):
    """The three signed examples published in the AWS S3 Signature Version 4 documentation."""
    when = datetime(2013, 5, 24, tzinfo=timezone.utc)
    creds = s3.Credentials(KEY, SECRET)

    def sig(self, *args):
        return self.sign(*args)['Authorization'].rsplit('Signature=', 1)[1]

    def sign(self, method, url, headers, payload=s3.EMPTY_SHA256):
        return s3.sign(method, url, headers, payload, self.creds, 'us-east-1', self.when)

    def test_get_object(self):
        self.assertEqual(self.sig('GET', 'https://examplebucket.s3.amazonaws.com/test.txt', {'Range': 'bytes=0-9'}),
                         'f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41')

    def test_put_object(self):
        payload = hashlib.sha256(b'Welcome to Amazon S3.').hexdigest()
        self.assertEqual(self.sig('PUT', 'https://examplebucket.s3.amazonaws.com/test%24file.text',
                                  {'Date': 'Fri, 24 May 2013 00:00:00 GMT', 'x-amz-storage-class': 'REDUCED_REDUNDANCY'}, payload),
                         '98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd')

    def test_list_with_query(self):
        self.assertEqual(self.sig('GET', 'https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J', {}),
                         '34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7')

    def test_session_token_is_signed(self):
        h = s3.sign('GET', 'https://b.s3.amazonaws.com/k', {}, s3.EMPTY_SHA256, s3.Credentials(KEY, SECRET, 'TOKEN'), 'us-east-1', self.when)
        self.assertEqual(h['x-amz-security-token'], 'TOKEN')
        self.assertIn('x-amz-security-token', h['Authorization'])


class Credentials(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {k: '' for k in S3_ENV})
        self.env.start()
        self.addCleanup(self.env.stop)

    def no_network(self, *a, **kw):
        raise OSError('no metadata service')

    def test_environment_first(self):
        os.environ.update(AWS_ACCESS_KEY_ID='AK', AWS_SECRET_ACCESS_KEY='SK', AWS_SESSION_TOKEN='T')
        c = s3.CredentialChain(self.no_network).get()
        self.assertEqual((c.key, c.secret, c.token, c.source), ('AK', 'SK', 'T', 'environment'))

    def test_shared_file_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'credentials')
            path.write_text('[default]\naws_access_key_id=A\naws_secret_access_key=B\n[lab]\naws_access_key_id = C\naws_secret_access_key = D\n')
            os.environ.update(AWS_SHARED_CREDENTIALS_FILE=str(path), AWS_PROFILE='lab')
            c = s3.CredentialChain(self.no_network).get()
        self.assertEqual((c.key, c.secret), ('C', 'D'))

    def test_instance_role_via_imdsv2_and_refresh(self):
        seen = []

        def imds(method, url, headers, body=b'', timeout=None):
            seen.append((method, url, dict(headers)))
            if url.endswith('/api/token'):
                return 200, {}, b'tok'
            assert headers.get('X-aws-ec2-metadata-token') == 'tok'
            if url.endswith('/security-credentials/'):
                return 200, {}, b'orchard-ec2\n'
            if url.endswith('/orchard-ec2'):
                return 200, {}, json.dumps({'AccessKeyId': 'ASIA' + str(len(seen)), 'SecretAccessKey': 's', 'Token': 't', 'Expiration': '2000-01-01T00:00:00Z'}).encode()
            if url.endswith('/placement/region'):
                return 200, {}, b'us-west-2'
            return 404, {}, b''
        os.environ['AWS_SHARED_CREDENTIALS_FILE'] = '/nonexistent'
        chain = s3.CredentialChain(imds)
        first = chain.get()
        self.assertEqual((first.token, first.source), ('t', 'instance role orchard-ec2'))
        self.assertEqual(seen[0][0], 'PUT')  # IMDSv2 session token first
        self.assertNotEqual(chain.get().key, first.key, 'expired role credentials are fetched again')
        self.assertEqual(chain.region(), 'us-west-2')

    def test_no_credentials_is_a_clear_error(self):
        os.environ['AWS_SHARED_CREDENTIALS_FILE'] = '/nonexistent'
        with self.assertRaisesRegex(s3.S3Error, 'No AWS credentials found'):
            s3.CredentialChain(self.no_network).get()

    def test_config_from_environment(self):
        self.assertIsNone(s3.Config.from_env())
        self.assertEqual(s3.describe(), {'configured': False})
        os.environ.update(ORCHARD_S3_BUCKET='lab-data', ORCHARD_S3_PREFIX='/eyelink', ORCHARD_S3_KEEP_LOCAL='1')
        cfg = s3.Config.from_env()
        self.assertEqual((cfg.prefix, cfg.keep_local, cfg.uri('x')), ('eyelink/', True, 's3://lab-data/x'))
        os.environ['ORCHARD_S3_BUCKET'] = 'Bad_Bucket'
        self.assertIn('not a valid', s3.describe()['error'])


class Uploads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeS3()

    def file(self, data):
        path = Path(self.tmp.name, 'f.bin')
        path.write_bytes(data)
        return path

    def test_single_put_with_metadata_then_skip_identical(self):
        c = client(self.fake, storage_class='INTELLIGENT_TIERING')
        path = self.file(b'abc' * 100)
        r = c.upload_file(path, 'repository-data/osf_x/a b.asc', metadata={'source-path': 'a b.asc'})
        self.assertEqual((r['status'], r['parts']), ('uploaded', 1))
        obj = self.fake.objects['repository-data/osf_x/a b.asc']
        self.assertEqual(obj['body'], b'abc' * 100)
        self.assertEqual(obj['meta']['x-amz-meta-sha256'], hashlib.sha256(b'abc' * 100).hexdigest())
        self.assertEqual(obj['meta']['x-amz-storage-class'], 'INTELLIGENT_TIERING')
        self.assertEqual(c.upload_file(path, 'repository-data/osf_x/a b.asc')['status'], 'already_present')
        self.assertTrue(self.fake.calls[1][1].startswith('https://lab-data.s3.us-west-2.amazonaws.com/repository-data/osf_x/a%20b.asc'))

    def test_multipart_for_large_files(self):
        data = os.urandom(1000) * 40
        with mock.patch.object(s3, 'PART', 16_000):
            r = client(self.fake).upload_file(self.file(data), 'k')
        self.assertEqual((r['status'], r['parts']), ('uploaded', 3))
        self.assertEqual(self.fake.objects['k']['body'], data)
        self.assertEqual(self.fake.uploads, {})

    def test_cancel_aborts_multipart(self):
        with mock.patch.object(s3, 'PART', 16_000):
            r = client(self.fake).upload_file(self.file(b'x' * 40_000), 'k', cancelled=lambda: True)
        self.assertEqual(r['status'], 'cancelled')
        self.assertNotIn('k', self.fake.objects)
        self.assertEqual(self.fake.uploads, {})
        self.assertEqual(self.fake.calls[-1][0], 'DELETE')

    def test_wrong_region_is_corrected_from_redirect(self):
        c = client(self.fake, region='us-east-1')
        c.put_bytes('m.json', b'{}')
        self.assertIn('m.json', self.fake.objects)
        self.assertEqual(c.region, 'us-west-2')

    def test_dotted_bucket_and_custom_endpoint_use_path_style(self):
        self.assertEqual(s3.Client(s3.Config('lab.data', region='eu-west-1'), self.fake, Static(None)).url('a/b'),
                         'https://s3.eu-west-1.amazonaws.com/lab.data/a/b')
        self.assertEqual(s3.Client(s3.Config('lab', region='x', endpoint='http://minio:9000/'), self.fake, Static(None)).url('a'),
                         'http://minio:9000/lab/a')

    def test_access_denied_raises(self):
        self.fake.fail_puts = 1
        with self.assertRaisesRegex(s3.S3Error, 'AccessDenied'):
            client(self.fake).put_bytes('k', b'1')


class ProjectTransfer(Inspected):
    """The 'Copy all files to S3' button on a repository project."""

    def setUp(self):
        super().setUp()
        self.s3env = mock.patch.dict(os.environ, {**{k: '' for k in S3_ENV}, 'ORCHARD_S3_BUCKET': 'lab-data', 'ORCHARD_S3_REGION': 'us-west-2'})
        self.s3env.start()
        self.addCleanup(self.s3env.stop)
        self.fake = FakeS3()
        self.s.s3_client = client(self.fake)
        pid = self.ctd['project']['id']
        self.pid = pid
        # Give every file a correct checksum so verification is real.
        with self.s.db.connect() as c:
            for f in self.ctd['files']:
                reply = self.web.match(f['url'], {}) if f['url'] else None
                if reply is not None and f['download'] == 'public':
                    c.execute('UPDATE sweep_files SET checksum=? WHERE id=?', ('sha256:' + hashlib.sha256(reply.body).hexdigest(), f['id']))

    def test_all_files_planned_with_bucket_keys(self):
        plan = D.download_plan(self.s, self.pid, 'all', 's3')
        downloadable = [f for f in self.ctd['files'] if f['download'] in ('public', 'lfs_object')]
        self.assertEqual(len(plan['files']), len(downloadable))
        self.assertEqual(plan['destination'], 's3://lab-data/repository-data/osf_ctd34/')
        f = next(x for x in plan['files'] if x['path'] == 'Experiment 1/Pupil_Data/pp02.asc')
        self.assertEqual(f['remote'], 's3://lab-data/repository-data/osf_ctd34/Experiment 1/Pupil_Data/pp02.asc')
        self.assertTrue(D.project_detail(self.s, self.pid)['storage']['configured'])

    def test_copy_all_to_s3_verifies_uploads_and_writes_manifest(self):
        batch = D.download(self.s, self.pid, 'all', reviewer='Ashna', background=False, target='s3')
        self.assertEqual(batch['status'], 'completed', [(i['path'], i['status'], i['detail'], i['upload_detail']) for i in batch['items']])
        self.assertTrue(all(i['uploaded'] and i['status'] == 'verified' for i in batch['items']))
        pp02 = self.fake.objects['repository-data/osf_ctd34/Experiment 1/Pupil_Data/pp02.asc']
        self.assertEqual(pp02['body'], ASC)
        self.assertEqual(pp02['meta']['x-amz-meta-repository'], 'osf')
        self.assertFalse(any(Path(i['dest']).exists() for i in batch['items']), 'staging copies removed after upload')
        manifest = json.loads(self.fake.objects['repository-data/osf_ctd34/orchard-manifest.json']['body'])
        self.assertEqual(manifest['destination'], 's3://lab-data/repository-data/osf_ctd34/')
        self.assertTrue(all(f['s3_uri'].startswith('s3://lab-data/repository-data/osf_ctd34/') and f['uploaded'] for f in manifest['files']))
        self.assertTrue(batch['manifest_path'].startswith('s3://lab-data/'))
        event = self.s.db.one("SELECT detail FROM audit WHERE event='repository_download_completed' ORDER BY id DESC")
        self.assertEqual(json.loads(event['detail'])['uploaded'], len(batch['items']))

    def test_failed_checksum_is_never_uploaded(self):
        f = next(x for x in self.ctd['files'] if x['path'] == 'Experiment 1/Pupil_Data/pp02.asc')
        with self.s.db.connect() as c:
            c.execute('UPDATE sweep_files SET checksum=? WHERE id=?', ('sha256:' + 'b' * 64, f['id']))
        batch = D.download(self.s, self.pid, [f['id']], background=False, target='s3')
        self.assertEqual((batch['status'], batch['items'][0]['status'], batch['items'][0]['uploaded']), ('partial', 'failed', None))
        self.assertFalse(any(k.endswith('pp02.asc') for k in self.fake.objects))

    def test_failed_upload_resumes_without_downloading_again(self):
        f = next(x for x in self.ctd['files'] if x['path'] == 'Experiment 1/Pupil_Data/pp02.asc')
        self.fake.fail_puts = 1
        batch = D.download(self.s, self.pid, [f['id']], background=False, target='s3')
        item = batch['items'][0]
        self.assertEqual((batch['status'], item['status'], item['uploaded']), ('partial', 'verified', None))
        self.assertIn('AccessDenied', item['upload_detail'])
        self.assertTrue(Path(item['dest']).exists(), 'verified local copy kept for the retry')
        fetched = len(self.web.calls)
        batch = D.download_action(self.s, batch['id'], 'resume', background=False)
        self.assertEqual(batch['status'], 'completed')
        self.assertEqual(len(self.web.calls), fetched, 'resume re-uploads the staged file instead of downloading it again')
        self.assertIn('pp02.asc', ' '.join(self.fake.objects))

    def test_keep_local_copy_when_configured(self):
        f = next(x for x in self.ctd['files'] if x['path'] == 'Experiment 1/Pupil_Data/pp02.asc')
        os.environ['ORCHARD_S3_KEEP_LOCAL'] = '1'
        batch = D.download(self.s, self.pid, [f['id']], background=False, target='s3')
        self.assertTrue(Path(batch['items'][0]['dest']).exists())

    def test_not_configured_explains_how(self):
        os.environ['ORCHARD_S3_BUCKET'] = ''
        with self.assertRaisesRegex(ValueError, 'ORCHARD_S3_BUCKET'):
            D.download_plan(self.s, self.pid, 'all', 's3')
        self.assertEqual(D.project_detail(self.s, self.pid)['storage'], {'configured': False})

    def test_local_downloads_unchanged(self):
        f = next(x for x in self.ctd['files'] if x['path'] == 'Experiment 1/Pupil_Data/pp02.asc')
        batch = D.download(self.s, self.pid, [f['id']], background=False)
        self.assertEqual((batch['status'], batch['target'], batch['items'][0]['uploaded']), ('completed', 'local', None))
        self.assertTrue(Path(batch['items'][0]['dest']).exists())
        self.assertEqual(self.fake.calls, [])

    def test_http_route_accepts_target(self):
        import threading, urllib.request
        from orchard.server import make_server
        server = make_server(self.s, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        req = urllib.request.Request(f'http://127.0.0.1:{port}/api/discovery/download-plan', json.dumps({'project_id': self.pid, 'file_ids': 'all', 'target': 's3'}).encode(),
                                     {'Content-Type': 'application/json', 'Host': f'127.0.0.1:{port}'})
        plan = json.loads(urllib.request.urlopen(req).read())
        self.assertEqual(plan['target'], 's3')
        self.assertTrue(plan['destination'].startswith('s3://lab-data/'))


if __name__ == '__main__':
    unittest.main()
