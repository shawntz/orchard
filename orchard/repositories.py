"""Repository discovery adapters for OSF, Zenodo, Dryad and GitHub.

Every adapter uses a documented public API and declares, per search strategy, what it can
and cannot see. A *file* strategy queries an index of file names or file contents; a
*metadata* strategy only matches titles, descriptions and keywords, and projects found that
way are counted as examined records until their full file listing has been read.

API references:
- OSF: https://developer.osf.io (v2 nodes, files, wikis) and the SHARE/trove index-card search
  used by https://osf.io/search (https://share.osf.io/trove/docs).
- Zenodo: https://developers.zenodo.org (records search; ``filetype:`` is the indexed extension
  of each top-level file).
- Dryad: https://datadryad.org/api/v2/docs (search, datasets, versions, files).
- GitHub: https://docs.github.com/rest/search (code and repository search; git trees).
"""
import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from html import unescape
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit

from . import eyelink

EYELINK_NAME = re.compile(r'\.(edf|asc)(\.gz)?$', re.I)
ARCHIVE_NAME = re.compile(r'\.(zip|tar|tgz|tar\.gz|tar\.bz2|tar\.xz|7z|rar)$', re.I)
DOC_NAME = re.compile(r'(?i)(^|/)(readme[^/]*|codebook[^/]*|data[ _-]?dictionary[^/]*|citation\.cff|\.zenodo\.json|codemeta\.json|methods?[^/]*\.(md|txt))$')
TEXT_DOC = re.compile(r'(?i)\.(md|markdown|txt|rst|cff|json|org|html?)$|(^|/)readme$')
SOURCES = ('osf', 'zenodo', 'dryad', 'github')
DEFAULT_PHRASES = ('pupillometry', 'pupil size', 'pupil diameter', 'pupil dilation', 'eyelink pupil')
# Minimum seconds between requests to one host, below each service's published anonymous limits.
INTERVALS = {'zenodo.org': 2.1, 'api.github.com': 1.0, 'api.osf.io': 0.4, 'share.osf.io': 0.6,
             'datadryad.org': 1.1, 'api.crossref.org': 0.25, 'api.openalex.org': 0.15, 'api.datacite.org': 0.25}


class RateLimited(RuntimeError):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


def env(*names):
    for name in names:
        value = os.environ.get(name, '').strip()
        if value:
            return value
    return None


def html_text(value):
    text = re.sub(r'(?i)<\s*(br|/p|/div|/li|/h\d)\s*/?>', '\n', str(value or ''))
    return unescape(re.sub(r'<[^>]+>', ' ', text)).replace('\xa0', ' ').strip()


class Client:
    """Per-host pacing and rate-limit handling around a ``Transport``-like ``get``."""

    def __init__(self, transport, intervals=None, sleeper=time.sleep, clock=time.monotonic, max_wait=90):
        self.transport, self.sleeper, self.clock, self.max_wait = transport, sleeper, clock, max_wait
        self.intervals = {**INTERVALS, **(intervals or {})}
        self.last = {}
        self.requests = 0
        self.lock = threading.Lock()

    def get(self, url, headers=None, limit=8_000_000, truncate=False):
        host = urlsplit(url).hostname or ''
        with self.lock:
            # Reserve the next slot for this host so concurrent jobs keep the spacing.
            now = self.clock()
            slot = max(now, self.last.get(host, -1e9) + self.intervals.get(host, 0))
            self.last[host] = slot
            self.requests += 1
        if slot > now:
            self.sleeper(slot - now)
        try:
            data, final, response_headers = self.transport.get(url, limit=limit, headers=headers, truncate=truncate)
        except HTTPError as exc:
            retry = self.retry_after(exc.code, exc.headers)
            if retry is not None:
                raise RateLimited(f'{host} rate limit reached (HTTP {exc.code}); retry after {int(retry)} s.', retry) from exc
            raise
        lowered = {str(k).lower(): v for k, v in (response_headers or {}).items()}
        if lowered.get('x-ratelimit-remaining') == '0':
            # Spend the reset wait now when it is short, so the next call does not fail.
            reset = self.retry_after(429, lowered) or 0
            if 0 < reset <= self.max_wait:
                self.sleeper(reset)
        return data, final, response_headers

    def retry_after(self, code, headers):
        headers = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        if code not in (429, 403):
            return None
        if code == 403 and headers.get('x-ratelimit-remaining') != '0' and 'retry-after' not in headers:
            return None
        value = str(headers.get('retry-after', ''))
        if value.isdigit():
            return float(value)
        reset = str(headers.get('x-ratelimit-reset', ''))
        if reset.isdigit():
            return max(1.0, float(reset) - time.time()) if int(reset) > 1e9 else float(reset)
        return 60.0

    def json(self, url, headers=None, limit=20_000_000):
        data, _, _ = self.get(url, headers=headers, limit=limit)
        return json.loads(data)


@dataclass
class Strategy:
    source: str
    kind: str            # 'file_index' | 'code_index' | 'metadata'
    query: str
    label: str
    coverage: str
    parameters: dict = field(default_factory=dict)
    skipped: str = None  # reason this strategy cannot run in the current configuration


@dataclass
class SearchPage:
    hits: list
    cursor: object
    total: int
    request: str


@dataclass
class Inventory:
    files: list = field(default_factory=list)
    complete: bool = True
    notes: list = field(default_factory=list)


def file_row(path, size=None, url=None, probe_url=None, checksum=None, download='public', note=None):
    return {'path': path, 'size': size, 'url': url, 'probe_url': probe_url, 'checksum': checksum,
            'download': download, 'note': note}


def eyelink_names(names):
    return [n for n in names if n and EYELINK_NAME.search(n)]


# --------------------------------------------------------------------------- OSF

OSF_API = 'https://api.osf.io/v2'
TROVE = 'https://share.osf.io/trove/index-card-search'


def trove_value(md, key):
    values = md.get(key) or []
    return values[0].get('@value') if values and isinstance(values[0], dict) else None


def osf_guid(url):
    match = re.search(r'osf\.io/([a-z0-9]{5,})', str(url or ''), re.I)
    return match.group(1).lower() if match else None


class OSF:
    name = 'osf'
    label = 'OSF'

    def __init__(self, client):
        self.client = client

    def plan(self, phrases, extensions=('asc', 'edf')):
        out = [Strategy('osf', 'file_index', ext, f'OSF file index: names ending .{ext}',
                        'SHARE/trove index of public OSF files. Only files that OSF has indexed are visible '
                        '(files never opened on osf.io often have no index card), and file contents and '
                        'archive members are not indexed. Projects found here are then listed in full.',
                        {'extension': ext}) for ext in extensions]
        out += [Strategy('osf', 'metadata', phrase, f'OSF projects and registrations: "{phrase}"',
                         'SHARE/trove full-text search of public project, component and registration '
                         'metadata (title, description, tags). Does not see file names; every project '
                         'found is listed in full before it counts as having EyeLink files.')
                for phrase in phrases]
        return out

    def search(self, strategy, cursor=None):
        if cursor:
            url = cursor
        elif strategy.kind == 'file_index':
            url = TROVE + '?' + urlencode({'cardSearchFilter[resourceType]': 'File', 'cardSearchText': strategy.query,
                                           'page[size]': 100})
        else:
            url = TROVE + '?' + urlencode({'cardSearchFilter[resourceType]': 'Project,ProjectComponent,Registration,RegistrationComponent',
                                           'cardSearchText': strategy.query, 'page[size]': 100})
        result = self.client.json(url, headers={'Accept': 'application/vnd.api+json'})
        data = result.get('data') if isinstance(result, dict) else None
        if not isinstance(data, dict) or 'totalResultCount' not in (data.get('attributes') or {}):
            raise ValueError('Unexpected OSF search response; this page was not counted.')
        cards = [i for i in result.get('included', []) if i.get('type') == 'index-card']
        hits = []
        for card in cards:
            md = card.get('attributes', {}).get('resourceMetadata', {})
            if strategy.kind == 'file_index':
                name = trove_value(md, 'fileName') or ''
                path = trove_value(md, 'filePath') or name
                for parent in md.get('isContainedBy') or []:
                    guid = osf_guid(parent.get('@id'))
                    if guid:
                        hits.append({'external_id': guid, 'url': f'https://osf.io/{guid}/',
                                     'title': trove_value(parent, 'title'), 'matched_files': eyelink_names([path]),
                                     'evidence': {'indexed_file': path, 'file_card': md.get('@id')}})
                        break
            else:
                guid = osf_guid(md.get('@id'))
                if guid:
                    hits.append({'external_id': guid, 'url': f'https://osf.io/{guid}/', 'title': unescape(trove_value(md, 'title') or ''),
                                 'matched_files': [], 'evidence': {'resource_type': [t.get('@id') for t in md.get('resourceType', [])]}})
        page = data.get('relationships', {}).get('searchResultPage', {})
        following = ((page.get('links') or {}).get('next') or {}).get('href')
        return SearchPage(hits, following if cards and following else None, int(data['attributes']['totalResultCount']), url)

    def metadata(self, guid):
        ref = self.client.json(f'{OSF_API}/guids/{guid}/?resolve=false')
        kind = ref['data']['relationships']['referent']['data']['type']
        if kind not in ('nodes', 'registrations'):
            raise ValueError(f'OSF identifier {guid} refers to {kind}, not a project.')
        node = self.client.json(f'{OSF_API}/{kind}/{guid}/?embed=license')['data']
        a = node.get('attributes', {})
        out = {'title': a.get('title'), 'description': a.get('description') or '', 'url': f'https://osf.io/{guid}/',
               'kind': ('registration' if kind == 'registrations' else 'project') + (' component' if (node.get('relationships', {}).get('parent') or {}).get('data') else ''),
               'created': a.get('date_created'), 'keywords': a.get('tags') or [], 'license': None, 'doi': None,
               'creators': [], 'related': [], 'api_kind': kind}
        lic = (node.get('embeds') or {}).get('license', {}).get('data')
        if lic:
            out['license'] = lic.get('attributes', {}).get('name')
        if a.get('article_doi'):
            out['related'].append({'relation': 'article_doi', 'identifier': a['article_doi'], 'scheme': 'doi', 'resource_type': 'publication', 'origin': 'OSF registration article DOI'})
        try:
            for ident in self.client.json(f'{OSF_API}/{kind}/{guid}/identifiers/').get('data', []):
                if ident['attributes'].get('category') == 'doi':
                    out['doi'] = ident['attributes']['value']
        except Exception:
            pass
        try:
            contributors = self.client.json(f'{OSF_API}/{kind}/{guid}/contributors/?embed=users&page[size]=50').get('data', [])
            for c in contributors:
                if c.get('attributes', {}).get('bibliographic') is False:
                    continue
                user = ((c.get('embeds') or {}).get('users') or {}).get('data') or {}
                name = (user.get('attributes') or {}).get('full_name')
                if name:
                    out['creators'].append(name)
        except Exception:
            pass
        if kind == 'nodes':
            try:
                for pre in self.client.json(f'{OSF_API}/nodes/{guid}/preprints/').get('data', []):
                    pa = pre.get('attributes', {})
                    out['related'].append({'relation': 'osf_preprint', 'identifier': pa.get('doi') or (pre.get('links') or {}).get('html'),
                                           'scheme': 'doi' if pa.get('doi') else 'url', 'resource_type': 'preprint', 'title': pa.get('title'),
                                           'origin': 'Preprint linked to this OSF project'})
            except Exception:
                pass
        return out

    def inventory(self, guid, budget, meta=None):
        listing = eyelink.Listing()
        link = eyelink.Link('osf', guid, f'https://osf.io/{guid}/', '')
        try:
            eyelink.list_osf(self.client, budget, link, listing)
        except eyelink.BudgetExceeded as exc:
            listing.complete = False
            listing.notes.append(str(exc))
        files = [file_row(f['path'], f.get('size'), f.get('url'), f.get('probe_url'), f.get('checksum'),
                          'public' if f.get('url') else 'unavailable') for f in listing.files]
        return Inventory(files, listing.complete, listing.notes)

    def documents(self, guid, meta, inventory):
        docs = []
        kind = meta.get('api_kind', 'nodes')
        try:
            for wiki in self.client.json(f'{OSF_API}/{kind}/{guid}/wikis/').get('data', [])[:10]:
                url = (wiki.get('links') or {}).get('download')
                if url:
                    text = self.client.get(url, limit=2_000_000)[0].decode('utf-8', 'replace')
                    docs.append({'kind': 'wiki', 'title': 'OSF wiki: ' + wiki.get('attributes', {}).get('name', ''), 'url': (wiki.get('links') or {}).get('iri') or url, 'text': text, 'method': 'OSF wiki API (markdown)'})
        except RateLimited:
            raise
        except Exception as exc:
            docs.append({'kind': 'error', 'title': 'OSF wiki', 'url': f'{OSF_API}/{kind}/{guid}/wikis/', 'text': '', 'method': f'failed: {exc}'})
        return docs + fetch_doc_files(self.client, inventory)


def fetch_doc_files(client, inventory, limit=8):
    """Download small README/codebook/citation files listed in the inventory."""
    out = []
    picks = [f for f in inventory.files if DOC_NAME.search(f['path']) and '!/' not in f['path'] and f.get('probe_url')
             and (f.get('size') or 0) <= 2_000_000]
    picks.sort(key=lambda f: (f['path'].count('/'), 'readme' not in f['path'].lower()))
    for f in picks[:limit]:
        try:
            data, _, _ = client.get(f['probe_url'], limit=2_000_000)
        except RateLimited:
            raise
        except Exception as exc:
            out.append({'kind': 'error', 'title': f['path'], 'url': f.get('url'), 'text': '', 'method': f'failed: {exc}'})
            continue
        if data[:5] == b'%PDF-':
            from . import pdftext
            text, method = pdftext.extract(data)
            out.append({'kind': 'readme', 'title': f['path'], 'url': f.get('url'), 'text': text or '', 'method': 'PDF text (' + method + ')' if text else 'PDF without extractable text'})
        elif TEXT_DOC.search(f['path']) or b'\x00' not in data[:2000]:
            out.append({'kind': 'citation' if re.search(r'(?i)citation|zenodo\.json|codemeta', f['path']) else 'readme',
                        'title': f['path'], 'url': f.get('url'), 'text': data.decode('utf-8', 'replace'), 'method': 'repository file download'})
    return out


# --------------------------------------------------------------------------- Zenodo

ZENODO = 'https://zenodo.org/api/records'


class Zenodo:
    name = 'zenodo'
    label = 'Zenodo'

    def __init__(self, client):
        self.client = client
        self.token = env('ORCHARD_ZENODO_TOKEN')

    def headers(self):
        return {'Authorization': 'Bearer ' + self.token} if self.token else None

    def plan(self, phrases, extensions=('asc', 'edf')):
        ext_query = ' OR '.join('filetype:' + e for e in extensions)
        out = [Strategy('zenodo', 'file_index', ext_query, 'Zenodo records with ' + ' / '.join('.' + e for e in extensions) + ' files',
                        'Zenodo indexes the extension of every top-level file, so this lists all public records '
                        'with such files. Files inside ZIP/TAR archives and compressed .asc.gz/.edf.gz files are '
                        'not indexed by extension; archives are opened during project inspection.')]
        out += [Strategy('zenodo', 'metadata', phrase, f'Zenodo metadata: "{phrase}"',
                         'Zenodo full-text search over record metadata. Finds records whose EyeLink files are inside '
                         'archives, which are then listed and opened where byte-range reads are allowed.')
                for phrase in phrases]
        return out

    def search(self, strategy, cursor=None):
        page = int(cursor or 1)
        size = 100 if self.token else 25
        url = ZENODO + '?' + urlencode({'q': strategy.query, 'page': page, 'size': size, 'sort': 'mostrecent'})
        result = self.client.json(url, headers=self.headers())
        hits_block = result.get('hits') if isinstance(result, dict) else None
        if not isinstance(hits_block, dict) or not isinstance(hits_block.get('hits'), list):
            raise ValueError('Unexpected Zenodo search response; this page was not counted.')
        total = hits_block.get('total')
        total = total.get('value') if isinstance(total, dict) else total
        hits = []
        for item in hits_block['hits']:
            names = [f.get('key') for f in item.get('files') or []]
            hits.append({'external_id': str(item['id']), 'url': (item.get('links') or {}).get('self_html') or f'https://zenodo.org/records/{item["id"]}',
                         'title': (item.get('metadata') or {}).get('title'), 'matched_files': eyelink_names(names),
                         'evidence': {'files_in_record': len(names)}, 'raw': item})
        links = result.get('links')
        if isinstance(links, dict):
            more = bool(links.get('next')) and bool(hits_block['hits'])
        else:
            more = bool(hits_block['hits']) and page * size < int(total or 0)
        # Zenodo serves at most 10,000 results per query; beyond that the query must be split.
        more = more and page * size < 10_000
        return SearchPage(hits, page + 1 if more else None, int(total or 0), url)

    def record(self, rid):
        return self.client.json(f'{ZENODO}/{quote(str(rid))}', headers=self.headers())

    def metadata(self, rid, record=None):
        record = record or self.record(rid)
        m = record.get('metadata', {})
        related = []
        for r in m.get('related_identifiers') or []:
            related.append({'relation': r.get('relation'), 'identifier': r.get('identifier'), 'scheme': r.get('scheme'),
                            'resource_type': r.get('resource_type'), 'origin': 'Zenodo related identifier'})
        lic = m.get('license')
        return {'title': m.get('title'), 'description': html_text(m.get('description')) + ('\n\n' + html_text(m.get('notes')) if m.get('notes') else ''),
                'url': (record.get('links') or {}).get('self_html') or f'https://zenodo.org/records/{rid}',
                'kind': 'record: ' + str((m.get('resource_type') or {}).get('type') or 'unknown'), 'created': record.get('created'),
                'keywords': m.get('keywords') or [], 'license': lic.get('id') if isinstance(lic, dict) else lic,
                'doi': record.get('doi') or m.get('doi'), 'creators': [c.get('name') for c in m.get('creators') or [] if c.get('name')],
                'related': related, 'access_right': m.get('access_right'), 'raw_files': record.get('files') or []}

    def inventory(self, rid, budget, meta=None):
        meta = meta or self.metadata(rid)
        restricted = meta.get('access_right') not in (None, 'open')
        files = []
        for f in meta.get('raw_files') or []:
            url = (f.get('links') or {}).get('self') or (f.get('links') or {}).get('content')
            files.append(file_row(f.get('key'), f.get('size'), url, None if restricted else url, f.get('checksum'),
                                  'restricted' if restricted else 'public'))
        notes = [f'Zenodo access right: {meta.get("access_right")}; files cannot be downloaded anonymously.'] if restricted else []
        return Inventory(files, True, notes)

    def documents(self, rid, meta, inventory):
        docs = []
        if meta.get('description'):
            docs.append({'kind': 'description', 'title': 'Zenodo record description', 'url': meta['url'], 'text': meta['description'], 'method': 'Zenodo records API metadata'})
        return docs + fetch_doc_files(self.client, inventory)


# --------------------------------------------------------------------------- Dryad

DRYAD = 'https://datadryad.org'


class Dryad:
    name = 'dryad'
    label = 'Dryad'

    def __init__(self, client):
        self.client = client
        self.token = env('ORCHARD_DRYAD_TOKEN')

    def headers(self):
        return {'Authorization': 'Bearer ' + self.token} if self.token else None

    def plan(self, phrases, extensions=('asc', 'edf')):
        return [Strategy('dryad', 'metadata', phrase, f'Dryad metadata: "{phrase}"',
                         'Dryad search covers dataset metadata (title, abstract, keywords, methods); it has no file-name '
                         'search, so .edf/.asc files are only seen when a dataset found here is listed. Dryad requires an '
                         'API token for file downloads, so READMEs, archives and headers are not read anonymously.')
                for phrase in phrases]

    def search(self, strategy, cursor=None):
        page = int(cursor or 1)
        url = f'{DRYAD}/api/v2/search?' + urlencode({'q': strategy.query, 'page': page, 'per_page': 100})
        result = self.client.json(url, headers=self.headers())
        datasets = (result.get('_embedded') or {}).get('stash:datasets') if isinstance(result, dict) else None
        if not isinstance(datasets, list):
            raise ValueError('Unexpected Dryad search response; this page was not counted.')
        hits = [{'external_id': str(ds.get('identifier') or ds.get('id')), 'url': f'{DRYAD}/dataset/{ds.get("identifier")}',
                 'title': ds.get('title'), 'matched_files': [], 'evidence': {}, 'raw': ds} for ds in datasets]
        total = int(result.get('total') or 0)
        more = datasets and page * 100 < total
        return SearchPage(hits, page + 1 if more else None, total, url)

    def metadata(self, ident, raw=None):
        ds = raw or self.client.json(f'{DRYAD}/api/v2/datasets/' + quote(ident, safe=''), headers=self.headers())
        related = [{'relation': w.get('relationship'), 'identifier': w.get('identifier'), 'scheme': (w.get('identifierType') or '').lower(),
                    'resource_type': 'publication' if 'article' in str(w.get('relationship')) else None, 'origin': 'Dryad related work'}
                   for w in ds.get('relatedWorks') or []]
        text = '\n\n'.join(filter(None, [html_text(ds.get('abstract')), 'Methods: ' + html_text(ds.get('methods')) if ds.get('methods') else '',
                                         'Usage notes: ' + html_text(ds.get('usageNotes')) if ds.get('usageNotes') else '']))
        return {'title': ds.get('title'), 'description': text, 'url': f'{DRYAD}/dataset/{ident}', 'kind': 'dataset',
                'created': ds.get('publicationDate'), 'keywords': ds.get('keywords') or [], 'license': ds.get('license'),
                'doi': str(ident).replace('doi:', ''), 'creators': [' '.join(filter(None, [a.get('firstName'), a.get('lastName')])) for a in ds.get('authors') or []],
                'related': related, 'version': (ds.get('_links') or {}).get('stash:version', {}).get('href')}

    def inventory(self, ident, budget, meta=None):
        meta = meta or self.metadata(ident)
        version = meta.get('version')
        if not version:
            return Inventory([], False, ['Dryad returned no published version; files could not be listed.'])
        files, url = [], DRYAD + version + '/files'
        while url:
            budget.spend()
            page = self.client.json(url, headers=self.headers())
            for f in (page.get('_embedded') or {}).get('stash:files', []):
                href = (f.get('_links') or {}).get('stash:download', {}).get('href')
                download = DRYAD + href if href else None
                files.append(file_row(f.get('path'), f.get('size'), download, download if self.token else None,
                                      f.get('digestType', 'md5') + ':' + f['digest'] if f.get('digest') else None,
                                      'public' if self.token else 'auth_required'))
            nxt = ((page.get('_links') or {}).get('next') or {}).get('href')
            url = DRYAD + nxt if nxt else None
        notes = [] if self.token else ['Dryad requires an API token for file downloads (set ORCHARD_DRYAD_TOKEN); file names and sizes are listed, but READMEs, archives and file headers were not read.']
        return Inventory(files, True, notes)

    def documents(self, ident, meta, inventory):
        docs = [{'kind': 'description', 'title': 'Dryad abstract, methods and usage notes', 'url': meta['url'], 'text': meta.get('description') or '', 'method': 'Dryad datasets API metadata'}]
        return docs + (fetch_doc_files(self.client, inventory) if self.token else [])


# --------------------------------------------------------------------------- GitHub

GITHUB = 'https://api.github.com'


class GitHub:
    name = 'github'
    label = 'GitHub'

    def __init__(self, client):
        self.client = client
        self.token = env('ORCHARD_GITHUB_TOKEN', 'GITHUB_TOKEN')

    def headers(self):
        return eyelink.github_headers(self.token)

    def plan(self, phrases, extensions=('asc', 'edf')):
        out = []
        if 'asc' in extensions:
            for marker in ('EYELINK', '"CONVERTED FROM"'):
                out.append(Strategy('github', 'code_index', f'{marker} extension:asc', f'GitHub code search: .asc files containing {marker}',
                                    'GitHub code search indexes text files under 384 KB on default branches and returns at most '
                                    '1,000 results per query. Full EyeLink .asc recordings are usually larger, so this mainly '
                                    'finds small recordings, excerpts, test files and Git LFS pointers. Binary .edf files are never indexed.',
                                    skipped=None if self.token else 'GitHub code search requires a token (set ORCHARD_GITHUB_TOKEN). Not searched.'))
        out += [Strategy('github', 'metadata', phrase, f'GitHub repositories: "{phrase}"',
                         'Repository search over names, descriptions, topics and READMEs (at most 1,000 results per query). '
                         'Each repository found is listed through the git tree API.') for phrase in phrases]
        return out

    def search(self, strategy, cursor=None):
        page = int(cursor or 1)
        if strategy.kind == 'code_index':
            url = f'{GITHUB}/search/code?' + urlencode({'q': strategy.query, 'per_page': 100, 'page': page})
        else:
            phrase = f'"{strategy.query}"' if ' ' in strategy.query and '"' not in strategy.query else strategy.query
            url = f'{GITHUB}/search/repositories?' + urlencode({'q': phrase + ' in:name,description,topics,readme', 'per_page': 100, 'page': page})
        result = self.client.json(url, headers=self.headers())
        if not isinstance(result, dict) or not isinstance(result.get('items'), list):
            raise ValueError('Unexpected GitHub search response; this page was not counted.')
        hits = []
        for item in result['items']:
            repo = item.get('repository') or item
            full = repo.get('full_name')
            if not full:
                continue
            hits.append({'external_id': full, 'url': repo.get('html_url') or f'https://github.com/{full}', 'title': full,
                         'matched_files': eyelink_names([item.get('path')]) if strategy.kind == 'code_index' else [],
                         'evidence': {'path': item.get('path')} if strategy.kind == 'code_index' else {'description': repo.get('description')}})
        total = int(result.get('total_count') or 0)
        more = result['items'] and page * 100 < min(total, 1000)
        return SearchPage(hits, page + 1 if more else None, total, url)

    def metadata(self, full):
        repo = self.client.json(f'{GITHUB}/repos/{full}', headers=self.headers())
        return {'title': repo.get('full_name'), 'description': repo.get('description') or '', 'url': repo.get('html_url'),
                'kind': 'repository' + (' (fork)' if repo.get('fork') else ''), 'created': repo.get('created_at'),
                'keywords': repo.get('topics') or [], 'license': (repo.get('license') or {}).get('spdx_id'), 'doi': None,
                'creators': [(repo.get('owner') or {}).get('login')], 'related': [], 'homepage': repo.get('homepage')}

    def inventory(self, full, budget, meta=None):
        listing = eyelink.Listing()
        link = eyelink.Link('github', full, f'https://github.com/{full}', '')
        try:
            eyelink.list_github(self.client, budget, link, listing, self.token)
        except eyelink.BudgetExceeded as exc:
            listing.complete = False
            listing.notes.append(str(exc))
        files = [file_row(f['path'], f.get('size'), f.get('url'), f.get('probe_url'), f.get('checksum'), 'public') for f in listing.files]
        return Inventory(files, listing.complete, listing.notes)

    def documents(self, full, meta, inventory):
        docs = []
        if meta.get('description'):
            docs.append({'kind': 'description', 'title': 'GitHub repository description', 'url': meta['url'], 'text': meta['description'], 'method': 'GitHub repository API'})
        try:
            readme = self.client.json(f'{GITHUB}/repos/{full}/readme', headers=self.headers())
            text = base64.b64decode(readme.get('content') or '').decode('utf-8', 'replace')
            docs.append({'kind': 'readme', 'title': readme.get('path', 'README'), 'url': readme.get('html_url'), 'text': text, 'method': 'GitHub readme API'})
        except RateLimited:
            raise
        except Exception as exc:
            docs.append({'kind': 'error', 'title': 'README', 'url': meta.get('url'), 'text': '', 'method': f'failed: {exc}'})
        others = Inventory([f for f in inventory.files if not re.search(r'(?i)(^|/)readme(\.\w+)?$', f['path']) or '/' in f['path']])
        return docs + fetch_doc_files(self.client, others, limit=5)


ADAPTERS = {'osf': OSF, 'zenodo': Zenodo, 'dryad': Dryad, 'github': GitHub}


def adapters(client):
    return {name: cls(client) for name, cls in ADAPTERS.items()}
