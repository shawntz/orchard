"""Find EyeLink recordings: PDF text/links, data-repository detection, and remote file listing.

Everything here is dependency-free and works through a ``Transport``-like object
(``get(url, limit=..., headers=..., truncate=...)``), so it can be exercised offline.
Nothing is downloaded beyond PDFs, API listings and small byte ranges used to read
archive directories and file headers.
"""
import json
import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, parse_qs

PDF_LIMIT = 60_000_000
EYELINK_KINDS = ('edf', 'asc')


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Request and time limits for one scan, so a huge repository cannot stall the app.

    ``start_repository`` sets a smaller allowance per repository, leaving room for the others.
    """

    def __init__(self, requests=400, seconds=900, archives=25, probes=12, per_repository=(150, 240)):
        self.requests, self.archives, self.probes, self.per_repository = requests, archives, probes, per_repository
        self.deadline = time.monotonic() + seconds
        self.start_repository()

    def start_repository(self, requests=None, seconds=None):
        self.repo_requests = requests or self.per_repository[0]
        self.repo_deadline = min(self.deadline, time.monotonic() + (seconds or self.per_repository[1]))

    def spend(self):
        if self.requests <= 0 or time.monotonic() > self.deadline:
            raise BudgetExceeded('Request/time limit for this scan reached; listing is incomplete.')
        if self.repo_requests <= 0 or time.monotonic() > self.repo_deadline:
            raise BudgetExceeded('Request/time limit for this repository reached; listing is incomplete.')
        self.requests -= 1
        self.repo_requests -= 1


def lower_headers(headers):
    return {str(k).lower(): v for k, v in (headers or {}).items()}


# --------------------------------------------------------------------------- PDF


def pdf_literal(raw):
    """Decode a PDF literal string body (without the outer parentheses)."""
    out, i = bytearray(), 0
    escapes = {ord('n'): 10, ord('r'): 13, ord('t'): 9, ord('b'): 8, ord('f'): 12}
    while i < len(raw):
        ch = raw[i]
        if ch != 0x5C:
            out.append(ch); i += 1; continue
        i += 1
        if i >= len(raw):
            break
        ch = raw[i]
        if ch in escapes:
            out.append(escapes[ch]); i += 1
        elif 0x30 <= ch <= 0x37:
            digits = re.match(rb'[0-7]{1,3}', raw[i:i + 3]).group(0)
            out.append(int(digits, 8) & 0xFF); i += len(digits)
        elif ch in (10, 13):
            i += 1 + (ch == 13 and raw[i + 1:i + 2] == b'\n')
        else:
            out.append(ch); i += 1
    if out[:2] == b'\xfe\xff':
        return out[2:].decode('utf-16-be', 'replace')
    return out.decode('latin-1')


def pdf_streams(data, total_limit=200_000_000):
    """Yield decompressed stream bodies; output is bounded to resist compression bombs."""
    used = 0
    for m in re.finditer(rb'stream\r?\n', data):
        end = data.find(b'endstream', m.end())
        if end < 0 or used >= total_limit:
            continue
        try:
            body = zlib.decompressobj().decompress(data[m.end():end], total_limit - used)
        except zlib.error:
            continue
        used += len(body)
        yield body


STRING = rb'\(((?:\\.|[^\\)])*)\)'
TEXT_OPS = re.compile(rb'\[((?:\\.|[^\]\\])*)\]\s*TJ|' + STRING + rb'\s*(?:Tj|\'|")|\b(T\*|TD|Td|Tm|ET)\b', re.S)


def content_text(stream):
    pieces = []
    for m in TEXT_OPS.finditer(stream):
        if m.group(1) is not None:
            for part in re.finditer(STRING + rb'|(-?\d+(?:\.\d+)?)', m.group(1), re.S):
                if part.group(1) is not None:
                    pieces.append(pdf_literal(part.group(1)))
                elif float(part.group(2)) < -200:
                    pieces.append(' ')
        elif m.group(2) is not None:
            pieces.append(pdf_literal(m.group(2)))
        else:
            pieces.append(' ' if m.group(3) in (b'Td', b'Tm') else '\n')
    text = ''.join(pieces)
    # Strings drawn with CID fonts decode as binary noise; keep only readable output.
    printable = sum(c.isprintable() or c.isspace() for c in text)
    return text if text and printable / len(text) > 0.9 else ''


def pdf_text(data):
    """Return (text, uris). Uses pypdf when installed; otherwise a built-in best-effort reader.

    Link annotations (/URI) are read from the raw file and from compressed object streams,
    which is where most publishers put clickable repository links.
    """
    if b'%PDF-' not in data[:1024]:
        raise ValueError('Downloaded file is not a PDF (often a login, cookie, or bot-check page).')
    uris, texts = [], []
    chunks = [data]
    for body in pdf_streams(data):
        chunks.append(body)
        if b'BT' in body:
            texts.append(content_text(body))
    for chunk in chunks:
        for m in re.finditer(rb'/URI\s*' + STRING, chunk, re.S):
            uris.append(pdf_literal(m.group(1)))
        for m in re.finditer(rb'/URI\s*<([0-9A-Fa-f\s]*)>', chunk):
            uris.append(bytes.fromhex(re.sub(rb'\s', b'', m.group(1)).decode()).decode('latin-1', 'replace'))
    text, engine = '\n'.join(t for t in texts if t), 'builtin'
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        text, engine = '\n'.join(page.extract_text() or '' for page in reader.pages), 'pypdf'
    except ImportError:
        pass
    except Exception:
        engine = 'builtin (pypdf failed)'
    uris = list(dict.fromkeys(u.strip() for u in uris if u.strip()))
    return text, uris, engine


# ------------------------------------------------------------ repository links


@dataclass
class Link:
    repository: str
    identifier: str
    url: str
    evidence: str
    params: dict = field(default_factory=dict)


OSF_RESERVED = {'about', 'login', 'legal', 'terms', 'press', 'files', 'guids', 'users', 'nodes', 'embed', 'store', 'share'}
GITHUB_RESERVED = {'orgs', 'sponsors', 'topics', 'features', 'about', 'settings', 'marketplace', 'apps',
                   'collections', 'login', 'pricing', 'site', 'enterprise', 'search', 'notifications', 'explore'}

PATTERNS = [
    ('osf', re.compile(r'(?:https?://)?(?:www\.)?osf\.io/([a-z0-9]{5})(?![a-z0-9])(/?\?view_only=[0-9a-f]+)?', re.I)),
    ('osf', re.compile(r'10\.17605/osf\.io/([a-z0-9]{5})(?![a-z0-9])', re.I)),
    ('zenodo', re.compile(r'zenodo\.org/(?:records?|deposit|uploads)/(\d{4,})', re.I)),
    ('zenodo', re.compile(r'10\.5281/zenodo\.(\d{4,})', re.I)),
    ('figshare', re.compile(r'figshare\.com/articles/(?:[^\s/]+/){0,3}(\d{5,})', re.I)),
    ('figshare', re.compile(r'10\.6084/m9\.figshare\.(\d{5,})', re.I)),
    ('dryad', re.compile(r'10\.5061/dryad\.([a-z0-9]+)', re.I)),
    ('dataverse', re.compile(r'(10\.7910/DVN/[A-Z0-9]{6})(?![A-Z0-9])', re.I)),
    ('dataverse', re.compile(r'https?://([\w.-]+)/dataset\.xhtml\?persistentId=((?:doi|hdl):[^\s&"<>]+)', re.I)),
    ('openneuro', re.compile(r'openneuro\.org/datasets/(ds\d{6})', re.I)),
    ('openneuro', re.compile(r'10\.18112/openneuro\.(ds\d{6})', re.I)),
    ('github', re.compile(r'github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]+)', re.I)),
    ('gin', re.compile(r'gin\.g-node\.org/([\w.-]+)/([\w.-]+)', re.I)),
    ('mendeley', re.compile(r'data\.mendeley\.com/datasets/([a-z0-9]{6,})', re.I)),
    ('gitlab', re.compile(r'gitlab\.com/([\w.-]+(?:/[\w.-]+)+)', re.I)),
]
SUPPORTED = ('osf', 'zenodo', 'figshare', 'dryad', 'dataverse', 'openneuro', 'github')


def joined(text):
    """Undo line breaks that PDF layout inserts inside URLs and DOIs."""
    return re.sub(r'(?<=[/._\-=?])[ \t]*\r?\n[ \t]*(?=\w)', '', text)


def find_repository_links(text):
    links, seen = [], set()
    for source in (text, joined(text)):
        for repo, pattern in PATTERNS:
            for m in pattern.finditer(source):
                evidence = source[max(0, m.start() - 200):m.end() + 200].replace('\n', ' ')
                link = make_link(repo, m)
                if not link:
                    continue
                link.evidence = evidence
                key = (link.repository, link.identifier.lower())
                if key not in seen:
                    seen.add(key)
                    links.append(link)
    # A hyphenated line break can yield both 'owner/eye' and 'owner/eye-tracking'; keep the longer one.
    return [l for l in links if not any(o is not l and o.repository == l.repository and o.identifier.lower().startswith(l.identifier.lower() + '-') for o in links)]


def make_link(repo, m):
    if repo == 'osf':
        guid = m.group(1).lower()
        if guid in OSF_RESERVED:
            return None
        params = {}
        if m.lastindex and m.lastindex >= 2 and m.group(2):
            params['view_only'] = parse_qs(m.group(2).lstrip('/?'))['view_only'][0]
        return Link('osf', guid, f'https://osf.io/{guid}/', '', params)
    if repo == 'zenodo':
        return Link('zenodo', m.group(1), f'https://zenodo.org/records/{m.group(1)}', '')
    if repo == 'figshare':
        return Link('figshare', m.group(1), f'https://figshare.com/articles/{m.group(1)}', '')
    if repo == 'dryad':
        doi = '10.5061/dryad.' + m.group(1).rstrip('.').lower()
        return Link('dryad', doi, 'https://doi.org/' + doi, '')
    if repo == 'dataverse':
        if m.lastindex == 1:
            doi = m.group(1).upper()
            return Link('dataverse', 'doi:' + doi, 'https://doi.org/' + doi, '', {'host': 'dataverse.harvard.edu'})
        pid = m.group(2).rstrip('.')
        return Link('dataverse', pid, m.group(0), '', {'host': m.group(1).lower()})
    if repo == 'openneuro':
        return Link('openneuro', m.group(1), f'https://openneuro.org/datasets/{m.group(1)}', '')
    if repo in ('github', 'gin'):
        owner, name = m.group(1), re.sub(r'\.git$', '', m.group(2).rstrip('.-'))
        if repo == 'github' and (owner.lower() in GITHUB_RESERVED or not name):
            return None
        host = 'github.com' if repo == 'github' else 'gin.g-node.org'
        return Link(repo, f'{owner}/{name}', f'https://{host}/{owner}/{name}', '')
    if repo == 'mendeley':
        return Link('mendeley', m.group(1), f'https://data.mendeley.com/datasets/{m.group(1)}', '')
    if repo == 'gitlab':
        path = m.group(1).rstrip('.')
        return Link('gitlab', path, 'https://gitlab.com/' + path, '')
    return None


# ------------------------------------------------------------ repository listings


@dataclass
class Listing:
    files: list = field(default_factory=list)   # dicts: path, size, url, probe_url
    complete: bool = True
    notes: list = field(default_factory=list)


def get_json(transport, budget, url, headers=None):
    budget.spend()
    data, _, _ = transport.get(url, limit=30_000_000, headers=headers)
    return json.loads(data)


def add_params(url, params):
    if not params:
        return url
    return url + ('&' if '?' in url else '?') + urlencode(params)


def list_osf(transport, budget, link, listing, token=None):
    api, view = 'https://api.osf.io/v2', link.params
    guid = get_json(transport, budget, add_params(f'{api}/guids/{link.identifier}/?resolve=false', view))
    kind = guid['data']['relationships']['referent']['data']['type']
    if kind == 'files':
        item = get_json(transport, budget, add_params(f'{api}/files/{link.identifier}/', view))['data']
        listing.files.append(osf_file(item, ''))
        return listing
    if kind == 'preprints':
        pre = get_json(transport, budget, add_params(f'{api}/preprints/{link.identifier}/', view))['data']
        node = (pre.get('relationships', {}).get('node', {}).get('data') or {}).get('id')
        listing.notes.append('OSF preprint; checked its linked project.' if node else 'OSF preprint without a linked project; no data files listed.')
        if not node:
            return listing
        kind, link_id = 'nodes', node
    elif kind in ('nodes', 'registrations'):
        link_id = link.identifier
    else:
        listing.notes.append(f'OSF GUID refers to {kind}, which has no file storage.')
        return listing
    nodes, addons = [(kind, link_id, '')], []
    while nodes:
        kind, node, prefix = nodes.pop(0)
        for provider in paged(transport, budget, add_params(f'{api}/{kind}/{node}/files/', view)):
            name = provider['attributes'].get('name') or provider['attributes'].get('provider')
            root = provider['relationships']['files']['links']['related']['href']
            if name == 'osfstorage':
                walk_osf(transport, budget, listing, root, prefix, view)
            else:
                addons.append((root, f'{prefix}{name}:'))
        for child in paged(transport, budget, add_params(f'{api}/{kind}/{node}/children/', view)):
            nodes.append((kind, child['id'], prefix + (child['attributes'].get('title') or child['id']) + '/'))
    # Add-on storage (GitHub, Dropbox, ...) is proxied and slow, so it is listed after OSF Storage.
    for root, prefix in addons:
        walk_osf(transport, budget, listing, root, prefix, view)
    return listing


def walk_osf(transport, budget, listing, root, prefix, view):
    folders = [root]
    while folders:
        for item in paged(transport, budget, add_params(folders.pop(0), view)):
            if item['attributes']['kind'] == 'folder':
                folders.append(item['relationships']['files']['links']['related']['href'])
            else:
                listing.files.append(osf_file(item, prefix))


def osf_file(item, prefix):
    a = item['attributes']
    path = (a.get('materialized_path') or '/' + a['name']).lstrip('/')
    return {'path': prefix + path, 'size': a.get('size'), 'url': item['links'].get('download'), 'probe_url': item['links'].get('download')}


def paged(transport, budget, url):
    while url:
        page = get_json(transport, budget, url)
        yield from page.get('data', [])
        url = (page.get('links') or {}).get('next')
        if isinstance(url, dict):
            url = url.get('href')


def list_zenodo(transport, budget, link, listing, token=None):
    record = get_json(transport, budget, f'https://zenodo.org/api/records/{link.identifier}')
    files = record.get('files')
    if isinstance(files, dict):
        files = list((files.get('entries') or {}).values())
    if not files:
        listing.notes.append('Zenodo lists no public files (restricted, embargoed, or metadata-only record).')
    for f in files or []:
        url = (f.get('links') or {}).get('self') or (f.get('links') or {}).get('content')
        listing.files.append({'path': f.get('key') or f.get('filename'), 'size': f.get('size') or f.get('filesize'), 'url': url, 'probe_url': url})
    return listing


def list_figshare(transport, budget, link, listing, token=None):
    for f in get_json(transport, budget, f'https://api.figshare.com/v2/articles/{link.identifier}/files?page_size=1000'):
        listing.files.append({'path': f['name'], 'size': f.get('size'), 'url': f.get('download_url'), 'probe_url': f.get('download_url')})
    return listing


def list_dryad(transport, budget, link, listing, token=None):
    base = 'https://datadryad.org'
    dataset = get_json(transport, budget, f'{base}/api/v2/datasets/' + quote('doi:' + link.identifier, safe=''))
    version = dataset['_links']['stash:version']['href']
    url = f'{base}{version}/files'
    while url:
        page = get_json(transport, budget, url)
        for f in page.get('_embedded', {}).get('stash:files', []):
            href = f.get('_links', {}).get('stash:download', {}).get('href')
            # Dryad refuses anonymous API downloads, so archives/headers cannot be probed remotely.
            listing.files.append({'path': f['path'], 'size': f.get('size'), 'url': base + href if href else None, 'probe_url': None})
        nxt = page.get('_links', {}).get('next', {}).get('href')
        url = base + nxt if nxt else None
    listing.notes.append('Dryad does not allow anonymous downloads; file names are listed but archives and headers are not inspected.')
    return listing


def list_dataverse(transport, budget, link, listing, token=None):
    host = link.params.get('host', 'dataverse.harvard.edu')
    data = get_json(transport, budget, f'https://{host}/api/datasets/:persistentId/?persistentId=' + quote(link.identifier, safe=':/'))['data']
    for f in data['latestVersion'].get('files', []):
        df = f['dataFile']
        name = df.get('originalFileName') or df['filename']
        path = (f.get('directoryLabel') + '/' if f.get('directoryLabel') else '') + name
        url = f'https://{host}/api/access/datafile/{df["id"]}'
        listing.files.append({'path': path, 'size': df.get('originalFileSize') or df.get('filesize'),
                              'url': url, 'probe_url': None if f.get('restricted') else url + '?format=original'})
    return listing


def github_headers(token):
    return {'Accept': 'application/vnd.github+json', **({'Authorization': 'Bearer ' + token} if token else {})}


def list_github(transport, budget, link, listing, token=None, owner_repo=None, probe_base=None):
    owner_repo = owner_repo or link.identifier
    headers = github_headers(token)
    repo = get_json(transport, budget, f'https://api.github.com/repos/{owner_repo}', headers)
    branch = repo['default_branch']
    tree = get_json(transport, budget, f'https://api.github.com/repos/{owner_repo}/git/trees/{quote(branch, safe="")}?recursive=1', headers)
    listing.complete = not tree.get('truncated')
    if tree.get('truncated'):
        listing.notes.append('GitHub truncated the file tree; very large repositories are only partly listed.')
    for item in tree.get('tree', []):
        if item.get('type') != 'blob':
            continue
        path = item['path']
        probe = (probe_base + path) if probe_base else f'https://raw.githubusercontent.com/{owner_repo}/{quote(branch)}/{quote(path)}'
        listing.files.append({'path': path, 'size': item.get('size'), 'url': f'https://github.com/{owner_repo}/blob/{quote(branch)}/{quote(path)}', 'probe_url': probe})
    return listing


def list_openneuro(transport, budget, link, listing, token=None):
    ds = link.identifier
    list_github(transport, budget, link, listing, token, 'OpenNeuroDatasets/' + ds, f'https://s3.amazonaws.com/openneuro.org/{ds}/')
    listing.notes.append('Listed through the OpenNeuroDatasets GitHub mirror; downloads use the OpenNeuro S3 bucket.')
    for f in listing.files:
        f['url'] = f['probe_url']
    return listing


# ------------------------------------------------------------ EyeLink detection


def file_kind(path):
    low = (path or '').lower()
    if low.endswith(('.edf', '.edf.gz')):
        return 'edf'
    if low.endswith(('.asc', '.asc.gz')):
        return 'asc'
    if low.endswith('.zip'):
        return 'zip'
    if low.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.7z', '.rar')):
        return 'archive'
    return None


def header_kind(head, kind):
    """Classify the first bytes of a .edf/.asc file. EyeLink EDFs begin with 'SR_RESEARCH'."""
    if head[:2] == b'\x1f\x8b':
        try:
            head = zlib.decompressobj(31).decompress(head, 8192)
        except zlib.error:
            return 'unrecognized'
    if head.startswith(b'SR_RESEARCH'):
        return 'eyelink'
    if kind == 'asc' and re.search(rb'SR_RESEARCH|CONVERTED FROM .{0,300}EDF|TYPE: EDF_FILE|VERSION: EYELINK|SOURCE: EYELINK', head[:8192], re.I | re.S):
        return 'eyelink'
    if kind == 'asc' and re.search(rb'^(?:MSG|START|SAMPLES|EVENTS|SFIX|EFIX|SSACC|ESACC)\s+\d', head[:8192], re.M):
        return 'eyelink'
    if kind == 'edf' and head[:8] == b'0       ':
        return 'european_data_format'
    if head.startswith(b'version https://git-lfs'):
        return 'git_lfs_pointer'
    if b'.git/annex/objects' in head[:400]:
        return 'git_annex_pointer'
    return 'unrecognized'


def read_range(transport, budget, url, headers_range, limit):
    budget.spend()
    data, _, headers = transport.get(url, limit=limit, headers={'Range': headers_range}, truncate=True)
    return data, lower_headers(headers)


def probe_header(transport, budget, url, kind):
    head, _ = read_range(transport, budget, url, 'bytes=0-8191', 8192)
    return header_kind(head, kind)


def zip_members(transport, budget, url, size=None):
    """List a remote ZIP's members by reading only its central directory with HTTP Range requests."""
    want = 65_558
    tail, headers = read_range(transport, budget, url, f'bytes=-{want}', want)
    crange = headers.get('content-range', '')
    match = re.match(r'bytes (\d+)-(\d+)/(\d+)', crange)
    if match:
        start, total = int(match.group(1)), int(match.group(3))
    elif size is not None and len(tail) >= size:
        start, total = 0, size
    else:
        raise ValueError('Server ignored the byte-range request; archive contents not inspected.')
    idx = tail.rfind(b'PK\x05\x06')
    if idx < 0:
        raise ValueError('ZIP end-of-directory record not found.')
    entries, cd_size, cd_off = struct.unpack('<HII', tail[idx + 10:idx + 20])
    if 0xFFFFFFFF in (cd_size, cd_off) or entries == 0xFFFF:
        loc = idx - 20
        if loc < 0 or tail[loc:loc + 4] != b'PK\x06\x07':
            raise ValueError('ZIP64 locator not found.')
        rec = struct.unpack('<Q', tail[loc + 8:loc + 16])[0] - start
        if rec < 0:
            raise ValueError('ZIP64 directory record outside the fetched range.')
        cd_size, cd_off = struct.unpack('<QQ', tail[rec + 40:rec + 56])
    if cd_size > 16_000_000:
        raise ValueError('ZIP directory too large to inspect.')
    if cd_off >= start:
        cd = tail[cd_off - start:cd_off - start + cd_size]
    else:
        cd, _ = read_range(transport, budget, url, f'bytes={cd_off}-{cd_off + cd_size - 1}', cd_size)
    members, pos = [], 0
    while pos + 46 <= len(cd) and cd[pos:pos + 4] == b'PK\x01\x02':
        flags, method = struct.unpack('<HH', cd[pos + 8:pos + 12])
        csize, usize, nlen, elen, clen = struct.unpack('<IIHHH', cd[pos + 20:pos + 34])
        local = struct.unpack('<I', cd[pos + 42:pos + 46])[0]
        raw_name = cd[pos + 46:pos + 46 + nlen]
        extra = cd[pos + 46 + nlen:pos + 46 + nlen + elen]
        name = raw_name.decode('utf-8' if flags & 0x800 else 'cp437', 'replace')
        if 0xFFFFFFFF in (usize, csize, local):
            usize, csize, local = zip64_extra(extra, usize, csize, local)
        if not name.endswith('/'):
            members.append({'name': name, 'size': usize, 'method': method, 'offset': local})
        pos += 46 + nlen + elen + clen
    return members, total


def zip64_extra(extra, usize, csize, local):
    pos = 0
    while pos + 4 <= len(extra):
        tag, length = struct.unpack('<HH', extra[pos:pos + 4])
        if tag == 1:
            values, p = [], pos + 4
            for current in (usize, csize, local):
                if current == 0xFFFFFFFF:
                    values.append(struct.unpack('<Q', extra[p:p + 8])[0]); p += 8
                else:
                    values.append(current)
            return tuple(values)
        pos += 4 + length
    return usize, csize, local


def probe_zip_member(transport, budget, url, member, kind):
    off = member['offset']
    chunk, _ = read_range(transport, budget, url, f'bytes={off}-{off + 30 + 1024 + 8192 - 1}', 30 + 1024 + 8192)
    if chunk[:4] != b'PK\x03\x04':
        return 'probe_failed'
    nlen, elen = struct.unpack('<HH', chunk[26:30])
    body = chunk[30 + nlen + elen:]
    if member['method'] == 8:
        try:
            body = zlib.decompressobj(-15).decompress(body, 8192)
        except zlib.error:
            return 'probe_failed'
    elif member['method'] != 0:
        return 'not_probed'
    return header_kind(body, kind)


LISTERS = {'osf': list_osf, 'zenodo': list_zenodo, 'figshare': list_figshare, 'dryad': list_dryad,
           'dataverse': list_dataverse, 'github': list_github, 'openneuro': list_openneuro}


def check_repository(transport, link, budget=None, github_token=None):
    """List a repository and return {status, files_listed, files: [...eyelink/archive rows], notes}."""
    budget = budget or Budget()
    budget.start_repository()
    result = {'status': 'listed', 'files_listed': 0, 'files': [], 'notes': []}
    if link.repository not in SUPPORTED:
        result.update(status='unsupported', notes=[f'{link.repository} listings are not automated yet; open the link and check manually.'])
        return result
    listing = Listing()
    try:
        LISTERS[link.repository](transport, budget, link, listing, github_token)
    except BudgetExceeded as exc:
        # Keep whatever was listed before the limit; it is still evidence.
        listing.complete = False
        listing.notes.append(str(exc))
    except Exception as exc:
        if not listing.files:
            result.update(status='failed', notes=listing.notes + [f'{type(exc).__name__}: {exc}'])
            return result
        listing.complete = False
        listing.notes.append(f'Listing stopped early: {type(exc).__name__}: {exc}')
    result['files_listed'] = len(listing.files)
    result['notes'] += listing.notes
    if not listing.complete:
        result['status'] = 'partial'
    # Archive and header reads get their own small allowance, even after a long listing.
    budget.start_repository(budget.archives * 2 + budget.probes + 5, 120)
    for f in listing.files:
        kind = file_kind(f['path'])
        if kind in EYELINK_KINDS:
            result['files'].append(classify_file(transport, budget, f, kind))
        elif kind == 'zip':
            result['files'] += inspect_zip(transport, budget, f, result)
        elif kind == 'archive':
            result['files'].append({**row(f, kind), 'header': 'not_inspected', 'note': 'Only ZIP directories can be read remotely; download to inspect.'})
    return result


def row(f, kind, path=None, size=None):
    return {'path': path or f['path'], 'size': f.get('size') if size is None else size, 'url': f.get('url'), 'kind': kind}


def classify_file(transport, budget, f, kind):
    entry = {**row(f, kind), 'header': 'not_probed', 'note': None}
    if f.get('probe_url') and budget.probes > 0:
        budget.probes -= 1
        try:
            entry['header'] = probe_header(transport, budget, f['probe_url'], kind)
        except Exception as exc:
            entry.update(header='probe_failed', note=f'{type(exc).__name__}: {exc}')
    return entry


def inspect_zip(transport, budget, f, result):
    archive = {**row(f, 'zip'), 'header': 'not_inspected', 'note': None}
    if not f.get('probe_url') or budget.archives <= 0:
        archive['note'] = 'Archive directory not read (scan limit reached or no direct download URL).'
        return [archive]
    budget.archives -= 1
    try:
        members, _ = zip_members(transport, budget, f['probe_url'], f.get('size'))
    except Exception as exc:
        archive.update(header='inspect_failed', note=f'{type(exc).__name__}: {exc}')
        return [archive]
    found, probes = [], 2  # a couple of headers per archive is enough to characterise it
    for member in members:
        kind = file_kind(member['name'])
        if kind not in EYELINK_KINDS:
            continue
        entry = {**row(f, kind, f['path'] + '!/' + member['name'], member['size']), 'header': 'not_probed', 'note': 'Inside ZIP archive'}
        if budget.probes > 0 and probes > 0:
            budget.probes -= 1; probes -= 1
            try:
                entry['header'] = probe_zip_member(transport, budget, f['probe_url'], member, kind)
            except Exception as exc:
                entry.update(header='probe_failed', note=f'Inside ZIP archive; {type(exc).__name__}: {exc}')
        found.append(entry)
    archive.update(header='listed', note=f'{len(members)} archive members; {len(found)} .edf/.asc')
    return [archive] + found


def unpaywall_pdfs(transport, doi, email):
    data, _, _ = transport.get(f'https://api.unpaywall.org/v2/{quote(doi, safe="/")}?' + urlencode({'email': email}))
    record = json.loads(data)
    locations = [record.get('best_oa_location')] + (record.get('oa_locations') or [])
    return [loc['url_for_pdf'] for loc in locations if isinstance(loc, dict) and loc.get('url_for_pdf')]


def is_eyelink(entry):
    return entry['kind'] in EYELINK_KINDS and entry['header'] not in ('european_data_format', 'unrecognized')


def summarize(checks):
    """One verdict per scan, from the strongest evidence across repository checks."""
    files = [f for c in checks for f in c['files']]
    if any(f['header'] == 'eyelink' for f in files):
        return 'eyelink_confirmed'
    if any(is_eyelink(f) for f in files):
        return 'eyelink_candidates'
    if any(f['kind'] in ('zip', 'archive') and f['header'] != 'listed' for f in files):
        return 'archives_unchecked'
    if not checks:
        return 'no_repository_links'
    if any(c['status'] in ('failed', 'partial', 'unsupported') for c in checks):
        return 'incomplete'
    return 'none_found'


def main(argv=None):
    """Command-line check of a PDF (path or URL) or a repository URL, without the app database."""
    import argparse
    from .providers import Transport
    parser = argparse.ArgumentParser(description='Find EyeLink .edf/.asc files linked from a paper PDF or repository URL.')
    parser.add_argument('source', help='PDF file path, PDF URL, repository URL, or text containing repository links')
    args = parser.parse_args(argv)
    transport, text = Transport(), args.source
    import os
    if os.path.isfile(args.source):
        text, uris, engine = pdf_text(open(args.source, 'rb').read())
        text += '\n' + '\n'.join(uris)
    elif args.source.lower().split('?')[0].endswith('.pdf'):
        data, _, _ = transport.get(args.source, limit=PDF_LIMIT)
        text, uris, engine = pdf_text(data)
        text += '\n' + '\n'.join(uris)
    links = find_repository_links(text)
    if not links:
        print('No supported data-repository links found.')
    budget = Budget()
    checks = []
    for link in links:
        print(f'\n{link.repository}: {link.url}')
        check = check_repository(transport, link, budget, os.environ.get('ORCHARD_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN'))
        checks.append(check)
        print(f'  status={check["status"]} files_listed={check["files_listed"]}')
        for note in check['notes']:
            print('  note:', note)
        for f in check['files']:
            print(f'  [{f["kind"]}] {f["path"]} size={f["size"]} header={f["header"]}' + (f' ({f["note"]})' if f.get('note') else ''))
    print('\nVerdict:', summarize(checks))


if __name__ == '__main__':
    main()
