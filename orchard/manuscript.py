"""Manuscript-ready supplementary tables built from one consistent database snapshot.

The full export (``Service.export_zip``) mirrors the database table by table. These tables
instead answer the questions a methods section and its supplement have to answer: what was
searched and when, how records flowed through screening, which reports were included or
excluded and why, and where their data are. Every row keeps its Orchard ID so it can be
traced back to the full export. Counts Orchard cannot know (such as studies) are left
blank and explained rather than inferred.
"""
import csv
import io
import json
import re
import zipfile
from html import unescape

PROVIDERS = {'europepmc': 'Europe PMC', 'manual-import': 'Manual import of provider records'}
DATA_KINDS = ('dataset', 'code', 'supplement', 'protocol', 'data_statement')
FEASIBILITY = ('target_finding', 'dataset_identity', 'data_level', 'timing_metadata', 'acquisition',
               'original_workflow', 'confounds', 'reuse_feasibility')
FINAL = {'included': 'Included after full-text review',
         'excluded_full_text': 'Excluded at full-text review',
         'excluded_title_abstract': 'Excluded at title/abstract screening',
         'not_retrieved': 'Full text could not be retrieved',
         'awaiting_full_text': 'Passed title/abstract screening; full-text decision pending',
         'title_abstract_unresolved': 'Title/abstract decision unresolved (uncertain or needs information)',
         'not_screened': 'Not yet screened'}

CITATION = [
    ('authors', 'Author list as displayed in Orchard (researcher corrections applied).'),
    ('year', 'Publication year.'),
    ('title', 'Article title (markup removed).'),
    ('journal', 'Journal or source title from the provider record, when present.'),
    ('volume', 'Journal volume, when present.'),
    ('issue', 'Journal issue, when present.'),
    ('pages', 'Page range or article number, when present.'),
    ('doi', 'Normalized DOI.'),
    ('pmid', 'PubMed ID.'),
    ('pmcid', 'PubMed Central ID.'),
    ('publication_type', 'Publication types reported by the provider.'),
    ('citation', 'Formatted reference: Authors (Year). Title. Journal, Volume(Issue), Pages. DOI link.'),
]

# file name, title, description, columns. Column descriptions double as the data dictionary.
TABLES = [
    ('table_s1_search_strategy.csv', 'Search strategy',
     'One row per database search or record import, in the order they were run.', [
         ('search_number', 'Sequential number used by the other tables (e.g. found_in_searches).'),
         ('database', 'Database or source searched.'),
         ('search_label', 'Researcher label of the approved query version.'),
         ('exact_query', 'Query string exactly as submitted to the database.'),
         ('date_started', 'UTC timestamp when the search started.'),
         ('date_finished', 'UTC timestamp when the search ended.'),
         ('status', 'completed, partial, failed or interrupted. Only completed searches retrieved every reported hit.'),
         ('database_hit_count', 'Number of hits the database reported for the query.'),
         ('records_retrieved', 'Records Orchard retrieved and stored for this search.'),
         ('retrieval_cap', 'Maximum records retrieved per attempt.'),
         ('repeat_of_search', 'search_number of an earlier identical search this one refreshed.'),
         ('notes', 'Errors or reasons a search is incomplete.'),
         ('query_version_id', 'Orchard ID of the approved query version.'),
         ('search_id', 'Orchard ID of the search run.'),
     ]),
    ('table_s2_search_terms.csv', 'Search terms',
     'Source terms behind each approved query, with their concept group and location in the imported terms document.', [
         ('search_label', 'Researcher label of the approved query version.'),
         ('concept_group', 'Concept group the term belongs to. Terms are OR-combined within a group; groups are AND-combined.'),
         ('term', 'Term exactly as written in the source document.'),
         ('normalized_term', 'Normalized form used to remove exact duplicates.'),
         ('source_document', 'Name of the imported terms document.'),
         ('source_line', 'Line number of the term in the source document.'),
         ('searches_run', 'search_number values of searches that used this query version.'),
         ('query_version_id', 'Orchard ID of the approved query version.'),
     ]),
    ('table_s3_eligibility_criteria.csv', 'Eligibility criteria',
     'Every version of the eligibility criteria, oldest first.', [
         ('criteria_version', 'Sequential version number.'),
         ('date_created', 'UTC timestamp when this version was saved.'),
         ('current', 'yes for the version in force at export.'),
         ('inclusion_criteria', 'Inclusion criteria, as written by the researcher.'),
         ('exclusion_criteria', 'Exclusion criteria, as written by the researcher.'),
         ('screening_decisions_recorded', 'Number of screening decisions recorded under this version.'),
         ('criteria_id', 'Orchard ID of the criteria version.'),
     ]),
    ('table_s4_prisma_flow.csv', 'PRISMA flow counts',
     'Counts for a PRISMA 2020 flow diagram, computed from the latest decision per record and stage.', [
         ('stage', 'PRISMA stage: Identification, Screening, Retrieval, Eligibility, Included, Pending.'),
         ('item', 'What is counted.'),
         ('n', 'Count. Blank when Orchard cannot determine it (see notes).'),
         ('notes', 'How the count was derived.'),
     ]),
    ('table_s5_included_studies.csv', 'Included reports',
     'Reports included after full-text review, with citation, screening and data-availability details.', [
         ('report_number', 'Sequential number, ordered by first author and year.'),
         *CITATION,
         ('full_text_reviewer', 'Reviewer who recorded the inclusion.'),
         ('full_text_decision_date', 'UTC timestamp of the inclusion decision.'),
         ('full_text_notes', 'Reviewer notes on the inclusion decision.'),
         ('criteria_version', 'Eligibility criteria version the decision was made under.'),
         ('data_links', 'Non-rejected dataset, code, supplement or protocol URLs found for the report.'),
         ('verified_data_links', 'Links a researcher inspected and confirmed.'),
         ('data_on_request', 'yes if the report states data are available on request.'),
         ('eyelink_verdict', 'Result of the latest completed EyeLink scan.'),
         ('eyelink_files', 'Number of .edf/.asc candidate files found in that scan.'),
         ('reuse_feasibility', 'Latest eyeris reuse-feasibility assessment.'),
         ('pdf_license', 'License of the stored open-access PDF, when known.'),
         ('orchard_id', 'Orchard ID of the canonical record.'),
     ]),
    ('table_s6_excluded_full_text.csv', 'Reports excluded at full text',
     'Reports excluded after full-text review, with the reason recorded by the reviewer.', [
         ('report_number', 'Sequential number, ordered by first author and year.'),
         *CITATION,
         ('exclusion_reason', 'Reason recorded with the exclusion.'),
         ('full_text_notes', 'Reviewer notes on the exclusion.'),
         ('full_text_reviewer', 'Reviewer who recorded the exclusion.'),
         ('full_text_decision_date', 'UTC timestamp of the exclusion.'),
         ('criteria_version', 'Eligibility criteria version the decision was made under.'),
         ('orchard_id', 'Orchard ID of the canonical record.'),
     ]),
    ('table_s7_screening_log.csv', 'Screening log',
     'Every unique record after duplicate consolidation, with its latest decision at each screening stage.', [
         ('record_number', 'Sequential number, ordered by first author and year.'),
         *CITATION,
         ('found_in_searches', 'search_number values of the searches that retrieved this record.'),
         ('merged_duplicates', 'Number of duplicate records merged into this one.'),
         ('title_abstract_decision', 'Latest title/abstract decision.'),
         ('title_abstract_reason', 'Reason recorded with that decision.'),
         ('title_abstract_reviewer', 'Reviewer who recorded it.'),
         ('title_abstract_date', 'UTC timestamp of that decision.'),
         ('full_text_decision', 'Latest full-text decision.'),
         ('full_text_reason', 'Reason recorded with that decision.'),
         ('full_text_reviewer', 'Reviewer who recorded it.'),
         ('full_text_date', 'UTC timestamp of that decision.'),
         ('final_status', 'Overall status derived from the latest decisions: ' + '; '.join(FINAL) + '.'),
         ('decision_events', 'Total screening decisions recorded, including revisions and merged duplicates.'),
         ('orchard_id', 'Orchard ID of the canonical record.'),
     ]),
    ('table_s8_data_availability.csv', 'Data and code availability',
     'Dataset, code, supplement, protocol and availability-statement links found for each record. Rejected candidates are omitted.', [
         ('record_number', 'record_number in the screening log.'),
         ('authors', 'Author list.'),
         ('year', 'Publication year.'),
         ('title', 'Article title.'),
         ('doi', 'DOI of the article.'),
         ('final_status', 'Screening status of the article (see the screening log).'),
         ('resource_type', 'dataset, code, supplement, protocol or data_statement.'),
         ('url', 'Link as found. Empty for availability statements without a link.'),
         ('repository', 'Host name of the link.'),
         ('verification_status', 'found_unverified, url_reachable, access_failed, researcher_verified or upon_request. A reachable URL is not a verified dataset.'),
         ('verification_note', 'What the researcher inspected, or the result of the URL check.'),
         ('verified_date', 'UTC timestamp of the latest check.'),
         ('found_in', 'Where the link was found (abstract, full text, PDF or EyeLink scan).'),
         ('evidence', 'Passage that the link was found in.'),
         ('orchard_id', 'Orchard ID of the canonical record.'),
     ]),
    ('table_s9_eyelink_and_reuse.csv', 'EyeLink data and reuse assessment',
     'Latest EyeLink scan and latest eyeris reuse-feasibility assessment for each record that has either.', [
         ('record_number', 'record_number in the screening log.'),
         ('authors', 'Author list.'),
         ('year', 'Publication year.'),
         ('title', 'Article title.'),
         ('doi', 'DOI of the article.'),
         ('final_status', 'Screening status of the article (see the screening log).'),
         ('eyelink_scan_date', 'UTC timestamp when the latest completed scan finished.'),
         ('eyelink_verdict', 'eyelink_confirmed, eyelink_candidates, archives_unchecked, none_found, no_repository_links or incomplete.'),
         ('repositories_checked', 'Number of repositories listed in that scan.'),
         ('eyelink_files', 'Number of .edf/.asc candidate files.'),
         ('eyelink_header_confirmed', 'Number of files whose header identifies an SR Research EyeLink recording.'),
         ('repository_urls', 'Repositories listed in that scan, with their listing status.'),
         *[(f, 'Reuse assessment: ' + f.replace('_', ' ') + '.') for f in FEASIBILITY],
         ('assessment_date', 'UTC timestamp of the latest reuse assessment.'),
         ('orchard_id', 'Orchard ID of the canonical record.'),
     ]),
]


def clean(text):
    """Single-line plain text: markup removed, whitespace collapsed."""
    if text is None:
        return ''
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', str(text)))).strip()


def safe(value):
    # Neutralize spreadsheet formulas, as in the full export.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def to_csv(columns, rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: safe(row.get(k)) for k in columns})
    # BOM so spreadsheet programs read accented author names as UTF-8.
    return '﻿' + output.getvalue()


def journal_info(raws):
    """Journal, volume, issue and pages from the first provider record that has them."""
    for raw in raws:
        info = raw.get('journalInfo') if isinstance(raw.get('journalInfo'), dict) else {}
        journal = info.get('journal') if isinstance(info.get('journal'), dict) else {}
        title = journal.get('title') or journal.get('medlineAbbreviation') or raw.get('journalTitle')
        if title or raw.get('pageInfo'):
            return {'journal': clean(title), 'volume': clean(info.get('volume')), 'issue': clean(info.get('issue')),
                    'pages': clean(raw.get('pageInfo'))}
    return {'journal': '', 'volume': '', 'issue': '', 'pages': ''}


def format_citation(c):
    authors = c['authors'].rstrip('.')
    parts = [f"{authors or 'Anonymous'} ({c['year'] or 'n.d.'}).", (c['title'].rstrip('.') + '.') if c['title'] else '']
    if c['journal']:
        source = c['journal']
        if c['volume']:
            source += ', ' + c['volume'] + (f"({c['issue']})" if c['issue'] else '')
        if c['pages']:
            source += ', ' + c['pages']
        parts.append(source + '.')
    if c['doi']:
        parts.append('https://doi.org/' + c['doi'])
    return ' '.join(p for p in parts if p)


def build(c):
    """All manuscript tables from an open connection: {file name: rows}, plus the PRISMA notes."""
    rows = lambda sql: [dict(r) for r in c.execute(sql)]
    papers = rows('SELECT rowid AS seq,* FROM papers ORDER BY rowid')
    by_id = {p['id']: p for p in papers}

    def root(pid):
        seen = set()
        while by_id[pid]['merged_into'] and pid not in seen:
            seen.add(pid)
            pid = by_id[pid]['merged_into']
        return pid

    family = {}
    for p in papers:
        family.setdefault(root(p['id']), []).append(p['id'])
    canonical = [p for p in papers if not p['merged_into']]

    runs = rows('SELECT rowid AS seq,* FROM runs ORDER BY rowid')
    run_number = {r['id']: n for n, r in enumerate(runs, 1)}
    queries = {q['id']: q for q in rows('SELECT * FROM queries')}
    sources = rows('SELECT id,paper_id,raw FROM source_records ORDER BY rowid')
    discoveries = rows('SELECT run_id,source_record_id FROM discoveries')
    source_paper = {s['id']: root(s['paper_id']) for s in sources}
    raws = {}
    for s in sources:
        try:
            raw = json.loads(s['raw'])
        except ValueError:
            continue
        if isinstance(raw, dict):
            raws.setdefault(root(s['paper_id']), []).append(raw)
    identifiers = {}
    for i in rows('SELECT * FROM identifiers ORDER BY rowid'):
        identifiers.setdefault((root(i['paper_id']), i['kind']), i['value'])
    found_in = {}
    for d in discoveries:
        found_in.setdefault(source_paper[d['source_record_id']], set()).add(run_number[d['run_id']])

    criteria = rows('SELECT rowid AS seq,* FROM criteria ORDER BY rowid')
    criteria_number = {cr['id']: n for n, cr in enumerate(criteria, 1)}
    decisions = rows('SELECT rowid AS seq,* FROM decisions ORDER BY rowid')
    latest, events = {}, {}
    for d in decisions:
        pid = root(d['paper_id'])
        latest[(pid, d['stage'])] = d
        events[pid] = events.get(pid, 0) + 1

    resources = {}
    for r in rows(f"SELECT * FROM resources WHERE kind IN ({','.join(repr(k) for k in DATA_KINDS)}) AND status<>'rejected' ORDER BY rowid"):
        resources.setdefault(root(r['paper_id']), []).append(r)
    scans = {}
    for s in rows("SELECT * FROM eyelink_scans WHERE status='completed' ORDER BY rowid"):
        scans[root(s['paper_id'])] = s
    checks = {}
    for ch in rows('SELECT * FROM repository_checks ORDER BY rowid'):
        checks.setdefault(ch['scan_id'], []).append(ch)
    confirmed = {r['scan_id']: r['n'] for r in rows("SELECT k.scan_id,COUNT(*) n FROM repository_files f JOIN repository_checks k ON k.id=f.check_id WHERE f.header='eyelink' GROUP BY k.scan_id")}
    assessments = {}
    for a in rows('SELECT * FROM assessments ORDER BY rowid'):
        assessments[root(a['paper_id'])] = a
    licenses = {}
    for f in rows('SELECT paper_id,license FROM pdfs WHERE license IS NOT NULL ORDER BY rowid'):
        licenses.setdefault(root(f['paper_id']), f['license'])

    def status(pid):
        ta, ft = latest.get((pid, 'title_abstract')), latest.get((pid, 'fulltext'))
        if ft and ft['decision'] in ('include', 'exclude'):
            return 'included' if ft['decision'] == 'include' else 'excluded_full_text'
        if ft and ft['decision'] == 'inaccessible':
            return 'not_retrieved'
        if ft or (ta and ta['decision'] == 'include'):
            return 'awaiting_full_text'
        if ta and ta['decision'] == 'exclude':
            return 'excluded_title_abstract'
        return 'title_abstract_unresolved' if ta else 'not_screened'

    def citation(p):
        pid = p['id']
        c = {'authors': clean(p['authors']), 'year': clean(p['year']), 'title': clean(p['title']),
             **journal_info(raws.get(pid, [])), 'doi': p['doi'] or identifiers.get((pid, 'doi'), ''),
             'pmid': identifiers.get((pid, 'pmid'), ''), 'pmcid': identifiers.get((pid, 'pmcid'), ''),
             'publication_type': clean(p['publication_type'])}
        c['citation'] = format_citation(c)
        return c

    # Reference-list order: first author, then year, then title; Orchard ID breaks ties.
    canonical.sort(key=lambda p: (clean(p['authors']).lower() or '￿', clean(p['year']), clean(p['title']).lower(), p['id']))
    cites = {p['id']: citation(p) for p in canonical}
    statuses = {p['id']: status(p['id']) for p in canonical}
    record_number = {p['id']: n for n, p in enumerate(canonical, 1)}

    out = {}
    out['table_s1_search_strategy.csv'] = [{
        'search_number': run_number[r['id']], 'database': PROVIDERS.get(r['provider'], r['provider']),
        'search_label': queries[r['query_id']]['label'] if r['query_id'] in queries else '',
        'exact_query': r['query'], 'date_started': r['created'], 'date_finished': r['finished'],
        'status': r['status'], 'database_hit_count': r['total'], 'records_retrieved': r['retrieved'],
        'retrieval_cap': json.loads(r['parameters']).get('max_records_per_attempt', ''),
        'repeat_of_search': run_number.get(r['refresh_of'], ''), 'notes': r['error'],
        'query_version_id': r['query_id'], 'search_id': r['id']} for r in runs]

    runs_by_query = {}
    for r in runs:
        runs_by_query.setdefault(r['query_id'], []).append(str(run_number[r['id']]))
    terms = {t['id']: t for t in rows('SELECT t.*,c.label concept,d.name document FROM terms t JOIN concepts c ON c.id=t.concept_id JOIN documents d ON d.id=c.document_id')}
    out['table_s2_search_terms.csv'] = [{
        'search_label': q['label'], 'concept_group': terms[tid]['concept'], 'term': terms[tid]['original'],
        'normalized_term': terms[tid]['normalized'], 'source_document': terms[tid]['document'],
        'source_line': terms[tid]['line'], 'searches_run': '; '.join(runs_by_query.get(q['id'], [])),
        'query_version_id': q['id']}
        for q in rows('SELECT * FROM queries WHERE approved=1 ORDER BY rowid')
        for tid in json.loads(q['term_ids']) if tid in terms]

    used = {}
    for d in decisions:
        used[d['criteria_id']] = used.get(d['criteria_id'], 0) + 1
    out['table_s3_eligibility_criteria.csv'] = [{
        'criteria_version': criteria_number[cr['id']], 'date_created': cr['created'],
        'current': 'yes' if n == len(criteria) else 'no', 'inclusion_criteria': cr['inclusion'],
        'exclusion_criteria': cr['exclusion'], 'screening_decisions_recorded': used.get(cr['id'], 0),
        'criteria_id': cr['id']} for n, cr in enumerate(criteria, 1)]

    out['table_s4_prisma_flow.csv'] = prisma(runs, discoveries, papers, canonical, statuses, latest)

    def screening(prefix, d):
        return {prefix + '_decision': d['decision'] if d else '', prefix + '_reason': d['reason'] if d else '',
                prefix + '_reviewer': d['reviewer'] if d else '', prefix + '_date': d['created'] if d else ''}

    def ft_details(pid):
        d = latest[(pid, 'fulltext')]
        return {'full_text_reviewer': d['reviewer'], 'full_text_decision_date': d['created'], 'full_text_notes': d['notes'],
                'exclusion_reason': d['reason'], 'criteria_version': criteria_number.get(d['criteria_id'], '')}

    def data_summary(pid):
        links = resources.get(pid, [])
        scan = scans.get(pid)
        assessment = json.loads(assessments[pid]['fields']) if pid in assessments else {}
        return {'data_links': '; '.join(dict.fromkeys(r['url'] for r in links if r['url'])),
                'verified_data_links': '; '.join(dict.fromkeys(r['url'] for r in links if r['url'] and r['status'] == 'researcher_verified')),
                'data_on_request': 'yes' if any(r['status'] == 'upon_request' for r in links) else 'no',
                'eyelink_verdict': scan['result'] if scan else 'not_scanned', 'eyelink_files': scan['eyelink_files'] if scan else '',
                'reuse_feasibility': clean(assessment.get('reuse_feasibility')), 'pdf_license': licenses.get(pid, '')}

    included = [p for p in canonical if statuses[p['id']] == 'included']
    out['table_s5_included_studies.csv'] = [{
        'report_number': n, **cites[p['id']], **ft_details(p['id']), **data_summary(p['id']), 'orchard_id': p['id']}
        for n, p in enumerate(included, 1)]
    excluded = [p for p in canonical if statuses[p['id']] == 'excluded_full_text']
    out['table_s6_excluded_full_text.csv'] = [{
        'report_number': n, **cites[p['id']], **ft_details(p['id']), 'orchard_id': p['id']}
        for n, p in enumerate(excluded, 1)]

    out['table_s7_screening_log.csv'] = [{
        'record_number': record_number[p['id']], **cites[p['id']],
        'found_in_searches': '; '.join(str(n) for n in sorted(found_in.get(p['id'], ()))),
        'merged_duplicates': len(family[p['id']]) - 1,
        **screening('title_abstract', latest.get((p['id'], 'title_abstract'))),
        **screening('full_text', latest.get((p['id'], 'fulltext'))),
        'final_status': statuses[p['id']], 'decision_events': events.get(p['id'], 0), 'orchard_id': p['id']}
        for p in canonical]

    def brief(p):
        c = cites[p['id']]
        return {'record_number': record_number[p['id']], 'authors': c['authors'], 'year': c['year'], 'title': c['title'],
                'doi': c['doi'], 'final_status': statuses[p['id']], 'orchard_id': p['id']}

    out['table_s8_data_availability.csv'] = [{
        **brief(p), 'resource_type': r['kind'], 'url': r['url'], 'repository': r['repository'],
        'verification_status': r['status'], 'verification_note': clean(r['access_note']), 'verified_date': r['verified'],
        'found_in': r['origin'], 'evidence': clean(r['evidence'])}
        for p in canonical for r in resources.get(p['id'], [])]

    eyelink = []
    for p in canonical:
        scan, assessment = scans.get(p['id']), assessments.get(p['id'])
        if not scan and not assessment:
            continue
        fields = json.loads(assessment['fields']) if assessment else {}
        eyelink.append({
            **brief(p), 'eyelink_scan_date': scan['finished'] if scan else '', 'eyelink_verdict': scan['result'] if scan else 'not_scanned',
            'repositories_checked': scan['repositories'] if scan else '', 'eyelink_files': scan['eyelink_files'] if scan else '',
            'eyelink_header_confirmed': confirmed.get(scan['id'], 0) if scan else '',
            'repository_urls': '; '.join(f"{ch['url']} ({ch['status']})" for ch in checks.get(scan['id'], [])) if scan else '',
            **{f: clean(fields.get(f)) for f in FEASIBILITY}, 'assessment_date': assessment['created'] if assessment else ''})
    out['table_s9_eyelink_and_reuse.csv'] = eyelink
    return out


def prisma(runs, discoveries, papers, canonical, statuses, latest):
    count = lambda *names: sum(statuses[p['id']] in names for p in canonical)
    run_provider = {r['id']: r['provider'] for r in runs}
    rows = []
    for provider in dict.fromkeys(r['provider'] for r in runs):
        rows.append(('Identification', 'Records identified: ' + PROVIDERS.get(provider, provider),
                     sum(run_provider[d['run_id']] == provider for d in discoveries),
                     'Records retrieved across all searches of this source; a record found by several searches is counted each time.'))
    incomplete = sum(r['status'] != 'completed' for r in runs)
    rows += [
        ('Identification', 'Records identified: total', len(discoveries), 'Sum of the records identified above.'),
        ('Identification', 'Searches with incomplete retrieval', incomplete,
         'Searches that did not retrieve every reported hit (partial, failed or interrupted). See the search strategy table.'),
        ('Identification', 'Duplicate records removed: identical identifiers', len(discoveries) - len(papers),
         'Records consolidated automatically because the same provider record, DOI, PMID or PMCID was already stored.'),
        ('Identification', 'Duplicate records removed: researcher-confirmed', len(papers) - len(canonical),
         'Similar records the researcher reviewed and merged.'),
        ('Screening', 'Records screened', len(canonical) - count('not_screened'),
         'Unique records with at least one screening decision.'),
        ('Screening', 'Records excluded at title/abstract', count('excluded_title_abstract'),
         'Latest title/abstract decision is exclude, with no later full-text decision.'),
        ('Retrieval', 'Reports sought for retrieval', count('included', 'excluded_full_text', 'not_retrieved', 'awaiting_full_text'),
         'Records that passed title/abstract screening or have a full-text decision.'),
        ('Retrieval', 'Reports not retrieved', count('not_retrieved'), 'Latest full-text decision is inaccessible.'),
        ('Eligibility', 'Reports assessed for eligibility', count('included', 'excluded_full_text'),
         'Reports with a final full-text include or exclude decision.'),
        ('Eligibility', 'Reports excluded at full text: total', count('excluded_full_text'), 'Itemized by reason below.'),
    ]
    reasons = {}
    for p in canonical:
        if statuses[p['id']] == 'excluded_full_text':
            reason = clean(latest[(p['id'], 'fulltext')]['reason']) or 'No reason recorded'
            reasons[reason] = reasons.get(reason, 0) + 1
    rows += [('Eligibility', 'Reports excluded: ' + reason, n, 'Reason as recorded by the reviewer.')
             for reason, n in sorted(reasons.items(), key=lambda x: (-x[1], x[0].lower()))]
    rows += [
        ('Included', 'Reports included', count('included'), 'Latest full-text decision is include.'),
        ('Included', 'Studies included', None,
         'Not determined: Orchard tracks reports and does not map reports to studies. Count studies manually if several reports describe one study.'),
        ('Pending', 'Records not yet screened', count('not_screened'), 'Unique records with no screening decision.'),
        ('Pending', 'Title/abstract decision unresolved', count('title_abstract_unresolved'), 'Latest decision is uncertain or needs information.'),
        ('Pending', 'Full-text decision pending', count('awaiting_full_text'),
         'Passed title/abstract screening, with no full-text decision or an uncertain/awaiting-full-text one.'),
    ]
    return [{'stage': s, 'item': i, 'n': n, 'notes': note} for s, i, n, note in rows]


def readme(project, exported_at):
    lines = ['ORCHARD MANUSCRIPT SUPPLEMENTARY TABLES', '',
             'Project: ' + (project['name'] or ''), 'Research question: ' + (project['question'] or 'not recorded'),
             'Exported: ' + exported_at, '',
             'Each CSV is UTF-8 with a byte-order mark, so spreadsheet programs show accented names correctly.',
             'Cells that begin with = + - or @ are prefixed with an apostrophe so they are not run as formulas.',
             'data_dictionary.csv describes every column. orchard_id links each row to the full Orchard export.',
             'Counts are reports (articles), not studies. Unknown values are left blank, not estimated.', '', 'Files:']
    lines += [f'  {name}  {title}: {description}' for name, title, description, _ in TABLES]
    lines += ['  data_dictionary.csv  Column descriptions for every table.', '']
    return '\n'.join(lines)


def write_zip(c, exported_at):
    tables = build(c)
    project = dict(c.execute('SELECT * FROM project').fetchone())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README.txt', readme(project, exported_at))
        for name, _, _, columns in TABLES:
            z.writestr(name, to_csv([col for col, _ in columns], tables[name]))
        dictionary = [{'file': name, 'table': title, 'column': col, 'description': desc}
                      for name, title, _, columns in TABLES for col, desc in columns]
        z.writestr('data_dictionary.csv', to_csv(['file', 'table', 'column', 'description'], dictionary))
    return buffer.getvalue()
