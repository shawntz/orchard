"""Find the publication(s) associated with a repository project, with evidence.

Order of evidence:
1. Explicit links recorded by the repository (Zenodo related identifiers, Dryad related works,
   OSF registration article DOIs and linked preprints, GitHub CITATION.cff preferred-citation).
2. DOIs in the project's own documentation, classified first: a repository DOI usually
   identifies the dataset or software itself, not a paper.
3. Only when 1-2 yield no paper: bibliographic search (Crossref, OpenAlex, Europe PMC) by title
   and contributors. A search result becomes a candidate only with author overlap or a near-
   identical distinctive title; shared common words ("attention", "pupil", "visual") never count.

Nothing is attached automatically. Every candidate needs a researcher's confirmation, and every
search, result count and rejected near-miss is kept so "no publication identified" is traceable.
"""
import re
import unicodedata
from urllib.parse import quote, urlencode

DOI = re.compile(r'\b(10\.\d{4,9}/[^\s"<>\])}]+)', re.I)
DATASET_PREFIXES = ('10.5061/dryad', '10.7910/dvn', '10.18112/openneuro', '10.6084/m9.figshare', '10.17632/', '10.12751/g-node')
STOP = set('a an and are as at be by for from has have in into is it its of on or that the this to was were with we our their via using use used'.split())
# Common in pupillometry and psychology titles, so they carry little identifying weight.
GENERIC = set('''pupil pupils pupillary pupillometry pupillometric eye eyes tracking tracker gaze attention attentional visual memory
cognitive cognition effect effects task tasks response responses study studies data dataset datasets experiment experiments
evidence human humans analysis role during under between across new based measure measures size dilation diameter
arousal processing information test tests stimuli stimulus behavior behaviour behavioral behavioural participants
neural brain perception perceptual learning working load control spatial temporal individual differences'''.split())
CONTEXT = re.compile(r'(?i)(this|our|the accompanying|associated|corresponding|related) (paper|article|manuscript|publication|preprint|study)|please cite|cite (this|the following|as)|citation|published (in|as)|described in|reported in|preprint|accepted (in|at|for)|in press')
STRONG_RELATIONS = {'issupplementto', 'isdocumentedby', 'isdescribedby', 'iscitedby', 'isreferencedby', 'primary_article', 'article', 'article_doi',
                    'osf_preprint', 'preferred-citation', 'issupplementedby', 'issourceof'}
PUBLICATION_TYPES = {'journal-article': 'article', 'proceedings-article': 'article', 'book-chapter': 'article', 'posted-content': 'preprint',
                     'report': 'article', 'dissertation': 'thesis', 'dataset': 'dataset', 'component': 'dataset', 'peer-review': 'other',
                     'book': 'article', 'monograph': 'article'}


def fold(text):
    return unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()


def tokens(title):
    return [w for w in re.findall(r'[a-z0-9]+', fold(title)) if len(w) > 2 and w not in STOP]


def clean_title(title):
    """Strip dataset prefixes so 'Data from: <paper title>' compares with the paper title."""
    title = re.sub(r'(?i)^\s*((replication |supplementary |supporting |open |raw |source |analysis )?(data|dataset|datasets|code|materials?|files|scripts|code and data|data and code)( for| from| of| accompanying| supporting)?|supplementary materials? (for|to))\s*[:\-–—]\s*', '', str(title or ''))
    title = re.sub(r'[_\-]+', ' ', title) if ' ' not in title.strip() else title
    return title.strip()


def surnames(names):
    out = set()
    for name in names or []:
        name = fold(name)
        if ',' in name:
            name = name.split(',')[0]
        else:
            parts = name.split()
            name = parts[-1] if parts else ''
        name = re.sub(r'[^a-z\-]', '', name)
        if len(name) > 1:
            out.add(name)
    return out


def title_evidence(project_title, candidate_title):
    a, b = tokens(clean_title(project_title)), tokens(candidate_title)
    if not a or not b:
        return {'exact': False, 'shared': [], 'distinctive_shared': [], 'distinctive_coverage': 0.0}
    exact = ' '.join(a) == ' '.join(b)
    da, db = {w for w in a if w not in GENERIC}, {w for w in b if w not in GENERIC}
    shared = sorted(set(a) & set(b))
    dshared = sorted(da & db)
    coverage = len(dshared) / max(1, min(len(da), len(db))) if da and db else 0.0
    return {'exact': exact, 'shared': shared, 'distinctive_shared': dshared, 'distinctive_coverage': round(coverage, 2),
            'distinctive_project': sorted(da), 'distinctive_candidate': sorted(db)}


def judge(project, candidate):
    """Return (strength or None, evidence lines). ``project``: title, creators, created; ``candidate``: title, authors, year."""
    t = title_evidence(project.get('title'), candidate.get('title'))
    authors = surnames(project.get('creators')) & surnames(candidate.get('authors'))
    lines = []
    if authors:
        lines.append('Shared author surname(s): ' + ', '.join(sorted(authors)))
    if t['exact']:
        lines.append('Titles are identical after removing case, punctuation and dataset prefixes.')
    elif t['distinctive_shared']:
        lines.append(f"Shared distinctive title words: {', '.join(t['distinctive_shared'])} ({int(t['distinctive_coverage'] * 100)}% of the shorter title's distinctive words)")
    year, created = str(candidate.get('year') or ''), str(project.get('created') or '')[:4]
    if year.isdigit() and created.isdigit():
        lines.append(f'Published {year}; repository created {created}.')
    if t['exact'] and (authors or len(t['distinctive_project']) >= 3):
        return 'moderate' if authors else 'weak', lines
    if authors and len(t['distinctive_shared']) >= 2 and t['distinctive_coverage'] >= 0.34:
        return 'moderate', lines
    if not authors and len(t['distinctive_shared']) >= 4 and t['distinctive_coverage'] >= 0.75:
        return 'weak', lines + ['No author overlap: confirm against the article before accepting.']
    reason = ('no shared author' if not authors else 'author shared but titles differ') + f"; distinctive words shared: {', '.join(t['distinctive_shared']) or 'none'}"
    return None, [reason]


def normalize_doi(value):
    match = DOI.search(str(value or '').replace('https://doi.org/', '').replace('http://dx.doi.org/', ''))
    return match.group(1).rstrip('.,;').lower() if match else None


class Lookup:
    """DOI classification and bibliographic search through a rate-limited ``repositories.Client``."""

    def __init__(self, client, contact=None):
        self.client, self.contact = client, contact

    def crossref_work(self, doi):
        return self.client.json('https://api.crossref.org/works/' + quote(doi, safe='/') + ('?' + urlencode({'mailto': self.contact}) if self.contact else '')).get('message') or {}

    def classify(self, doi):
        """Return {'doi', 'kind', 'type', 'title', 'authors', 'year', 'venue', 'registrar', 'raw'}."""
        doi = normalize_doi(doi)
        if not doi:
            return None
        if doi.startswith(DATASET_PREFIXES):
            return {'doi': doi, 'kind': 'dataset', 'type': 'repository DOI prefix', 'registrar': 'prefix', 'title': None, 'authors': [], 'year': None, 'venue': None, 'raw': {}}
        try:
            work = self.crossref_work(doi)
            kind = PUBLICATION_TYPES.get(work.get('type'), 'other')
            return {'doi': doi, 'kind': kind, 'type': work.get('type'), 'registrar': 'Crossref', 'title': ' '.join(work.get('title') or []) or None,
                    'authors': [' '.join(filter(None, [a.get('given'), a.get('family')])) or a.get('name') for a in work.get('author') or []],
                    'year': ((work.get('issued') or {}).get('date-parts') or [[None]])[0][0], 'venue': ' '.join(work.get('container-title') or []) or None, 'raw': work}
        except Exception as exc:
            crossref_error = exc
        try:
            attrs = self.client.json('https://api.datacite.org/dois/' + quote(doi, safe='/')).get('data', {}).get('attributes', {})
            general = ((attrs.get('types') or {}).get('resourceTypeGeneral') or '').lower()
            kind = {'dataset': 'dataset', 'software': 'software', 'preprint': 'preprint', 'journalarticle': 'article', 'text': 'other',
                    'collection': 'dataset', 'conferencepaper': 'article'}.get(general, 'other')
            return {'doi': doi, 'kind': kind, 'type': 'DataCite ' + (general or 'unknown'), 'registrar': 'DataCite',
                    'title': ((attrs.get('titles') or [{}])[0]).get('title'), 'authors': [c.get('name') for c in attrs.get('creators') or []],
                    'year': attrs.get('publicationYear'), 'venue': attrs.get('publisher'), 'raw': attrs}
        except Exception as exc:
            return {'doi': doi, 'kind': 'unresolved', 'type': None, 'registrar': None, 'title': None, 'authors': [], 'year': None, 'venue': None,
                    'raw': {'crossref_error': str(crossref_error), 'datacite_error': str(exc)}}

    def search(self, title, creators):
        """Bibliographic search in three sources; returns (results, log)."""
        results, log = [], []
        query = clean_title(title)
        names = sorted(surnames(creators))
        mail = {'mailto': self.contact} if self.contact else {}
        sources = [('Crossref', 'https://api.crossref.org/works?' + urlencode({'query.bibliographic': query, 'rows': 10, **mail}))]
        if names:
            sources.append(('Crossref', 'https://api.crossref.org/works?' + urlencode({'query.bibliographic': query, 'query.author': ' '.join(names[:3]), 'rows': 10, **mail})))
        sources += [
                    ('OpenAlex', 'https://api.openalex.org/works?' + urlencode({'search': query, 'per-page': 10, **mail}))]
        if names:
            sources.append(('Europe PMC', 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?' + urlencode({'query': ' OR '.join(f'AUTH:"{n}"' for n in names[:3]), 'format': 'json', 'pageSize': 25, 'resultType': 'lite'})))
        for name, url in sources:
            try:
                data = self.client.json(url)
                items = parse_search(name, data)
                log.append({'source': name, 'url': url, 'results': len(items), 'error': None})
                results += items
            except Exception as exc:
                log.append({'source': name, 'url': url, 'results': None, 'error': f'{type(exc).__name__}: {exc}'[:300]})
        return results, log


def parse_search(source, data):
    out = []
    if source == 'Crossref':
        for w in (data.get('message') or {}).get('items') or []:
            out.append({'source': source, 'doi': normalize_doi(w.get('DOI')), 'title': ' '.join(w.get('title') or []), 'type': w.get('type'),
                        'kind': PUBLICATION_TYPES.get(w.get('type'), 'other'),
                        'authors': [' '.join(filter(None, [a.get('given'), a.get('family')])) or a.get('name', '') for a in w.get('author') or []],
                        'year': ((w.get('issued') or {}).get('date-parts') or [[None]])[0][0], 'venue': ' '.join(w.get('container-title') or []) or None})
    elif source == 'OpenAlex':
        for w in data.get('results') or []:
            out.append({'source': source, 'doi': normalize_doi(w.get('doi')), 'title': w.get('display_name') or '', 'type': w.get('type'),
                        'kind': {'article': 'article', 'preprint': 'preprint', 'dataset': 'dataset', 'book-chapter': 'article'}.get(w.get('type'), 'other'),
                        'authors': [(a.get('author') or {}).get('display_name') or '' for a in w.get('authorships') or []],
                        'year': w.get('publication_year'), 'venue': ((w.get('primary_location') or {}).get('source') or {}).get('display_name')})
    else:
        for w in (data.get('resultList') or {}).get('result') or []:
            out.append({'source': source, 'doi': normalize_doi(w.get('doi')), 'title': w.get('title') or '', 'type': w.get('pubType'),
                        'kind': 'preprint' if w.get('source') == 'PPR' else 'article',
                        'authors': [a.strip() for a in (w.get('authorString') or '').rstrip('.').split(',') if a.strip()],
                        'year': w.get('pubYear'), 'venue': w.get('journalTitle'), 'pmid': w.get('pmid'), 'pmcid': w.get('pmcid')})
    return out


def explicit_relations(meta, docs):
    """Publication pointers the repository itself records."""
    out = []
    for r in meta.get('related') or []:
        relation = str(r.get('relation') or '').replace(' ', '').lower()
        rtype = str(r.get('resource_type') or '').lower()
        ident = r.get('identifier')
        is_pub = rtype.startswith('publication') or rtype in ('preprint', 'publication') or relation in ('primary_article', 'article', 'article_doi', 'osf_preprint')
        if not ident or rtype.startswith(('dataset', 'software', 'image', 'other')) and not is_pub:
            continue
        strength = 'explicit' if relation in STRONG_RELATIONS and (is_pub or relation in ('issupplementto', 'isdocumentedby', 'isdescribedby')) else 'mentioned'
        out.append({'identifier': ident, 'title': r.get('title'), 'relation': r.get('relation'), 'resource_type': r.get('resource_type'),
                    'strength': strength, 'origin': (r.get('origin') or 'Repository relation') + f" ({r.get('relation')})"})
    for doc in docs:
        if doc.get('kind') == 'citation' and re.search(r'(?i)citation\.cff', doc.get('title') or ''):
            block = re.search(r'(?ms)^preferred-citation:\s*\n(.*?)(?=^\S|\Z)', doc.get('text') or '')
            doi = normalize_doi(block.group(1)) if block else None
            if doi:
                title = re.search(r'(?m)^\s+title:\s*["\']?(.+?)["\']?\s*$', block.group(1))
                out.append({'identifier': doi, 'title': title.group(1) if title else None, 'relation': 'preferred-citation', 'resource_type': 'publication',
                            'strength': 'explicit', 'origin': 'CITATION.cff preferred-citation'})
    return out


def documentation_dois(docs, own_doi=None):
    """DOIs mentioned in documentation, with the sentence around each."""
    found = {}
    own = normalize_doi(own_doi)
    for doc in docs:
        text = doc.get('text') or ''
        for match in DOI.finditer(text):
            doi = normalize_doi(match.group(1))
            if not doi or doi == own or doi in found:
                continue
            context = text[max(0, match.start() - 250):match.end() + 150].replace('\n', ' ')
            found[doi] = {'identifier': doi, 'context': context, 'source': doc.get('title') or doc.get('kind'), 'cue': bool(CONTEXT.search(context))}
    return list(found.values())


def find(lookup, meta, docs, search=True):
    """Build candidates for one project. Returns dict with candidates, non_publications, searches, rejected."""
    project = {'title': meta.get('title'), 'creators': meta.get('creators') or [], 'created': meta.get('created')}
    candidates, non_pubs, rejected = {}, [], []

    def add(info, strength, origin, evidence):
        key = info.get('doi') or info.get('url') or info.get('title')
        if not key:
            return
        current = candidates.get(key)
        rank = {'explicit': 0, 'moderate': 1, 'weak': 2, 'mentioned': 3}
        if current and rank[current['strength']] <= rank[strength]:
            current['evidence'] += [e for e in evidence if e not in current['evidence']]
            current['origin'] += '; ' + origin if origin not in current['origin'] else ''
            return
        candidates[key] = {'doi': info.get('doi'), 'url': info.get('url') or (('https://doi.org/' + info['doi']) if info.get('doi') else None),
                           'title': info.get('title'), 'authors': info.get('authors') or [], 'year': info.get('year'), 'venue': info.get('venue'),
                           'kind': info.get('kind'), 'strength': strength, 'origin': origin, 'evidence': evidence, 'raw': info.get('raw') or {}}

    for rel in explicit_relations(meta, docs):
        doi = normalize_doi(rel['identifier'])
        info = lookup.classify(doi) if doi else {'url': rel['identifier'], 'title': rel.get('title'), 'kind': 'publication'}
        if info and info.get('kind') in ('dataset', 'software'):
            non_pubs.append({**rel, 'kind': info['kind'], 'note': 'Related identifier is a ' + info['kind'] + ', not a paper.'})
            continue
        add(info or {'doi': doi}, 'explicit' if rel['strength'] == 'explicit' else 'mentioned', rel['origin'],
            [f"The repository records this as {rel.get('relation')}" + (f" ({rel.get('resource_type')})" if rel.get('resource_type') else '') + '.'])
    for mention in documentation_dois(docs, meta.get('doi')):
        info = lookup.classify(mention['identifier'])
        if not info:
            continue
        if info['kind'] in ('dataset', 'software', 'other', 'unresolved'):
            non_pubs.append({'identifier': mention['identifier'], 'kind': info['kind'], 'type': info.get('type'), 'context': mention['context'],
                             'note': 'DOI in documentation identifies a ' + info['kind'] + (f" ({info.get('type')})" if info.get('type') else '') + ', not a paper.'})
            continue
        strength, lines = judge(project, info)
        shared = surnames(project['creators']) & surnames(info.get('authors'))
        if mention['cue'] and shared and strength in (None, 'weak'):
            strength = 'moderate'
            lines = ['Cited with citation wording and shares author surname(s): ' + ', '.join(sorted(shared))] + lines[1:]
        if mention['cue'] or strength:
            add(info, strength or 'mentioned', 'DOI in ' + str(mention['source']),
                [('Mentioned with citation wording: “' if mention['cue'] else 'Mentioned in documentation: “') + mention['context'].strip()[:300] + '”'] + lines)
        else:
            non_pubs.append({'identifier': mention['identifier'], 'kind': info['kind'], 'title': info.get('title'), 'context': mention['context'],
                             'note': 'Article DOI mentioned without citation wording or shared authors (often a method or background reference). Not proposed.'})
    searches = []
    if search and not any(c['strength'] == 'explicit' for c in candidates.values()) and project['title']:
        results, searches = lookup.search(project['title'], project['creators'])
        for item in results:
            if item.get('kind') in ('dataset', 'other') or not item.get('title'):
                continue
            if item.get('doi') and normalize_doi(item['doi']) == normalize_doi(meta.get('doi')):
                continue
            strength, lines = judge(project, item)
            if strength:
                add(item, strength, f"{item['source']} search by title and contributors", lines)
            else:
                rejected.append({'source': item['source'], 'doi': item.get('doi'), 'title': item.get('title'), 'year': item.get('year'), 'reason': lines[0]})
    ordered = sorted(candidates.values(), key=lambda c: {'explicit': 0, 'moderate': 1, 'weak': 2, 'mentioned': 3}[c['strength']])
    return {'candidates': ordered, 'non_publications': non_pubs, 'searches': searches, 'rejected': rejected[:25]}
