"""Documented Europe PMC adapter, bounded retries, guarded resource verification."""
import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'


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
    def __init__(self, sleeper=time.sleep):
        self.sleeper = sleeper
        self.lock = threading.Lock()
        self.last = 0.0

    def get(self, url, limit=8_000_000, callback=None):
        public_url(url)
        for attempt in range(3):
            with self.lock:
                self.sleeper(max(0, 0.35 - (time.monotonic() - self.last)))
                self.last = time.monotonic()
            try:
                req = Request(url, headers={'User-Agent': 'Orchard/0.1 (local research tool)', 'Accept': 'application/json, application/xml, text/html;q=0.8, */*;q=0.5'})
                with build_opener(SafeRedirect()).open(req, timeout=12) as response:
                    data = response.read(limit + 1)
                    if len(data) > limit:
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


class EuropePMC:
    name = 'europepmc'
    version = 'rest-v1-core'

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

    def fulltext(self, pmcid, callback=None):
        import re
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


def normalize_epmc(raw):
    if not isinstance(raw, dict) or not raw.get('id') or not raw.get('source'):
        raise ValueError('Provider record has no source/id.')
    authors = raw.get('authorString')
    types = raw.get('pubTypeList', {}).get('pubType', [])
    if not isinstance(types, list):
        types = []
    return {'source_id': str(raw['source']) + ':' + str(raw['id']), 'title': raw.get('title'),
            'abstract': raw.get('abstractText'), 'authors': authors, 'year': raw.get('pubYear'),
            'doi': raw.get('doi'), 'publication_type': '; '.join(types) or None,
            'pmid': raw.get('pmid'), 'pmcid': raw.get('pmcid')}
