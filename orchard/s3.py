"""Copy downloaded repository files to an Amazon S3 bucket, using only the standard library.

Configuration (environment variables):

``ORCHARD_S3_BUCKET``         bucket name; the feature is off when unset
``ORCHARD_S3_PREFIX``         key prefix (default ``repository-data/``)
``ORCHARD_S3_REGION``         bucket region (falls back to ``AWS_REGION``, ``AWS_DEFAULT_REGION``,
                              the EC2 instance's region, then ``us-east-1``; a wrong guess is
                              corrected from S3's redirect)
``ORCHARD_S3_STORAGE_CLASS``  optional, e.g. ``INTELLIGENT_TIERING``
``ORCHARD_S3_ENDPOINT``       optional S3-compatible endpoint (path-style), e.g. MinIO
``ORCHARD_S3_KEEP_LOCAL``     ``1`` keeps the local staging copy after a verified upload

Credentials come from the usual AWS sources, in order: ``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``; the shared credentials file
(``AWS_SHARED_CREDENTIALS_FILE`` or ``~/.aws/credentials``, profile ``AWS_PROFILE``); the ECS
container endpoint; the EC2 instance role (IMDSv2). Requests are signed with Signature V4 and the
SHA-256 of every body is signed, so S3 rejects any upload whose bytes changed in transit.
"""
import configparser
import hashlib
import hmac
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
PART = 16 << 20            # multipart part size; S3 allows at most 10,000 parts
MAX_PARTS = 10_000
IMDS = 'http://169.254.169.254'
ECS = 'http://169.254.170.2'


class S3Error(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


def env(*names, default=None):
    for name in names:
        value = (os.environ.get(name) or '').strip()
        if value:
            return value
    return default


class Config:
    def __init__(self, bucket, prefix='repository-data/', region=None, endpoint=None, storage_class=None, keep_local=False):
        self.bucket = bucket
        self.prefix = (prefix or '').lstrip('/')
        if self.prefix and not self.prefix.endswith('/'):
            self.prefix += '/'
        self.region, self.endpoint, self.storage_class, self.keep_local = region, (endpoint or '').rstrip('/') or None, storage_class, keep_local

    @classmethod
    def from_env(cls):
        bucket = env('ORCHARD_S3_BUCKET')
        if not bucket:
            return None
        if not re.fullmatch(r'[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]', bucket):
            raise ValueError(f'ORCHARD_S3_BUCKET "{bucket}" is not a valid S3 bucket name.')
        return cls(bucket, env('ORCHARD_S3_PREFIX', default='repository-data/'), env('ORCHARD_S3_REGION', 'AWS_REGION', 'AWS_DEFAULT_REGION'),
                   env('ORCHARD_S3_ENDPOINT'), env('ORCHARD_S3_STORAGE_CLASS'), env('ORCHARD_S3_KEEP_LOCAL', default='0').lower() in ('1', 'true', 'yes'))

    def uri(self, key=''):
        return f's3://{self.bucket}/{key}'

    def describe(self):
        return {'configured': True, 'bucket': self.bucket, 'prefix': self.prefix, 'destination': self.uri(self.prefix),
                'region': self.region, 'endpoint': self.endpoint, 'storage_class': self.storage_class, 'keep_local': self.keep_local}


def describe():
    """Settings shown in the interface; never includes credentials."""
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        return {'configured': False, 'error': str(exc)}
    return cfg.describe() if cfg else {'configured': False}


# --------------------------------------------------------------------------- HTTP transport

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = build_opener(_NoRedirect())


def http(method, url, headers, body=b'', timeout=120):
    """Return (status, lower-cased headers, body). Never raises for HTTP status codes."""
    req = Request(url, data=body if method in ('PUT', 'POST') else None, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b''


# --------------------------------------------------------------------------- credentials

class Credentials:
    def __init__(self, key, secret, token=None, expires=None, source=''):
        self.key, self.secret, self.token, self.expires, self.source = key, secret, token, expires, source

    def fresh(self):
        return self.expires is None or self.expires - time.time() > 300


def _expiry(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return time.time() + 900  # unknown format: refresh again soon


class CredentialChain:
    def __init__(self, transport=http):
        self.transport = transport
        self._cached = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._cached and self._cached.fresh():
                return self._cached
            for source in (self.from_env, self.from_file, self.from_ecs, self.from_imds):
                try:
                    creds = source()
                except (OSError, ValueError, KeyError, configparser.Error):
                    creds = None
                if creds:
                    self._cached = creds
                    return creds
        raise S3Error('No AWS credentials found. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, configure ~/.aws/credentials, '
                      'or attach an IAM role with s3:PutObject on the bucket to this instance.')

    def from_env(self):
        key, secret = env('AWS_ACCESS_KEY_ID'), env('AWS_SECRET_ACCESS_KEY')
        return Credentials(key, secret, env('AWS_SESSION_TOKEN'), source='environment') if key and secret else None

    def from_file(self):
        path = Path(env('AWS_SHARED_CREDENTIALS_FILE', default=str(Path.home() / '.aws' / 'credentials'))).expanduser()
        if not path.is_file():
            return None
        parser = configparser.ConfigParser()
        parser.read(path)
        profile = env('AWS_PROFILE', default='default')
        if not parser.has_section(profile):
            return None
        sec = parser[profile]
        key, secret = sec.get('aws_access_key_id'), sec.get('aws_secret_access_key')
        return Credentials(key.strip(), secret.strip(), (sec.get('aws_session_token') or '').strip() or None, source=f'{path} [{profile}]') if key and secret else None

    def from_ecs(self):
        rel, full = env('AWS_CONTAINER_CREDENTIALS_RELATIVE_URI'), env('AWS_CONTAINER_CREDENTIALS_FULL_URI')
        if not (rel or full):
            return None
        headers = {'Authorization': env('AWS_CONTAINER_AUTHORIZATION_TOKEN')} if env('AWS_CONTAINER_AUTHORIZATION_TOKEN') else {}
        status, _, body = self.transport('GET', full or ECS + rel, headers, timeout=5)
        if status != 200:
            return None
        d = json.loads(body)
        return Credentials(d['AccessKeyId'], d['SecretAccessKey'], d.get('Token'), _expiry(d.get('Expiration')), 'container role')

    def imds_token(self):
        status, _, body = self.transport('PUT', IMDS + '/latest/api/token', {'X-aws-ec2-metadata-token-ttl-seconds': '21600'}, timeout=2)
        return body.decode() if status == 200 else None

    def from_imds(self):
        if env('AWS_EC2_METADATA_DISABLED', default='').lower() == 'true':
            return None
        token = self.imds_token()
        if not token:
            return None
        h = {'X-aws-ec2-metadata-token': token}
        status, _, body = self.transport('GET', IMDS + '/latest/meta-data/iam/security-credentials/', h, timeout=2)
        if status != 200 or not body.strip():
            return None
        role = body.decode().split()[0]
        status, _, body = self.transport('GET', IMDS + '/latest/meta-data/iam/security-credentials/' + role, h, timeout=2)
        if status != 200:
            return None
        d = json.loads(body)
        return Credentials(d['AccessKeyId'], d['SecretAccessKey'], d.get('Token'), _expiry(d.get('Expiration')), f'instance role {role}')

    def region(self):
        try:
            token = self.imds_token()
            if token:
                status, _, body = self.transport('GET', IMDS + '/latest/meta-data/placement/region', {'X-aws-ec2-metadata-token': token}, timeout=2)
                if status == 200 and body.strip():
                    return body.decode().strip()
        except OSError:
            pass
        return None


# --------------------------------------------------------------------------- Signature V4

def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def uri_encode(value, slash=True):
    return quote(value, safe='-_.~' + ('/' if slash else ''))


def sign(method, url, headers, payload_hash, creds, region, when=None, service='s3'):
    """Return ``headers`` plus x-amz-date, x-amz-content-sha256, security token and Authorization."""
    when = when or datetime.now(timezone.utc)
    amz_date, day = when.strftime('%Y%m%dT%H%M%SZ'), when.strftime('%Y%m%d')
    parts = urlsplit(url)
    out = {**headers, 'x-amz-date': amz_date, 'x-amz-content-sha256': payload_hash}
    if creds.token:
        out['x-amz-security-token'] = creds.token
    out['host'] = parts.netloc
    query = []
    for pair in filter(None, parts.query.split('&')):
        k, _, v = pair.partition('=')
        query.append((uri_encode(unquote(k), False), uri_encode(unquote(v), False)))
    canonical_query = '&'.join(f'{k}={v}' for k, v in sorted(query))
    canon = {k.lower().strip(): re.sub(r'\s+', ' ', str(v).strip()) for k, v in out.items()}
    signed = ';'.join(sorted(canon))
    canonical_headers = ''.join(f'{k}:{canon[k]}\n' for k in sorted(canon))
    path = uri_encode(unquote(parts.path or '/'))
    request = '\n'.join([method, path, canonical_query, canonical_headers, signed, payload_hash])
    scope = f'{day}/{region}/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(('AWS4' + creds.secret).encode(), day), region), service), 'aws4_request')
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out['Authorization'] = f'AWS4-HMAC-SHA256 Credential={creds.key}/{scope}, SignedHeaders={signed}, Signature={signature}'
    out.pop('host')  # urllib sets Host itself from the URL; it is still signed.
    return out


# --------------------------------------------------------------------------- client

def _error(status, body):
    try:
        root = ET.fromstring(body)
        code, message = root.findtext('Code'), root.findtext('Message')
    except ET.ParseError:
        code, message = None, body[:200].decode('utf-8', 'replace')
    return S3Error(f'S3 HTTP {status}' + (f' {code}' if code else '') + (f': {message}' if message else ''), status, code)


def _xml_text(body, tag):
    root = ET.fromstring(body)
    for el in root.iter():
        if el.tag.split('}')[-1] == tag:
            return el.text
    return None


class Client:
    def __init__(self, config, transport=http, credentials=None, sleeper=time.sleep):
        self.config = config
        self.transport = transport
        self.credentials = credentials or CredentialChain(transport)
        self.sleeper = sleeper
        self._region = config.region

    @property
    def region(self):
        if not self._region:
            self._region = (None if self.config.endpoint else self.credentials.region()) or 'us-east-1'
        return self._region

    def url(self, key='', query=''):
        bucket, path = self.config.bucket, uri_encode(key)
        if self.config.endpoint:
            base = f'{self.config.endpoint}/{bucket}/{path}'
        elif '.' in bucket:
            base = f'https://s3.{self.region}.amazonaws.com/{bucket}/{path}'
        else:
            base = f'https://{bucket}.s3.{self.region}.amazonaws.com/{path}'
        return base + ('?' + query if query else '')

    def request(self, method, key='', query='', body=b'', headers=None, ok=(200,)):
        payload = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        headers = dict(headers or {})
        if body and not any(k.lower() == 'content-type' for k in headers):
            headers['Content-Type'] = 'application/octet-stream'  # urllib would otherwise label the bytes as a form
        last = None
        for attempt in range(4):
            url = self.url(key, query)
            signed = sign(method, url, headers, payload, self.credentials.get(), self.region)
            try:
                status, rh, rb = self.transport(method, url, signed, body)
            except (URLError, OSError) as exc:
                last = S3Error(f'Could not reach S3: {exc}')
                self.sleeper(2 ** attempt)
                continue
            if status in ok:
                return status, rh, rb
            region = rh.get('x-amz-bucket-region')
            if status in (301, 307, 400) and region and region != self._region and not self.config.endpoint:
                self._region = region  # bucket lives elsewhere; re-sign for its region
                continue
            last = _error(status, rb)
            if status >= 500 or status == 429 or last.code in ('RequestTimeout', 'SlowDown', 'ExpiredToken'):
                if last.code == 'ExpiredToken':
                    self.credentials._cached = None
                self.sleeper(2 ** attempt)
                continue
            raise last
        raise last

    def head(self, key):
        status, headers, _ = self.request('HEAD', key, ok=(200, 403, 404))
        return headers if status == 200 else None

    def object_headers(self, sha256, metadata=None, content_type='application/octet-stream'):
        h = {'Content-Type': content_type, 'x-amz-meta-sha256': sha256}
        for k, v in (metadata or {}).items():
            h['x-amz-meta-' + k] = re.sub(r'[^\x20-\x7e]', '?', str(v))[:1024]
        if self.config.storage_class:
            h['x-amz-storage-class'] = self.config.storage_class
        return h

    def put_bytes(self, key, data, content_type='application/json', metadata=None):
        self.request('PUT', key, body=data, headers=self.object_headers(hashlib.sha256(data).hexdigest(), metadata, content_type))

    def upload_file(self, path, key, sha256=None, metadata=None, progress=None, cancelled=lambda: False):
        """Upload a local file. Returns {'status': 'uploaded'|'already_present'|'cancelled', 'bytes', 'parts', 'etag'}."""
        size = os.path.getsize(path)
        sha256 = sha256 or _file_sha256(path)
        existing = self.head(key)
        if existing and existing.get('x-amz-meta-sha256') == sha256 and int(existing.get('content-length', -1)) == size:
            return {'status': 'already_present', 'bytes': size, 'parts': 0, 'etag': existing.get('etag')}
        headers = self.object_headers(sha256, metadata)
        if size <= PART:
            with open(path, 'rb') as f:
                data = f.read()
            if cancelled():
                return {'status': 'cancelled', 'bytes': 0, 'parts': 0, 'etag': None}
            _, rh, _ = self.request('PUT', key, body=data, headers={**headers, 'Content-Length': str(size)})
            if progress:
                progress(size)
            return {'status': 'uploaded', 'bytes': size, 'parts': 1, 'etag': rh.get('etag')}
        part_size = max(PART, -(-size // MAX_PARTS))
        _, _, body = self.request('POST', key, 'uploads=', headers=headers)
        upload_id = _xml_text(body, 'UploadId')
        if not upload_id:
            raise S3Error('S3 did not return a multipart UploadId.')
        q = 'uploadId=' + uri_encode(upload_id, False)
        etags, done = [], 0
        try:
            with open(path, 'rb') as f:
                number = 0
                while True:
                    chunk = f.read(part_size)
                    if not chunk:
                        break
                    if cancelled():
                        self.request('DELETE', key, q, ok=(204, 200, 404))
                        return {'status': 'cancelled', 'bytes': done, 'parts': number, 'etag': None}
                    number += 1
                    _, rh, _ = self.request('PUT', key, f'partNumber={number}&{q}', body=chunk, headers={'Content-Length': str(len(chunk))})
                    etags.append(rh.get('etag'))
                    done += len(chunk)
                    if progress:
                        progress(done)
            xml = '<CompleteMultipartUpload>' + ''.join(f'<Part><PartNumber>{i}</PartNumber><ETag>{e}</ETag></Part>' for i, e in enumerate(etags, 1)) + '</CompleteMultipartUpload>'
            _, _, body = self.request('POST', key, q, body=xml.encode(), headers={'Content-Type': 'application/xml'})
            if b'<Error>' in body:  # S3 can report a failed completion inside an HTTP 200
                raise _error(200, body)
            return {'status': 'uploaded', 'bytes': done, 'parts': len(etags), 'etag': _xml_text(body, 'ETag')}
        except Exception:
            try:
                self.request('DELETE', key, q, ok=(204, 200, 404))
            except Exception:
                pass
            raise


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()
