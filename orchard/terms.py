"""Conservative extraction: original lines survive, grouping is only a proposal."""
import re
import unicodedata


def normalize(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value).strip()).casefold()


def parse_document(text):
    if not isinstance(text, str) or not text.strip() or '\x00' in text:
        raise ValueError('Import a non-empty UTF-8 text or Markdown document.')
    if len(text.encode()) > 2_000_000:
        raise ValueError('Document exceeds 2 MB.')
    groups = []
    heading = 'Unclassified terms'
    candidates = []
    screening_section = False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith('HOW TO SCREEN CANDIDATES'):
            screening_section = True
        if line.lower().startswith('blocks of search'):
            screening_section = False
        if screening_section:
            candidates.append({'line': n, 'text': raw, 'kind': 'screening_guidance'})
            continue
        if re.search(r'\b(?:AND|OR|NOT)\b', line):
            candidates.append({'line': n, 'text': raw, 'kind': 'query_example'})
            continue
        # Tab-indented lists and semicolon-separated rows are explicit term lists.
        if raw.startswith('\t') or (';' in line and len(line) < 1600):
            parts = re.split(r';|,', line)
            groups.append({'label': heading, 'terms': [{'original': t.strip(), 'normalized': normalize(t), 'line': n,
                             'reason': 'Explicit list under source heading; related terms are not assumed equivalent'}
                            for t in parts if t.strip()]})
        elif line.startswith('* ') and ': ' in line and len(line) < 1500:
            label, body = line[2:].split(': ', 1)
            if ',' in body or ';' in body:
                groups.append({'label': label, 'terms': [{'original': t.strip(), 'normalized': normalize(t), 'line': n,
                               'reason': 'Source labeled list; grouping needs review'} for t in re.split(r';|,', body) if t.strip()]})
        elif len(line) < 100 and not re.match(r'\d+\.', line):
            heading = line.strip('#* :')
        else:
            candidates.append({'line': n, 'text': raw, 'kind': 'unparsed_text'})
    if not groups:
        # Plain one-term-per-line imports remain usable, without inventing synonyms.
        groups = [{'label': 'Imported lines', 'terms': [{'original': t.strip(), 'normalized': normalize(t), 'line': n,
                   'reason': 'One non-empty source line; review before query generation'}
                  for n, t in enumerate(text.splitlines(), 1) if t.strip() and len(t) < 150 and not re.search(r'\b(AND|OR|NOT)\b', t)]}]
    return groups, candidates


def quote_term(term):
    # Original values are untouched; these are provider-specific query expressions.
    t = term.strip().strip('“”"').replace('"', '')
    if not t or any(c in t for c in '[]():'):
        raise ValueError(f'The selected term “{term.strip()}” contains search syntax. Deselect it or edit the exact query manually.')
    return '"' + t + '"' if ' ' in t else t


def build_query(groups):
    if not groups:
        raise ValueError('Select at least one concept group.')
    blocks = []
    for terms in groups:
        seen = set()
        expr = []
        for term in terms:
            key = normalize(term)
            if key not in seen:
                expr.append(quote_term(term))
                seen.add(key)
        if not expr:
            raise ValueError('Each selected group needs terms.')
        blocks.append('(' + ' OR '.join(expr) + ')')
    query = ' AND '.join(blocks)
    if len(query) > 6000:
        raise ValueError('Query is too large. Select fewer concepts or split the search.')
    return query
