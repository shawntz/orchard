"""Search and full-text location adapters, bounded retries, guarded resource verification."""
import ipaddress
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
PDF_LIMIT = 40_000_000


def parse_article_xml(data):
    """Accept an external DOCTYPE label without loading DTDs or custom entities."""
    from xml.parsers import expat

    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def doctype(name, system_id, public_id, has_internal_subset):
        if has_internal_subset:
            raise ValueError('XML internal DTD subsets are unsupported; import authorized plain text instead.')

    def reject_entity(*args):
        raise ValueError('XML custom or external entities are unsupported.')

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = reject_entity
    parser.ExternalEntityRefHandler = reject_entity
    parser.SkippedEntityHandler = reject_entity
    parser.Parse(data, True)
    return ET.fromstring(data)


def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError('Only public HTTP(S) URLs on standard ports are allowed.')
    for item in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80)):
        if not ipaddress.ip_address(item[4][0]).is_global:
            raise ValueError('Private and local network resources cannot be fetched.')
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self, sleeper=time.sleep, interval=0.35):
        self.sleeper = sleeper
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    def get(self, url, limit=8_000_000, callback=None, headers=None, truncate=False):
        """Fetch at most ``limit`` bytes. ``truncate`` returns the prefix instead of failing (range probes)."""
        public_url(url)
        contact = os.environ.get('ORCHARD_CONTACT_EMAIL')
        agent = 'Orchard/0.2 (local research tool' + ('; mailto:' + contact if contact else '') + ')'
        for attempt in range(3):
            with self.lock:
                self.sleeper(max(0, self.interval - (time.monotonic() - self.last)))
                self.last = time.monotonic()
            try:
                req = Request(url, headers={'User-Agent': agent, 'Accept': 'application/json, application/xml, application/pdf, text/html;q=0.8, */*;q=0.5', **(headers or {})})
                with build_opener(SafeRedirect()).open(req, timeout=12) as response:
                    data = response.read(limit + 1)
                    if len(data) > limit and truncate:
                        data = data[:limit]
                    elif len(data) > limit:
                        raise ValueError('Response exceeds safe size limit.')
                    return data, response.geturl(), dict(response.headers)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                code = getattr(exc, 'code', None)
                transient = code in (429, 500, 502, 503, 504) or code is None
                if callback:
                    callback({'attempt': attempt + 1, 'status': code, 'error': str(exc), 'retry': transient and attempt < 2})
                if not transient or attempt == 2:
                    raise
                delay = 2 ** attempt
                if code == 429:
                    value = exc.headers.get('Retry-After', '')
                    if value.isdigit():
                        delay = min(10, int(value))
                self.sleeper(delay)


@dataclass
class Page:
    records: list
    cursor: str | None
    total: int


def env(name):
    return os.environ.get(name, '').strip() or None


def contact_params(key='mailto'):
    email = env('ORCHARD_CONTACT_EMAIL')
    return {key: email} if email else {}


TOKENS = re.compile(r'"[^"]*"|[()]|[^\s()"]+')


def require_portable(query, label):
    """Queries are written in Europe PMC Boolean syntax; only fielded syntax is provider-specific."""
    for token in TOKENS.findall(query):
        if not token.startswith('"') and (re.match(r'[A-Za-z_]+:', token) or re.search(r'\[[A-Za-z ]+\]$', token)):
            raise ValueError(f'{label} cannot run this query: {token!r} uses database-specific field syntax. Remove the field tag or run it on Europe PMC only.')
    return query


def semantic_scholar_query(query):
    """Translate AND/OR/NOT to Semantic Scholar bulk syntax (+, |, -). Phrases and grouping are kept."""
    out, prefix = [], ''
    for token in TOKENS.findall(require_portable(query, 'Semantic Scholar')):
        if token in ('AND', 'NOT'):
            prefix = '-' if token == 'NOT' else prefix or '+'
        elif token == 'OR':
            out.append('|')
            prefix = ''
        elif token == ')':
            out.append(token)
        else:
            out.append(prefix + token)
            prefix = ''
    return ' '.join(out).replace('( ', '(').replace(' )', ')')


def pmcid_normalize(value):
    match = re.search(r'(?:PMC)?(\d+)/?$', str(value or '').strip(), re.I)
    return 'PMC' + match.group(1) if value and match else None


def strip_prefix(value, *prefixes):
    if not isinstance(value, str):
        return None
    for prefix in prefixes:
        if value.lower().startswith(prefix):
            return value[len(prefix):]
    return value


def as_text(value):
    return None if value in (None, '') else str(value)


def link(url, kind, evidence):
    return {'url': url, 'kind': kind, 'evidence': evidence if isinstance(evidence, str) else json.dumps(evidence, ensure_ascii=False, sort_keys=True)}


def check_page(ok, label):
    if not ok:
        raise ValueError(f'Malformed {label} response.')


class EuropePMC:
    name = 'europepmc'
    label = 'Europe PMC'
    version = 'rest-v1-core'
    roles = ('search', 'fulltext', 'locate')
    configured = True
    note = 'No API key required.'

    def __init__(self, transport=None):
        self.transport = transport or Transport()

    def page(self, query, cursor='*', size=100, callback=None):
        params = {'query': query, 'format': 'json', 'resultType': 'core', 'cursorMark': cursor, 'pageSize': size}
        data, _, _ = self.transport.get(BASE + '/search?' + urlencode(params), callback=callback)
        result = json.loads(data)
        if not isinstance(result, dict) or 'hitCount' not in result or not isinstance(result.get('resultList'), dict):
            raise ValueError('Malformed provider response: resultList/hitCount absent.')
        records = result['resultList'].get('result', [])
        if not isinstance(records, list) or not isinstance(result['hitCount'], int) or result['hitCount'] < 0:
            raise ValueError('Malformed result list or total.')
        return Page(records, result.get('nextCursorMark'), result['hitCount'])

    def locate(self, ids, callback=None):
        """Find a PMCID and Europe PMC full-text links for a paper discovered elsewhere."""
        if ids.get('pmid'):
            query = 'EXT_ID:' + ids['pmid'] + ' AND SRC:MED'
        elif ids.get('doi'):
            query = 'DOI:"' + ids['doi'] + '"'
        else:
            return [], {}
        records = self.page(query, size=1, callback=callback).records
        if not records:
            return [], {}
        norm = normalize_epmc(records[0])
        return norm['links'], {'pmcid': norm['pmcid']}

    def fulltext(self, pmcid, callback=None):
        if not re.fullmatch(r'PMC\d+', pmcid):
            raise ValueError('Invalid PMCID.')
        data, url, _ = self.transport.get(BASE + '/' + pmcid + '/fullTextXML', callback=callback)
        root = parse_article_xml(data)
        pieces = []
        for elem in root.iter():
            if elem.tag.split('}')[-1] in ('p', 'title'):
                pieces.append(' '.join(elem.itertext()))
                for child in elem.iter():
                    for key, value in child.attrib.items():
                        if key.endswith('href') and value.startswith(('http://', 'https://')):
                            pieces.append(' '.join(elem.itertext()) + ' ' + value)
        return '\n'.join(pieces), url


EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'


def xml_text(elem):
    return ' '.join(' '.join(elem.itertext()).split()) or None if elem is not None else None


def pubmed_record(article):
    """Flatten one PubmedArticle; the original XML is retained alongside the parsed fields."""
    cit = article.find('MedlineCitation')
    art = cit.find('Article') if cit is not None else None
    if art is None:
        raise ValueError('PubMed article has no citation.')
    idlist = article.find('PubmedData/ArticleIdList')
    ids = {i.get('IdType'): xml_text(i) for i in (idlist if idlist is not None else [])}
    doi = ids.get('doi') or next((xml_text(e) for e in art.findall('ELocationID') if e.get('EIdType') == 'doi'), None)
    abstract = '\n'.join((a.get('Label') + ': ' if a.get('Label') else '') + (xml_text(a) or '') for a in art.findall('Abstract/AbstractText'))
    authors = []
    for author in art.findall('AuthorList/Author'):
        name = ' '.join(filter(None, (xml_text(author.find('LastName')), xml_text(author.find('Initials'))))) or xml_text(author.find('CollectiveName'))
        if name:
            authors.append(name)
    year = xml_text(art.find('Journal/JournalIssue/PubDate/Year')) or (xml_text(art.find('Journal/JournalIssue/PubDate/MedlineDate')) or '')[:4] or xml_text(art.find('ArticleDate/Year'))
    return {'pmid': xml_text(cit.find('PMID')), 'title': xml_text(art.find('ArticleTitle')), 'abstract': abstract or None,
            'authors': ', '.join(authors) or None, 'journal': xml_text(art.find('Journal/Title')), 'year': year or None,
            'doi': doi, 'pmcid': ids.get('pmc'), 'publication_types': [xml_text(t) for t in art.findall('PublicationTypeList/PublicationType')],
            'xml': ET.tostring(article, encoding='unicode')}


class PubMed:
    name = 'pubmed'
    label = 'PubMed'
    version = 'eutils-v1'
    roles = ('search',)
    configured = True
    note = 'No API key required; ORCHARD_NCBI_API_KEY raises the rate limit.'

    def __init__(self, transport=None):
        self.api_key = env('ORCHARD_NCBI_API_KEY')
        self.transport = transport or Transport(interval=0.12 if self.api_key else 0.36)

    def params(self, **extra):
        params = {'db': 'pubmed', 'tool': 'orchard', **contact_params('email'), **extra}
        if self.api_key:
            params['api_key'] = self.api_key
        return params

    def page(self, query, cursor='*', size=100, callback=None):
        start = 0 if cursor in (None, '*') else int(cursor)
        data, _, _ = self.transport.get(EUTILS + '/esearch.fcgi?' + urlencode(self.params(term=require_portable(query, 'PubMed'), retmode='json', retstart=start, retmax=size)), callback=callback)
        result = json.loads(data).get('esearchresult')
        check_page(isinstance(result, dict) and str(result.get('count', '')).isdigit() and isinstance(result.get('idlist'), list), 'PubMed esearch')
        if result.get('ERROR'):
            raise ValueError('PubMed: ' + str(result['ERROR']))
        ids, total = [str(i) for i in result['idlist']], int(result['count'])
        records = []
        if ids:
            xml, _, _ = self.transport.get(EUTILS + '/efetch.fcgi?' + urlencode(self.params(id=','.join(ids), retmode='xml')), callback=callback)
            records = [pubmed_record(a) for a in parse_article_xml(xml).iter('PubmedArticle')]
        end = start + len(ids)
        return Page(records, str(end) if ids and end < total else None, total)


OPENALEX = 'https://api.openalex.org'
OPENALEX_FIELDS = 'id,doi,display_name,publication_year,ids,type,authorships,abstract_inverted_index,open_access,best_oa_location,primary_location,locations,is_retracted'


def openalex_links(work):
    links, seen = [], set()
    for loc in [work.get('best_oa_location')] + list(work.get('locations') or []):
        if not isinstance(loc, dict):
            continue
        evidence = {k: loc.get(k) for k in ('is_oa', 'version', 'license', 'landing_page_url', 'pdf_url')}
        evidence['source'] = (loc.get('source') or {}).get('display_name')
        for url, kind in ((loc.get('pdf_url'), 'pdf'), (loc.get('landing_page_url') if loc.get('is_oa') else None, 'fulltext')):
            if isinstance(url, str) and url not in seen:
                seen.add(url)
                links.append(link(url, kind, evidence))
    return links


class OpenAlex:
    name = 'openalex'
    label = 'OpenAlex'
    version = 'works-v1'
    roles = ('search', 'locate')
    configured = True
    note = 'No API key required; ORCHARD_OPENALEX_API_KEY gives a private daily budget and deep paging beyond 10,000 results.'
    per_page = 100

    def __init__(self, transport=None):
        self.transport = transport or Transport(interval=0.15)
        self.lock = threading.Lock()
        self.cache = None

    def params(self, **extra):
        params = {**contact_params(), **extra}
        if env('ORCHARD_OPENALEX_API_KEY'):
            params['api_key'] = env('ORCHARD_OPENALEX_API_KEY')
        return params

    def fetch(self, callback, **params):
        data, _, _ = self.transport.get(OPENALEX + '/works?' + urlencode(self.params(select=OPENALEX_FIELDS, **params)), callback=callback)
        result = json.loads(data)
        meta = result.get('meta') if isinstance(result, dict) else None
        check_page(isinstance(meta, dict) and isinstance(meta.get('count'), int) and isinstance(result.get('results'), list), 'OpenAlex')
        return result

    def page(self, query, cursor='*', size=100, callback=None):
        search = require_portable(query, 'OpenAlex')
        if env('ORCHARD_OPENALEX_API_KEY') and not str(cursor).startswith('offset:'):
            result = self.fetch(callback, search=search, cursor=cursor or '*', **{'per-page': min(size, 200)})
            return Page(result['results'], result['meta'].get('next_cursor'), result['meta']['count'])
        # Without a key OpenAlex only allows numbered pages (first 10,000 results); the cursor is "offset:N".
        offset = 0 if cursor in (None, '*') else int(str(cursor).split(':', 1)[1])
        number = offset // self.per_page + 1
        with self.lock:
            if not self.cache or self.cache[0] != (search, number):
                self.cache = ((search, number), self.fetch(callback, search=search, page=number, **{'per-page': self.per_page}))
            result = self.cache[1]
        records = result['results'][offset % self.per_page:offset % self.per_page + size]
        end = offset + len(records)
        following = 'offset:' + str(end) if records and end < min(result['meta']['count'], 10_000) else None
        return Page(records, following, result['meta']['count'])

    def locate(self, ids, callback=None):
        if not ids.get('doi'):
            return [], {}
        url = OPENALEX + '/works/' + quote('https://doi.org/' + ids['doi'], safe=':/') + '?' + urlencode(self.params(select=OPENALEX_FIELDS))
        work = json.loads(self.transport.get(url, callback=callback)[0])
        norm = normalize_openalex(work)
        return norm['links'], {k: norm[k] for k in ('pmid', 'pmcid', 'arxiv')}


S2 = 'https://api.semanticscholar.org/graph/v1'
S2_FIELDS = 'paperId,externalIds,title,abstract,year,authors,publicationTypes,venue,isOpenAccess,openAccessPdf'


class SemanticScholar:
    name = 'semanticscholar'
    label = 'Semantic Scholar'
    version = 'graph-v1-bulk'
    roles = ('search', 'locate')
    configured = True
    note = 'No API key required, but the shared pool is often rate-limited; ORCHARD_S2_API_KEY is recommended.'

    def __init__(self, transport=None):
        self.transport = transport or Transport(interval=1.05)
        self.lock = threading.Lock()
        self.cache = None

    def headers(self):
        return {'x-api-key': env('ORCHARD_S2_API_KEY')} if env('ORCHARD_S2_API_KEY') else {}

    def page(self, query, cursor='*', size=100, callback=None):
        # Bulk search returns up to 1,000 papers per token; the cursor is "token#offset" so the cap still applies.
        token, offset = ('', 0) if cursor in (None, '*') else (cursor.rsplit('#', 1)[0], int(cursor.rsplit('#', 1)[1]))
        translated = semantic_scholar_query(query)
        with self.lock:
            if not self.cache or self.cache[0] != (translated, token):
                params = {'query': translated, 'fields': S2_FIELDS, **({'token': token} if token else {})}
                result = json.loads(self.transport.get(S2 + '/paper/search/bulk?' + urlencode(params), callback=callback, headers=self.headers())[0])
                check_page(isinstance(result, dict) and isinstance(result.get('total'), int) and isinstance(result.get('data'), list), 'Semantic Scholar')
                self.cache = ((translated, token), result)
            result = self.cache[1]
        records = result['data'][offset:offset + size]
        end = offset + len(records)
        if end < len(result['data']):
            following = token + '#' + str(end)
        else:
            following = result['token'] + '#0' if result.get('token') else None
        return Page(records, following, result['total'])

    def locate(self, ids, callback=None):
        key = next((prefix + ids[kind] for kind, prefix in (('doi', 'DOI:'), ('pmid', 'PMID:'), ('arxiv', 'ARXIV:')) if ids.get(kind)), None)
        if not key:
            return [], {}
        paper = json.loads(self.transport.get(S2 + '/paper/' + quote(key, safe=':/') + '?' + urlencode({'fields': S2_FIELDS}), callback=callback, headers=self.headers())[0])
        norm = normalize_s2(paper)
        return norm['links'], {k: norm[k] for k in ('pmid', 'pmcid', 'arxiv')}


CORE = 'https://api.core.ac.uk/v3'


class Core:
    name = 'core'
    label = 'CORE'
    version = 'v3-works'
    roles = ('search', 'locate')
    note = 'Requires a free API key in ORCHARD_CORE_API_KEY (https://core.ac.uk/services/api).'

    def __init__(self, transport=None):
        self.transport = transport or Transport(interval=6.5)

    @property
    def configured(self):
        return bool(env('ORCHARD_CORE_API_KEY'))

    def get(self, params, callback):
        if not self.configured:
            raise ValueError('CORE is not configured. Set ORCHARD_CORE_API_KEY.')
        data, _, _ = self.transport.get(CORE + '/search/works?' + urlencode(params), callback=callback, headers={'Authorization': 'Bearer ' + env('ORCHARD_CORE_API_KEY')})
        result = json.loads(data)
        check_page(isinstance(result, dict) and isinstance(result.get('totalHits'), int) and isinstance(result.get('results'), list), 'CORE')
        return result

    def page(self, query, cursor='*', size=100, callback=None):
        start = 0 if cursor in (None, '*') else int(cursor)
        result = self.get({'q': require_portable(query, 'CORE'), 'limit': size, 'offset': start}, callback)
        end = start + len(result['results'])
        return Page(result['results'], str(end) if result['results'] and end < result['totalHits'] else None, result['totalHits'])

    def locate(self, ids, callback=None):
        if not ids.get('doi') or not self.configured:
            return [], {}
        links = []
        for work in self.get({'q': 'doi:"' + ids['doi'] + '"', 'limit': 3}, callback)['results']:
            if str(work.get('doi') or '').lower() == ids['doi']:
                links += normalize_core(work)['links']
        return links, {}


class Crossref:
    name = 'crossref'
    label = 'Crossref'
    roles = ('locate',)
    configured = True
    note = 'Full-text links registered by publishers; many require a subscription. Not used for Boolean search because its ranking ignores Boolean operators.'

    def __init__(self, transport=None):
        self.transport = transport or Transport()

    def locate(self, ids, callback=None):
        if not ids.get('doi'):
            return [], {}
        url = 'https://api.crossref.org/works/' + quote(ids['doi'], safe='/') + ('?' + urlencode(contact_params()) if contact_params() else '')
        message = json.loads(self.transport.get(url, callback=callback)[0]).get('message') or {}
        links = []
        for item in message.get('link') or []:
            if isinstance(item, dict) and isinstance(item.get('URL'), str):
                pdf = item.get('content-type') == 'application/pdf' or item['URL'].lower().endswith('.pdf')
                links.append(link(item['URL'], 'pdf' if pdf else 'fulltext', {**item, 'license': message.get('license')}))
        return links, {}


class Unpaywall:
    name = 'unpaywall'
    label = 'Unpaywall'
    roles = ('locate',)
    note = 'Requires your email address in ORCHARD_CONTACT_EMAIL.'

    def __init__(self, transport=None):
        self.transport = transport or Transport(interval=0.1)

    @property
    def configured(self):
        return bool(env('ORCHARD_CONTACT_EMAIL'))

    def locate(self, ids, callback=None):
        if not ids.get('doi') or not self.configured:
            return [], {}
        result = json.loads(self.transport.get('https://api.unpaywall.org/v2/' + quote(ids['doi'], safe='/') + '?' + urlencode(contact_params('email')), callback=callback)[0])
        links = []
        for loc in [result.get('best_oa_location')] + list(result.get('oa_locations') or []):
            if not isinstance(loc, dict):
                continue
            evidence = {k: loc.get(k) for k in ('host_type', 'version', 'license', 'evidence', 'url_for_landing_page')}
            if loc.get('url_for_pdf'):
                links.append(link(loc['url_for_pdf'], 'pdf', evidence))
            elif loc.get('url_for_landing_page'):
                links.append(link(loc['url_for_landing_page'], 'fulltext', evidence))
        return links, {}


def build_providers():
    """Search providers by name and full-text locators in the order they are consulted."""
    epmc, openalex, s2, core = EuropePMC(), OpenAlex(), SemanticScholar(), Core()
    providers = {p.name: p for p in (epmc, PubMed(), openalex, s2, core)}
    return providers, [epmc, Unpaywall(), openalex, s2, core, Crossref()]


def normalize_epmc(raw):
    if not isinstance(raw, dict) or not raw.get('id') or not raw.get('source'):
        raise ValueError('Provider record has no source/id.')
    authors = raw.get('authorString')
    types = raw.get('pubTypeList', {}).get('pubType', [])
    if not isinstance(types, list):
        types = []
    links = []
    full = raw.get('fullTextUrlList', {}).get('fullTextUrl', []) if isinstance(raw.get('fullTextUrlList'), dict) else []
    for item in full if isinstance(full, list) else []:
        if isinstance(item, dict) and isinstance(item.get('url'), str):
            links.append(link(item['url'], 'pdf' if item.get('documentStyle') == 'pdf' else 'fulltext', item))
    return {'source_id': str(raw['source']) + ':' + str(raw['id']), 'title': raw.get('title'),
            'abstract': raw.get('abstractText'), 'authors': authors, 'year': raw.get('pubYear'),
            'doi': raw.get('doi'), 'publication_type': '; '.join(types) or None,
            'pmid': raw.get('pmid'), 'pmcid': raw.get('pmcid'), 'arxiv': None, 'links': links}


def normalize_pubmed(raw):
    if not isinstance(raw, dict) or not raw.get('pmid'):
        raise ValueError('PubMed record has no PMID.')
    return {'source_id': 'PMID:' + raw['pmid'], 'title': raw.get('title'), 'abstract': raw.get('abstract'),
            'authors': raw.get('authors'), 'year': as_text(raw.get('year')), 'doi': raw.get('doi'),
            'publication_type': '; '.join(filter(None, raw.get('publication_types') or [])) or None,
            'pmid': raw['pmid'], 'pmcid': pmcid_normalize(raw.get('pmcid')), 'arxiv': None, 'links': []}


def openalex_abstract(index):
    if not isinstance(index, dict) or not index:
        return None
    words = sorted((pos, word) for word, positions in index.items() for pos in positions)
    return ' '.join(word for _, word in words)


def normalize_openalex(raw):
    if not isinstance(raw, dict) or not raw.get('id'):
        raise ValueError('OpenAlex record has no ID.')
    ids = raw.get('ids') or {}
    authors = [(a.get('author') or {}).get('display_name') for a in raw.get('authorships') or [] if isinstance(a, dict)]
    arxiv = next((m.group(1) for l in openalex_links(raw) for m in [re.search(r'arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?$', l['url'])] if m), None)
    return {'source_id': strip_prefix(raw['id'], 'https://openalex.org/'), 'title': raw.get('display_name') or raw.get('title'),
            'abstract': openalex_abstract(raw.get('abstract_inverted_index')), 'authors': ', '.join(filter(None, authors)) or None,
            'year': as_text(raw.get('publication_year')), 'doi': raw.get('doi'), 'publication_type': raw.get('type'),
            'pmid': strip_prefix(ids.get('pmid'), 'https://pubmed.ncbi.nlm.nih.gov/'), 'pmcid': pmcid_normalize(ids.get('pmcid')),
            'arxiv': arxiv, 'links': openalex_links(raw)}


def normalize_s2(raw):
    if not isinstance(raw, dict) or not raw.get('paperId'):
        raise ValueError('Semantic Scholar record has no paperId.')
    ext = raw.get('externalIds') or {}
    pdf = raw.get('openAccessPdf') or {}
    links = [link(pdf['url'], 'pdf', pdf)] if isinstance(pdf, dict) and pdf.get('url') else []
    return {'source_id': raw['paperId'], 'title': raw.get('title'), 'abstract': raw.get('abstract'),
            'authors': ', '.join(filter(None, (a.get('name') for a in raw.get('authors') or [] if isinstance(a, dict)))) or None,
            'year': as_text(raw.get('year')), 'doi': ext.get('DOI'), 'publication_type': '; '.join(raw.get('publicationTypes') or []) or None,
            'pmid': as_text(ext.get('PubMed')), 'pmcid': pmcid_normalize(ext.get('PubMedCentral')), 'arxiv': as_text(ext.get('ArXiv')), 'links': links}


def normalize_core(raw):
    if not isinstance(raw, dict) or not raw.get('id'):
        raise ValueError('CORE record has no ID.')
    links = []
    if isinstance(raw.get('downloadUrl'), str) and raw['downloadUrl']:
        links.append(link(raw['downloadUrl'], 'pdf', {'field': 'downloadUrl', 'url': raw['downloadUrl']}))
    for url in raw.get('sourceFulltextUrls') or []:
        if isinstance(url, str) and url not in (l['url'] for l in links):
            links.append(link(url, 'pdf' if url.lower().endswith('.pdf') else 'fulltext', {'field': 'sourceFulltextUrls', 'url': url}))
    return {'source_id': str(raw['id']), 'title': raw.get('title'), 'abstract': raw.get('abstract'),
            'authors': ', '.join(filter(None, (a.get('name') for a in raw.get('authors') or [] if isinstance(a, dict)))) or None,
            'year': as_text(raw.get('yearPublished')), 'doi': raw.get('doi'), 'publication_type': as_text(raw.get('documentType')),
            'pmid': as_text(raw.get('pubmedId')), 'pmcid': None, 'arxiv': as_text(raw.get('arxivId')), 'links': links}


NORMALIZERS = {'europepmc': normalize_epmc, 'manual-import': normalize_epmc, 'pubmed': normalize_pubmed,
               'openalex': normalize_openalex, 'semanticscholar': normalize_s2, 'core': normalize_core}
