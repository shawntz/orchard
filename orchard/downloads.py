"""Selected-file downloads with sizes shown first, resumable transfers, verification and a manifest.

Nothing is downloaded automatically. The researcher chooses files; Orchard shows their total
size and destination, then streams each file to ``<name>.part`` and renames it only after the
size and any repository checksum match. Interrupted transfers resume with an HTTP Range request
when the server allows it, otherwise they restart from zero (recorded).
"""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.request import Request, build_opener

from .providers import SafeRedirect, public_url

CHUNK = 1 << 20
LARGE = 5_000_000_000  # total bytes that need an explicit second confirmation


def safe_part(value):
    value = re.sub(r'[\x00-\x1f<>:"\\|?*]+', '_', str(value)).strip(' .')
    return value if value not in ('', '.', '..') else '_'


def target_path(root, repository, external_id, path):
    """Destination under ``root`` that mirrors the repository path, without traversal."""
    folder = safe_part(f'{repository}_{external_id}'.replace('/', '_'))
    parts = [safe_part(p) for p in str(path).replace('!/', '/').split('/') if p not in ('', '.', '..')]
    dest = Path(root, folder, *parts).resolve()
    if Path(root).resolve() not in dest.parents:
        raise ValueError('Unsafe destination path.')
    return dest


def git_blob_sha1(path):
    size = os.path.getsize(path)
    h = hashlib.sha1(b'blob %d\0' % size)
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def digest(path, algorithm):
    h = hashlib.new(algorithm)
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def verify(path, expected_size, checksum):
    """Return (status, detail, sha256)."""
    size = os.path.getsize(path)
    sha256 = digest(path, 'sha256')
    if expected_size is not None and size != expected_size:
        return 'failed', f'Size {size:,} bytes differs from the listed {expected_size:,} bytes.', sha256
    if not checksum:
        return 'size_verified', 'Size matches the listing; the repository provides no checksum.', sha256
    algorithm, _, value = checksum.partition(':')
    algorithm = algorithm.lower()
    if algorithm == 'git-blob-sha1':
        actual = git_blob_sha1(path)
    elif algorithm in ('md5', 'sha1', 'sha256', 'sha512'):
        actual = sha256 if algorithm == 'sha256' else digest(path, algorithm)
    else:
        return 'size_verified', f'Unsupported checksum type {algorithm}; size matches.', sha256
    if actual.lower() != value.lower():
        return 'failed', f'{algorithm} checksum mismatch (expected {value}, got {actual}).', sha256
    return 'verified', f'Size and {algorithm} checksum match the repository listing.', sha256


def open_url(url, headers):
    public_url(url)
    req = Request(url, headers={'User-Agent': 'Orchard/0.2 (local research tool)', **headers})
    return build_opener(SafeRedirect()).open(req, timeout=60)


class Downloader:
    """Stream one file. ``opener(url, headers)`` returns a response with .status/.headers/.read(n)."""

    def __init__(self, opener=open_url):
        self.opener = opener

    def fetch(self, url, dest, expected_size=None, progress=None, cancelled=lambda: False, headers=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + '.part')
        offset = part.stat().st_size if part.exists() else 0
        notes = []
        if expected_size is not None and offset > expected_size:
            part.unlink()
            offset = 0
            notes.append('Discarded a partial file larger than the listed size.')
        request_headers = dict(headers or {})
        if offset:
            request_headers['Range'] = f'bytes={offset}-'
        response = self.opener(url, request_headers)
        try:
            status = getattr(response, 'status', None) or response.getcode()
            if offset and status != 206:
                notes.append(f'Server ignored the resume request (HTTP {status}); restarted from the beginning.')
                offset = 0
            mode = 'ab' if offset else 'wb'
            done = offset
            with open(part, mode) as out:
                while True:
                    if cancelled():
                        return {'status': 'cancelled', 'bytes': done, 'notes': notes}
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done)
        finally:
            close = getattr(response, 'close', None)
            if close:
                close()
        if offset:
            notes.append(f'Resumed from byte {offset:,}.')
        os.replace(part, dest)
        return {'status': 'downloaded', 'bytes': done, 'notes': notes}


def manifest(batch, project, rows):
    """Manifest written next to the files and stored in the database."""
    return {'generated_by': 'Orchard', 'batch_id': batch['id'], 'created': batch['created'], 'finished': batch.get('finished'),
            'project': {'repository': project['repository'], 'external_id': project['external_id'], 'url': project['url'],
                        'title': project.get('title'), 'license': project.get('license')},
            'destination': batch['dest'],
            'files': [{'path': r['path'], 'local_path': r['dest'], 'url': r['url'], 'listed_size': r['expected_size'], 'bytes': r['bytes_done'],
                       'repository_checksum': r['expected_checksum'], 'sha256': r['sha256'], 'status': r['status'], 'detail': r['detail']} for r in rows],
            'note': 'Verification confirms the bytes match the repository listing. It does not establish licence terms, '
                    'participant consent for reuse, or that the files parse correctly in eyeris.'}


def write_manifest(dest_dir, data):
    path = Path(dest_dir) / 'orchard-manifest.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)
    return str(path)


def lfs_media_url(github_url):
    """Git LFS objects are served from media.githubusercontent.com, not raw.githubusercontent.com."""
    match = re.match(r'https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.+)$', github_url or '')
    return f'https://media.githubusercontent.com/media/{match.group(1)}/{match.group(2)}/{match.group(3)}' if match else None

