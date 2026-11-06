"""Bounded, metadata-first discovery of public EyeLink datasets.

This is a candidate search, never a census of repository holdings. No data files
are downloaded; filenames are confirmed against each project's file listing.
"""
import json
import re
from html import unescape
from urllib.parse import urlencode, quote, urlsplit

from .providers import Transport

EXT = re.compile(r'\.(edf|asc)$', re.I)
DEFAULT_TERMS = ('pupil', 'pupillometry', 'memory', 'attention', 'emotion',
                 'cognitive', 'decision', 'eyelink', 'eye tracking')


def _text(value):
    return unescape(re.sub('<[^>]*>', ' ', str(value or '')))


def classify(text, terms=DEFAULT_TERMS, source='metadata'):
    """Conservative candidate flag, never an automatic exclusion decision."""
    text = re.sub(r'(?i)\bpupilsize\b','pupil size',_text(text))
    terms=list(dict.fromkeys([t.strip().lower() for t in terms]+['pupil','pupillometry','pupil size','pupil diameter']))
    hits = [term for term in terms if re.search(r'(?<!\w)' + re.escape(term) + r'(?:s|ometry|lary)?(?!\w)', text, re.I)]
    evidence = []
    for hit in hits[:8]:
        match = re.search(r'(?<!\w)' + re.escape(hit) + r'(?:s|ometry|lary)?(?!\w)', text, re.I)
        evidence.append(text[max(0, match.start()-100):match.end()+100].strip())
    # EyeLink alone establishes an acquisition system, not a cognitive task.
    domain = {'memory', 'attention', 'emotion', 'cognitive', 'decision','learning','arousal','perception','language','mental effort','working memory'}
    measure = {'pupil', 'pupillometry','pupil size','pupil diameter'}
    if any(h.lower() in domain for h in hits) and any(h.lower() in measure for h in hits):
        status, reason = 'candidate', 'Cognitive and pupil terms occur in available text; manual review required.'
    elif hits:
        status, reason = 'review', 'Partial term match; do not exclude automatically.'
    else:
        status, reason = 'review', 'No configured terms found in available text; absence is not evidence of irrelevance.'
    return {'status': status, 'reason': reason, 'hits': hits, 'evidence': evidence, 'text_source': source}


def _related(obj, name):
    link = obj.get('relationships', {}).get(name, {}).get('links', {}).get('related')
    return link.get('href') if isinstance(link, dict) else link


def _edfs(items, path=''):
    found = []
    for item in items:
        name = str(item.get('name') or item.get('filename') or item.get('key') or item.get('path') or '')
        full = path + name
        match = EXT.search(full)
        if match:
            found.append({'path': full, 'url': item.get('url') or item.get('download'),
                          'size': item.get('size'), 'extension': '.' + match.group(1).lower()})
    return found


class RepositorySweep:
    def __init__(self, transport=None):
        self.transport = transport or Transport()

    def get(self, url):
        data, _, _ = self.transport.get(url, limit=4_000_000)
        return json.loads(data)

    def pages(self, url, maximum):
        seen = set()
        for _ in range(maximum):
            if not url or url in seen:
                return
            seen.add(url)
            result = self.get(url)
            yield result
            url = result.get('links', {}).get('next')

    def osf_files(self, node, max_pages=4):
        out = []
        # OSF exposes multiple storage providers; inspect each public provider.
        providers = _related(node, 'files') or 'https://api.osf.io/v2/nodes/' + quote(node['id']) + '/files/'
        for page in self.pages(providers, max_pages):
            for store in page.get('data', []):
                endpoint = _related(store, 'files')
                if endpoint:
                    out.extend(self.osf_folder(endpoint, max_pages))
        return out

    def osf_folder(self, endpoint, max_pages, depth=0):
        if depth > 4:
            return []
        out = []
        for page in self.pages(endpoint, max_pages):
            for item in page.get('data', []):
                a = item.get('attributes', {})
                name = str(a.get('materialized_path') or a.get('name') or '')
                if a.get('kind') == 'folder':
                    sub = _related(item, 'files')
                    if sub:
                        out.extend(self.osf_folder(sub, max_pages, depth+1))
                elif EXT.search(name):
                    out.extend(_edfs([{'name': name, 'url': item.get('links', {}).get('download'), 'size': a.get('size')}]))
        return out

    def search(self, repository, query, max_pages=2):
        """Yield normalized projects; caller records individual provider failures."""
        q = quote(query)
        if repository == 'osf':
            url = 'https://api.osf.io/v2/nodes/?' + urlencode({'filter[title]': query, 'filter[public]': 'true'})
            for page in self.pages(url, max_pages):
                if not isinstance(page.get('data'), list):
                    raise ValueError('Unexpected OSF search response; sweep is incomplete.')
                for node in page.get('data', []):
                    a = node.get('attributes', {})
                    yield {'repository': repository, 'id': node['id'], 'url': node.get('links', {}).get('html') or 'https://osf.io/'+node['id']+'/',
                           'title': a.get('title'), 'description': a.get('description'), 'doi': a.get('doi'), 'files': self.osf_files(node)}
        elif repository == 'zenodo':
            for page in range(1, max_pages+1):
                result = self.get('https://zenodo.org/api/records/?' + urlencode({'q': query, 'page': page, 'size': 25}))
                if not isinstance(result.get('hits', {}).get('hits'), list):
                    raise ValueError('Unexpected Zenodo search response; sweep is incomplete.')
                hits = result.get('hits', {}).get('hits', [])
                for item in hits:
                    meta = item.get('metadata', {})
                    files = item.get('files') or []
                    yield {'repository': repository, 'id': str(item['id']), 'url': item.get('links', {}).get('html') or 'https://zenodo.org/records/'+str(item['id']),
                           'title': meta.get('title'), 'description': meta.get('description'), 'doi': meta.get('related_identifiers', [{}])[0].get('identifier') if meta.get('related_identifiers') else None,
                           'files': _edfs([{'name': f.get('key'), 'url': f.get('links', {}).get('self'), 'size': f.get('size')} for f in files])}
                if not hits or len(hits) < 25:
                    break
        elif repository == 'github':
            for page in range(1, max_pages+1):
                result = self.get('https://api.github.com/search/repositories?' + urlencode({'q': query, 'per_page': 25, 'page': page}))
                if not isinstance(result.get('items'), list):
                    raise ValueError('Unexpected GitHub search response; sweep is incomplete.')
                items = result.get('items', [])
                for item in items:
                    full = item['full_name']
                    tree = self.get('https://api.github.com/repos/' + quote(full, safe='/') + '/git/trees/' + quote(item['default_branch'], safe='') + '?recursive=1')
                    if tree.get('truncated'):
                        raise ValueError('GitHub tree truncated for '+full+'; cannot certify file coverage.')
                    files = _edfs([{'name': t.get('path'), 'size': t.get('size'), 'url': 'https://github.com/'+full+'/blob/'+quote(item['default_branch'], safe='')+'/'+quote(t.get('path', ''), safe='/')} for t in tree.get('tree', []) if t.get('type') == 'blob'])
                    yield {'repository': repository, 'id': full, 'url': item['html_url'], 'title': item.get('name'), 'description': item.get('description'), 'doi': None, 'files': files}
                if len(items) < 25:
                    break
        elif repository == 'dryad':
            # Dryad search is metadata-based. Dataset file lists are paginated.
            for page in range(1, max_pages+1):
                result = self.get('https://datadryad.org/api/v2/search?' + urlencode({'q': query, 'page': page, 'per_page': 25}))
                if not isinstance(result.get('_embedded', {}).get('stash:datasets'), list):
                    raise ValueError('Unexpected Dryad search response; sweep is incomplete.')
                datasets = result.get('_embedded', {}).get('stash:datasets', [])
                for ds in datasets:
                    doi = ds.get('identifier') or ds.get('doi')
                    version = ds.get('_links', {}).get('stash:version', {}).get('href')
                    files = []
                    if version:
                        version_url = 'https://datadryad.org' + version if version.startswith('/') else version
                        ver = self.get(version_url)
                        file_link = ver.get('_links', {}).get('stash:files', {}).get('href')
                        if file_link:
                            file_url = 'https://datadryad.org' + file_link if file_link.startswith('/') else file_link
                            for fp in self.pages(file_url, max_pages):
                                files.extend(_edfs([{'name': f.get('path') or f.get('filename'), 'size': f.get('size'), 'url': f.get('_links', {}).get('stash:download', {}).get('href')} for f in fp.get('_embedded', {}).get('stash:files', [])]))
                    yield {'repository': repository, 'id': str(doi or ds.get('id')), 'url': 'https://datadryad.org/dataset/doi:'+str(doi) if doi else ds.get('_links', {}).get('self', {}).get('href', ''),
                           'title': ds.get('title'), 'description': ds.get('abstract'), 'doi': ds.get('relatedPublicationDOI'), 'files': files}
                if not datasets or len(datasets) < 25:
                    break
        else:
            raise ValueError('Unsupported repository')
