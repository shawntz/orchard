"""Repository projects: files, documentation, publications, article text, screening, downloads and reporting."""
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path

from fakeweb import Reply
from repository_fixtures import ARTICLE_DOI, ASC, CLOUDFLARE, NBACK_DOI, OSF_SHELL, TITLE_CTD34, build_web
from test_repository_discovery import Base, make_service
from orchard import articles, discovery as D, inventory as inv, publications as P, screening
from orchard.db import SCHEMA
from orchard.server import make_server


class Inspected(Base):
    """Runs one full search with inspection; individual tests look at the stored results."""

    def setUp(self):
        super().setUp()
        self.job = self.run_search()
        self.ctd = self.detail('osf', 'ctd34')

    def detail(self, repository, external_id):
        return D.project_detail(self.s, self.project(repository, external_id)['id'])

    def criteria(self):
        self.s.save_criteria('Cognitive or psychological task with pupil recordings', 'Non-human; no pupil data')


class Files(Inspected):
    def test_regression_project_inventory_and_exclusion_folder(self):
        d = self.ctd
        self.assertEqual(d['project']['url'], 'https://osf.io/ctd34/')
        self.assertEqual(d['project']['match_status'], 'eyelink_confirmed')
        paths = {f['path']: f for f in d['files']}
        self.assertIn('Experiment 1/Pupil_Data/pp01.asc', paths)  # nested folders listed
        excluded = paths['Experiment 1/Pupil_Data/Not_included/pp03.asc']
        self.assertEqual(excluded['role'], 'recording_excluded_group')
        self.assertEqual(excluded['exclusion_group'], 'Experiment 1/Pupil_Data/Not_included')
        self.assertEqual(paths['Experiment 1/Behavioural_Data/Not_Included/03-results.mat']['exclusion_group'], 'Experiment 1/Behavioural_Data/Not_Included')
        self.assertEqual(paths['Experiment 1/Pupil_Data/pp01.asc']['role'], 'recording')
        self.assertEqual(paths['Experiment 1/Behavioural_Data/01-results.mat']['role'], 'behavioral')
        self.assertEqual(paths['Experiment 1/AnalysisExp1OSF.py']['role'], 'code')
        self.assertEqual(paths['Experiment 2/Behavioural_Data_21']['role'], 'placeholder')
        self.assertEqual(paths['README.md']['role'], 'documentation')
        self.assertTrue(all(f['size'] is not None and f['download'] == 'public' and f['url'] for f in d['files']))
        probed = [f for f in d['files'] if f['header'] == 'eyelink']
        self.assertTrue(probed and probed[0]['header_details']['pupil'] == 'AREA' and probed[0]['header_details']['sample_rate_hz'] == 1000.0)
        self.assertTrue(any(f['exclusion_group'] for f in probed), 'files from the exclusion folder are probed separately')

    def test_eeg_edf_and_gis_asc_are_not_eyelink(self):
        nb = {f['path']: f for f in self.detail('osf', 'nb4ck')['files']}
        self.assertEqual(nb['data/sub-01.edf']['header'], 'eyelink')
        self.assertEqual(nb['eeg/sub-01_eeg.edf']['header'], 'european_data_format')
        self.assertEqual(nb['eeg/sub-01_eeg.edf']['role'], 'not_eyelink')
        gis = self.detail('zenodo', '7654321')
        self.assertEqual(gis['files'][0]['header'], 'unrecognized')
        self.assertEqual(gis['project']['match_status'], 'no_study_recordings')

    def test_archive_members_listed_with_ranges_and_examples_separated(self):
        d = self.detail('zenodo', '1234567')
        paths = {f['path']: f for f in d['files']}
        self.assertEqual(paths['data.zip']['role'], 'archive')
        self.assertEqual(paths['data.zip!/data/sub01.asc']['in_archive'], 'data.zip')
        self.assertEqual(paths['data.zip!/data/sub01.asc']['download'], 'inside_archive')
        self.assertEqual(paths['data.zip!/practice/example_trial.asc']['role'], 'example_or_fixture')
        self.assertIn('eyelink', {paths['data.zip!/data/sub01.asc']['header'], paths['data.zip!/data/sub02.asc']['header']})
        self.assertTrue(any(r and r.startswith('bytes=-') for u, r in self.web.calls if 'data.zip' in u), 'central directory read by range')
        self.assertFalse(any(u.endswith('data.zip/content') and not r for u, r in self.web.calls), 'archive never downloaded whole')

    def test_test_fixtures_and_lfs_pointers(self):
        d = self.detail('github', 'lab/pupil-tools')
        paths = {f['path']: f for f in d['files']}
        self.assertEqual(paths['tests/data/sample.asc']['role'], 'example_or_fixture')
        self.assertIn('test folder of a package', paths['tests/data/sample.asc']['role_reason'])
        lfs = paths['data/raw/p01.asc']
        self.assertEqual((lfs['header'], lfs['role'], lfs['download']), ('git_lfs_pointer', 'placeholder', 'lfs_object'))
        self.assertTrue(lfs['url'].startswith('https://media.githubusercontent.com/media/lab/pupil-tools/main/'))
        self.assertEqual(lfs['checksum'], 'sha256:' + 'a' * 64)
        self.assertEqual(d['project']['match_status'], 'no_study_recordings')

    def test_dryad_files_listed_but_marked_as_needing_sign_in(self):
        d = self.detail('dryad', 'doi:10.5061/dryad.abc123')
        self.assertEqual(d['files'][0]['download'], 'auth_required')
        self.assertIn('requires an API token', ' '.join(s['detail'] for s in d['project']['inspection_steps']))
        with self.assertRaisesRegex(ValueError, 'sign-in or an API token'):
            D.download(self.s, d['project']['id'], [d['files'][0]['id']], background=False)

    def test_documentation_read_and_empty_wiki_not_counted(self):
        docs = {x['kind']: x for x in self.ctd['documents']}
        self.assertEqual(docs['readme']['status'], 'readable')
        self.assertEqual(docs['wiki']['status'], 'empty')
        self.assertGreater(docs['readme']['words'], 100)

    def test_checklist_never_claims_compatibility(self):
        items = {c['key']: c for c in self.ctd['checklist']}
        self.assertEqual(items['pupil_channel']['status'], 'observed')
        self.assertIn('AREA', items['pupil_channel']['detail'])
        self.assertEqual(items['units']['status'], 'unverified')
        self.assertEqual(items['licence']['status'], 'unverified')
        self.assertNotIn('compatible', json.dumps(self.ctd['checklist']).lower().replace('compatibility', ''))
        edf_only = {c['key']: c for c in self.detail('osf', 'nb4ck')['checklist']}
        self.assertIn('edf2asc', edf_only['format']['detail'])

    def test_reinspection_keeps_one_row_per_file(self):
        before = len(self.ctd['files'])
        D.inspect(self.s, self.ctd['project']['id'])
        self.assertEqual(len(self.detail('osf', 'ctd34')['files']), before)
        self.assertEqual(len([x for x in self.detail('osf', 'ctd34')['documents'] if x['kind'] == 'readme']), 1)


class Publications(Inspected):
    def test_regression_project_has_no_invented_article(self):
        d = self.ctd
        self.assertEqual(d['candidates'], [])
        self.assertEqual(d['publication_state'], 'not_found')
        rejected = {r['title'] for r in d['publication_search']['rejected']}
        self.assertIn('Interactions Between Visual Working Memory, Attention, and Color Categories: a Pupillometry Study', rejected)
        self.assertIn('Revealing the hidden', rejected)
        sources = [x['source'] for x in d['publication_search']['searches']]
        self.assertEqual(sorted(set(sources)), ['Crossref', 'Europe PMC', 'OpenAlex'])
        self.assertEqual(d['article_status'], 'no_publication')

    def test_explicit_repository_link_and_dataset_doi(self):
        d = self.detail('zenodo', '1234567')
        cands = d['candidates']
        self.assertEqual([c['doi'] for c in cands], [ARTICLE_DOI])
        self.assertEqual(cands[0]['strength'], 'explicit')
        self.assertIn('isSupplementTo', cands[0]['origin'])
        self.assertEqual(d['publication_search']['searches'], [], 'no title search when an explicit link exists')
        self.assertEqual(d['project']['doi'], '10.5281/zenodo.1234567')  # dataset DOI stays on the project

    def test_documentation_doi_classified_before_proposing(self):
        d = self.detail('osf', 'nb4ck')
        self.assertEqual([(c['doi'], c['strength']) for c in d['candidates']], [(NBACK_DOI, 'moderate')])
        software = [n for n in d['publication_search']['non_publications'] if 'eyelinkparser' in n['identifier']]
        self.assertTrue(software and software[0]['kind'] == 'software')

    def test_title_word_overlap_alone_is_rejected(self):
        project = {'title': 'Pupil size during visual attention and working memory tasks', 'creators': ['A. Nobody']}
        strength, why = P.judge(project, {'title': 'Visual attention and working memory: a pupil size study', 'authors': ['Someone Else']})
        self.assertIsNone(strength)
        self.assertIn('no shared author', why[0])
        self.assertEqual(P.judge({'title': 'Data for: Pupil dilation tracks working memory load', 'creators': ['Jane Doe']},
                                 {'title': 'Pupil dilation tracks working memory load', 'authors': ['Jane Doe']})[0], 'strong')
        # Real case (OSF 9uyzf): a prolific lab's other papers share one surname and topic words; they must rank below the exact match.
        project = {'title': 'Concealed identity information detection with pupillometry in rapid serial visual presentation',
                   'creators': ['Ivory Yining Chen', 'Aytac Karabay', 'Sebastiaan Mathot', 'Howard Bowman', 'Elkan Akyurek']}
        self.assertEqual(P.judge(project, {'title': project['title'], 'authors': ['I. Y. Chen', 'A. Karabay', 'S. Mathot', 'H. Bowman', 'E. Akyurek']})[0], 'strong')
        self.assertEqual(P.judge(project, {'title': 'Identity and semantic negative priming in rapid serial visual presentation streams', 'authors': ['Z. Chen']})[0], 'weak')
        self.assertEqual(P.judge(project, {'title': 'Supplemental Material for X', 'authors': []})[0], None)
        self.assertTrue(P.is_supplement({'title': 'Supplemental Material for Reading Is Disrupted', 'doi': '10.1037/xhp0000680.supp'}))

    def test_confirm_links_paper_library_and_retrieves_article(self):
        self.criteria()
        pid = self.project('zenodo', '1234567')['id']
        cand = self.detail('zenodo', '1234567')['candidates'][0]
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            D.publication_action(self.s, pid, 'confirm', reviewer='', candidate_id=cand['id'])
        d = D.publication_action(self.s, pid, 'confirm', reviewer='Ashna', candidate_id=cand['id'])
        paper = d['papers'][0]
        self.assertEqual(paper['doi'], ARTICLE_DOI)
        library = self.s.paper(paper['id'])
        self.assertEqual(library['repository_projects'][0]['id'], pid)
        self.assertEqual(self.s.listing(q='oddball')['total'], 1)
        d = D.retrieve(self.s, pid, paper['id'])
        self.assertEqual(d['retrieval']['format'], 'pdf')
        self.assertTrue(d['retrieval']['text_extracted'])
        paper = d['papers'][0]
        self.assertEqual(len(paper['pdfs']), 1)
        self.assertGreater(paper['fulltexts'][0]['words'], 100)
        self.assertEqual(d['article_status'], 'readable')
        self.assertEqual(d['assessment']['reproducibility']['article_status'], 'readable')
        self.assertTrue(any(s['kind'] == 'article' for s in d['assessment']['sources']))

    def test_paper_shared_by_two_projects_counted_once(self):
        self.criteria()
        first = self.project('zenodo', '1234567')['id']
        cand = self.detail('zenodo', '1234567')['candidates'][0]
        paper = D.publication_action(self.s, first, 'confirm', reviewer='A', candidate_id=cand['id'])['papers'][0]
        second = self.project('osf', 'nb4ck')['id']
        d = D.publication_action(self.s, second, 'manual', reviewer='A', doi=ARTICLE_DOI, reason='Same experiment, second deposit')
        self.assertEqual(d['papers'][0]['id'], paper['id'])
        self.assertEqual(d['papers'][0]['other_projects'][0]['id'], first)
        for pid in (first, second):
            D.decide(self.s, pid, 'include', 'Auditory oddball pupillometry', reviewer='A')
        s = D.funnel(self.s)['summary']
        self.assertEqual((s['included_projects'], s['reports_linked_to_included_projects'], s['independent_studies']), (2, 1, None))
        study = D.study_action(self.s, first, 'new', reviewer='A', label='Smith 2024 oddball')['study']
        D.study_action(self.s, second, 'assign', reviewer='A', study_id=study['id'])
        s = D.funnel(self.s)['summary']
        self.assertEqual((s['independent_studies'], s['independent_studies_mapped_so_far']), (1, 1))

    def test_osf_preprint_doi_and_published_version(self):
        # Shape of a real OSF preprint record: own DOI in links.preprint_doi; attributes.doi is the published article.
        from repository_fixtures import osf_project
        pre = {'attributes': {'title': 'Preprint title', 'doi': '10.5555/published.1'}, 'links': {'preprint_doi': 'https://doi.org/10.31234/osf.io/abcde', 'html': 'https://osf.io/preprints/psyarxiv/abcde'}}
        osf_project(self.web, 'pre11', 'A project with a preprint', '', 'Jane Doe', {'': [('a.asc', ASC)]}, preprints=[pre])
        related = D.adapters(self.s)['osf'].metadata('pre11')['related']
        self.assertEqual([(r['relation'], r['identifier']) for r in related],
                         [('osf_preprint', 'https://doi.org/10.31234/osf.io/abcde'), ('article_doi', '10.5555/published.1')])

    def test_manual_dataset_doi_needs_explicit_override(self):
        pid = self.project('osf', 'ctd34')['id']
        self.web.exact('https://api.datacite.org/dois/10.17605/osf.io/zzzzz', {'data': {'attributes': {'types': {'resourceTypeGeneral': 'Dataset'}, 'titles': [{'title': 'x'}]}}})
        with self.assertRaisesRegex(ValueError, 'not a paper'):
            D.publication_action(self.s, pid, 'manual', reviewer='A', doi='10.5061/dryad.zzz')
        with self.assertRaisesRegex(ValueError, 'not a paper'):
            D.publication_action(self.s, pid, 'manual', reviewer='A', doi='https://doi.org/10.17605/OSF.IO/ZZZZZ')

    def test_no_publication_identified_keeps_dataset_in_review(self):
        pid = self.project('osf', 'ctd34')['id']
        with self.assertRaisesRegex(ValueError, 'what was checked'):
            D.publication_action(self.s, pid, 'none_identified', reviewer='A', reason='')
        d = D.publication_action(self.s, pid, 'none_identified', reviewer='Ashna', reason='Checked README, OSF wiki, Crossref, Google Scholar')
        self.assertEqual(d['publication_state'], 'none_identified')
        self.assertEqual(d['assessment']['status'], 'likely_relevant')
        self.assertEqual(D.projects(self.s, decision='undecided', match='with_files')['total'], D.funnel(self.s)['summary']['projects_with_matching_files'])

    def test_reject_and_unlink_keep_history(self):
        pid = self.project('osf', 'nb4ck')['id']
        cand = self.detail('osf', 'nb4ck')['candidates'][0]
        with self.assertRaises(ValueError):
            D.publication_action(self.s, pid, 'reject', reviewer='A', candidate_id=cand['id'])
        D.publication_action(self.s, pid, 'confirm', reviewer='A', candidate_id=cand['id'])
        paper = self.detail('osf', 'nb4ck')['papers'][0]
        d = D.publication_action(self.s, pid, 'unlink', reviewer='A', candidate_id=paper['id'], reason='Wrong experiment')
        self.assertEqual(d['papers'][0]['link']['status'], 'removed')
        self.assertEqual([h['action'] for h in d['publication_history']], ['unlink', 'confirm'])
        with self.s.db.connect() as c, self.assertRaises(sqlite3.DatabaseError):
            c.execute('DELETE FROM publication_decisions')


class ArticleChecks(unittest.TestCase):
    def test_osf_shell_cloudflare_login_and_empty_rejected(self):
        self.assertEqual(articles.assess_html(OSF_SHELL)['outcome'], 'js_shell')
        self.assertEqual(articles.assess_html(CLOUDFLARE)['outcome'], 'bot_check')
        self.assertEqual(articles.assess_html(b'<html><title>Sign in</title><form><input type="password"></form></html>')['outcome'], 'login_page')
        self.assertEqual(articles.classify_response(b'', {}, 'x')['outcome'], 'empty')
        self.assertEqual(articles.assess_html(b'<html><body><h1>OSF</h1><script src=app.js></script></body></html>')['outcome'], 'js_shell')
        abstract = b'<html><head><meta name="citation_abstract" content="x"></head><body><h2>Abstract</h2><p>' + b'word ' * 250 + b'</p></body></html>'
        self.assertEqual(articles.assess_html(abstract)['outcome'], 'abstract_only')

    def test_readable_article_html_accepted(self):
        body = '<html><body><article><h2>Abstract</h2><p>' + 'pupil ' * 300 + '</p><h2>Methods</h2><p>' + 'task ' * 900 + '</p><h2>Results</h2><p>' + 'data ' * 500 + '</p></article></body></html>'
        r = articles.assess_html(body.encode())
        self.assertEqual(r['outcome'], 'readable_html')
        self.assertGreater(r['words'], 1500)


class Retrieval(Inspected):
    def test_inaccessible_article_is_recorded_not_excluded(self):
        self.criteria()
        pid = self.project('osf', 'nb4ck')['id']
        cand = self.detail('osf', 'nb4ck')['candidates'][0]
        paper = D.publication_action(self.s, pid, 'confirm', reviewer='A', candidate_id=cand['id'])['papers'][0]
        d = D.retrieve(self.s, pid, paper['id'])
        self.assertIsNone(d['retrieval'])
        outcomes = [a['outcome'] for a in d['papers'][0]['attempts']]
        self.assertIn('bot_check', outcomes)  # doi.org -> Cloudflare page, recorded with its reason
        self.assertEqual(d['papers'][0]['fulltexts'], [])
        self.assertEqual(d['article_status'], 'inaccessible')
        self.assertNotEqual(d['assessment']['status'], 'possible_exclusion')
        with self.assertRaisesRegex(ValueError, 'not an exclusion'):
            D.decide(self.s, pid, 'exclude', 'Article paywalled', reviewer='A', exclusion_reason='Article inaccessible')
        d = D.decide(self.s, pid, 'awaiting_information', 'Article behind a bot check; request a copy', reviewer='A')
        self.assertEqual(d['decisions'][0]['decision'], 'awaiting_information')

    def test_uploaded_pdf_becomes_readable_text(self):
        from repository_fixtures import ARTICLE_PDF
        pid = self.project('osf', 'nb4ck')['id']
        cand = self.detail('osf', 'nb4ck')['candidates'][0]
        paper = D.publication_action(self.s, pid, 'confirm', reviewer='A', candidate_id=cand['id'])['papers'][0]
        with self.assertRaises(ValueError):
            self.s.store_pdf(paper['id'], b'<html>login</html>', 'x.pdf', 'upload')
        stored = self.s.store_pdf(paper['id'], ARTICLE_PDF, 'library.pdf', 'Uploaded by researcher: library.pdf')
        self.assertTrue(stored['text_extracted'])
        D.assess(self.s, pid)
        self.assertEqual(D.project_detail(self.s, pid)['article_status'], 'readable')


class Screening(unittest.TestCase):
    def test_variants_and_passage_evidence(self):
        for text in ('Pupilsize was recorded during a working memory task.', 'pupil_size during the n-back task', 'Pupil-diameter changed with memory load in a working memory task.'):
            self.assertEqual(screening.assess([{'id': 'x', 'kind': 'readme', 'text': text}])['status'], 'likely_relevant', text)

    def test_isolated_words_are_not_enough(self):
        text = 'This repository contains pupil data. ' + 'Files are organised by date and the code is in Python. ' * 8 + 'Attention: please cite the dataset.'
        r = screening.assess([{'id': 'x', 'kind': 'readme', 'text': text}])
        self.assertEqual(r['status'], 'needs_review')

    def test_exclusion_concepts_flag_but_do_not_exclude(self):
        r = screening.assess([{'id': 'x', 'kind': 'readme', 'text': 'Pupillary light reflex recorded in anesthetized mice. Pupil size followed light intensity over trials.'}])
        self.assertEqual(r['status'], 'possible_exclusion')
        self.assertTrue(any(e['kind'] == 'exclusion' for e in r['evidence']))
        mixed = screening.assess([{'id': 'x', 'kind': 'readme', 'text': 'Pupil dilation during a working memory task in humans. Control recordings of the pupillary light reflex.'}])
        self.assertEqual(mixed['status'], 'needs_review')

    def test_no_text_is_insufficient_information(self):
        r = screening.assess([{'id': 'x', 'kind': 'metadata', 'text': 'pp01'}])
        self.assertEqual(r['status'], 'insufficient_information')
        self.assertIsNone(r['reproducibility']['semantic_model'])

    def test_concepts_editable_and_versioned(self):
        text = screening.format_concepts(screening.DEFAULT_CONCEPTS)
        self.assertEqual(screening.parse_concepts(text), screening.DEFAULT_CONCEPTS)
        with self.assertRaises(ValueError):
            screening.parse_concepts('[tasks]\nno colon here')


class Decisions(Inspected):
    def test_versioned_history_with_reviewer_criteria_and_evidence(self):
        pid = self.ctd['project']['id']
        with self.assertRaisesRegex(ValueError, 'criteria'):
            D.decide(self.s, pid, 'include', 'Attention task with pupil recordings', reviewer='Ashna')
        self.criteria()
        with self.assertRaisesRegex(ValueError, 'reason'):
            D.decide(self.s, pid, 'include', '', reviewer='Ashna')
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            D.decide(self.s, pid, 'include', 'x', reviewer=' ')
        evidence = [{'kind': 'pupil_and_task', 'passage': self.ctd['assessment']['evidence'][0]['passage']}]
        D.decide(self.s, pid, 'include', 'README describes attention experiments with pupil recordings', reviewer='Ashna', evidence=evidence, version=0)
        from orchard.service import Conflict
        with self.assertRaises(Conflict):
            D.decide(self.s, pid, 'exclude', 'stale page', reviewer='B', exclusion_reason='Other (explain in reason)', version=0)
        d = D.decide(self.s, pid, 'exclude', 'Second reviewer: luminance manipulation only', reviewer='B', exclusion_reason='Not a psychological or cognitive task', version=1)
        self.assertEqual([x['version'] for x in d['decisions']], [2, 1])
        first = d['decisions'][1]
        self.assertEqual((first['reviewer'], first['decision'], first['evidence'][0]['kind']), ('Ashna', 'include', 'pupil_and_task'))
        self.assertEqual(first['criteria']['criteria']['inclusion'], 'Cognitive or psychological task with pupil recordings')
        self.assertEqual(first['criteria']['automated_assessment']['status'], 'likely_relevant')
        with self.s.db.connect() as c, self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE project_decisions SET decision='include'")
        s = D.funnel(self.s)['summary']
        self.assertEqual(s['decisions'].get('exclude'), 1)
        self.assertEqual(s['exclusion_reasons'], {'Not a psychological or cognitive task': 1})

    def test_duplicate_suggestion_needs_review(self):
        from repository_fixtures import osf_project
        osf_project(self.web, 'dup11', TITLE_CTD34, '', 'Marleen Abbestee', {'': [('pp01.asc', ASC)]})
        with self.s.db.connect() as c:
            c.execute("INSERT INTO sweep_projects(id,repository,external_id,url,title,first_seen) VALUES('dup','osf','dup11','https://osf.io/dup11/',?,datetime('now'))", (TITLE_CTD34,))
        d = D.inspect(self.s, 'dup')
        rel = d['relations'][0]
        self.assertEqual((rel['status'], rel['basis']), ('suggested', 'identical title'))
        D.relation_action(self.s, rel['id'], 'same_dataset', reviewer='A', reason='Re-upload')
        self.assertEqual(D.funnel(self.s)['summary']['confirmed_duplicate_projects'], 1)


class Downloads(Inspected):
    def test_plan_download_verify_and_manifest(self):
        d = self.ctd
        pick = [f for f in d['files'] if f['path'] in ('Experiment 1/Pupil_Data/pp01.asc', 'Experiment 1/Behavioural_Data/01-results.mat', 'Experiment 1/AnalysisExp1OSF.py')]
        plan = D.download_plan(self.s, d['project']['id'], [f['id'] for f in pick])
        self.assertEqual(plan['total_bytes'], sum(f['size'] for f in pick))
        self.assertTrue(plan['destination'].endswith('downloads/osf_ctd34'))
        # A checksum that does not match fails verification instead of passing silently.
        with self.s.db.connect() as c:
            c.execute('UPDATE sweep_files SET checksum=? WHERE id=?', ('sha256:' + 'b' * 64, pick[0]['id']))
        batch = D.download(self.s, d['project']['id'], [pick[0]['id']], reviewer='A', background=False)
        self.assertEqual(batch['items'][0]['status'], 'failed')
        self.assertIn('checksum mismatch', batch['items'][0]['detail'])
        with self.s.db.connect() as c:
            c.execute('UPDATE sweep_files SET checksum=? WHERE id=?', ('sha256:' + hashlib.sha256(self.web.match(pick[0]['url'], {}).body).hexdigest(), pick[0]['id']))
        batch = D.download(self.s, d['project']['id'], [f['id'] for f in pick], reviewer='A', background=False)
        self.assertEqual({i['status'] for i in batch['items']}, {'verified'})
        local = Path(batch['items'][0]['dest'])
        self.assertTrue(local.exists() and str(local).startswith(plan['destination']))
        manifest = json.loads(Path(batch['manifest_path']).read_text())
        self.assertEqual(len(manifest['files']), 3)
        self.assertEqual(manifest['project']['url'], 'https://osf.io/ctd34/')
        self.assertIn('does not establish licence', manifest['note'])

    def test_interrupted_download_resumes_with_range(self):
        f = next(x for x in self.ctd['files'] if x['path'] == 'Experiment 1/Pupil_Data/pp02.asc')
        with self.s.db.connect() as c:
            c.execute('UPDATE sweep_files SET checksum=? WHERE id=?', ('sha256:' + hashlib.sha256(ASC).hexdigest(), f['id']))
        dest = D.download_plan(self.s, self.ctd['project']['id'], [f['id']])['files'][0]['dest']
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest + '.part').write_bytes(ASC[:5000])
        batch = D.download(self.s, self.ctd['project']['id'], [f['id']], background=False)
        self.assertEqual(batch['items'][0]['status'], 'verified')
        self.assertIn('Resumed from byte 5,000', batch['items'][0]['detail'])
        self.assertIn(('https://osf.io/download/ctd34/Experiment 1/Pupil_Data/pp02.asc', 'bytes=5000-'), self.web.calls)

    def test_size_check_and_large_confirmation(self):
        f = next(x for x in self.ctd['files'] if x['path'] == 'Experiment 1/Pupil_Data/pp04.asc')
        with self.s.db.connect() as c:
            c.execute('UPDATE sweep_files SET size=?,checksum=NULL WHERE id=?', (6_000_000_000, f['id']))
        with self.assertRaisesRegex(ValueError, 'Confirm the large download'):
            D.download(self.s, self.ctd['project']['id'], [f['id']], background=False)
        batch = D.download(self.s, self.ctd['project']['id'], [f['id']], confirm_large=True, background=False)
        self.assertEqual(batch['items'][0]['status'], 'failed')
        self.assertIn('differs from the listed', batch['items'][0]['detail'])

    def test_destination_cannot_escape_download_folder(self):
        from orchard.downloads import target_path
        root = Path(self.tmp.name) / 'downloads'
        self.assertTrue(str(target_path(root, 'osf', 'x', '../../etc/passwd')).startswith(str(root.resolve())))


class Reporting(Inspected):
    def test_funnel_counts_are_computed_and_unknowns_listed(self):
        f = D.funnel(self.s)
        s = f['summary']
        projects = self.s.db.rows('SELECT * FROM sweep_projects')
        self.assertEqual(s['unique_projects_found'], len(projects))
        self.assertEqual(s['records_examined'], self.s.db.one('SELECT COUNT(*) n FROM discovery_observations')['n'])
        self.assertEqual(s['projects_with_matching_files'], sum(p['match_status'] in D.MATCHING for p in projects))
        self.assertEqual(s['projects_header_confirmed'], sum(p['match_status'] == 'eyelink_confirmed' for p in projects))
        self.assertIsInstance(s['independent_studies'], int)  # nothing included yet, so nothing is unmapped
        self.assertTrue(any('skipped' in u for u in f['unknowns']))
        self.assertIn('does not remove selection', f['bias_note'])
        text = D.prisma_text(f)
        self.assertIn('Records examined across repository searches', text)
        self.assertIn('GitHub code search requires a token', text)

    def test_export_zip_contains_repository_records(self):
        self.criteria()
        D.decide(self.s, self.ctd['project']['id'], 'include', 'Attention pupillometry', reviewer='Ashna')
        archive = zipfile.ZipFile(io.BytesIO(self.s.export_zip()))
        names = set(archive.namelist())
        for name in ('repository-prisma.txt', 'repository-prisma.json', 'project_decisions.csv', 'sweep_files.csv', 'discovery_tasks.csv',
                     'publication_searches.csv', 'relevance_assessments.csv', 'retrieval_attempts.csv', 'downloads.csv', 'studies.csv'):
            self.assertIn(name, names)
        self.assertIn('Ashna', archive.read('project_decisions.csv').decode())
        self.assertIn('Not_included', archive.read('sweep_files.csv').decode())

    def test_http_routes_and_reader(self):
        server = make_server(self.s, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(root + '/api/discovery') as r:
                self.assertEqual(json.load(r)['jobs'][0]['id'], self.job['id'])
            with urllib.request.urlopen(root + '/api/discovery/project/' + self.ctd['project']['id']) as r:
                self.assertEqual(json.load(r)['project']['external_id'], 'ctd34')
            readme = next(x for x in self.ctd['documents'] if x['kind'] == 'readme')
            with urllib.request.urlopen(root + '/api/discovery/documents/' + readme['id']) as r:
                self.assertIn('attentional field', json.load(r)['content'])
            with urllib.request.urlopen(root + '/api/export?format=prisma') as r:
                self.assertIn(b'Unique repository projects found', r.read())
            req = urllib.request.Request(root + '/api/discovery/decision', data=b'{}', headers={'Content-Type': 'application/json', 'Origin': 'http://evil.example'})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 403)
        finally:
            server.shutdown(); server.server_close(); thread.join()


class Migration(unittest.TestCase):
    """A database created by the earlier prototype keeps its records and gains the new structure."""

    def test_prototype_database_migrates_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'orchard.sqlite3'
            with sqlite3.connect(path) as c:
                c.executescript(SCHEMA)
                c.executescript(D.LEGACY)
                c.execute("INSERT INTO sweep_projects VALUES('p','osf','ctd34','https://osf.io/ctd34/',?,'',NULL,NULL)", (TITLE_CTD34,))
                c.execute("INSERT INTO sweep_files VALUES('f','p','Experiment 1/Pupil_Data/pp01.asc','https://osf.io/download/x',100,'.asc')")
                c.execute("INSERT INTO discovery_documents VALUES('d','p','article','https://osf.io/ctd34/','','OSF',NULL,'x','2026-10-01')")
                c.execute("INSERT INTO discovery_candidates VALUES('c','p','10.31234/osf.io/n7uvj','Interactions Between Visual Working Memory',0.74,'Close title match','{}','2026-10-01')")
                c.execute("INSERT INTO discovery_events VALUES('e1','p','human','exclude','No paper found','[]','[\"pupil\"]','2026-10-02')")
                c.execute("INSERT INTO discovery_events VALUES('e2','p','human','review','Re-check','[]','[]','2026-10-03')")
                c.execute("INSERT INTO discovery_jobs VALUES('j','completed','{}','2026-10-01','2026-10-01',NULL,NULL)")
            s, _ = make_service(tmp)
            try:
                backups = list(Path(tmp).glob('orchard.before-v4-*.sqlite3'))
                self.assertEqual(len(backups), 1)
                with sqlite3.connect(backups[0]) as c:
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM discovery_events').fetchone()[0], 2)
                d = D.project_detail(s, 'p')
                self.assertEqual([(x['version'], x['decision'], x['reviewer']) for x in d['decisions']], [(2, 'needs_review', D.UNKNOWN_REVIEWER), (1, 'exclude', D.UNKNOWN_REVIEWER)])
                doc = d['documents'][0]
                self.assertEqual(doc['status'], 'not_article_text')
                self.assertIn('not the article', doc['quality_reason'])
                self.assertEqual(d['candidates'][0]['strength'], 'legacy')
                self.assertEqual(d['publication_state'], 'not_searched')
                self.assertEqual(d['files'][0]['path'], 'Experiment 1/Pupil_Data/pp01.asc')
                self.assertEqual(s.db.one('SELECT COUNT(*) n FROM discovery_events')['n'], 2)
            finally:
                s.close()
            s, _ = make_service(tmp)  # second start: no new backup, no duplicate migration
            try:
                self.assertEqual(len(list(Path(tmp).glob('orchard.before-v4-*.sqlite3'))), 1)
                self.assertEqual(s.db.one('SELECT COUNT(*) n FROM project_decisions')['n'], 2)
            finally:
                s.close()


class Units(unittest.TestCase):
    def test_roles_and_participants(self):
        self.assertEqual(inv.classify('sub-01/ses-1/eyetrack/sub-01.edf', 10**6)[0], 'recording')
        self.assertEqual(inv.classify('excluded_participants/s07.asc', 10**6)[0], 'recording_excluded_group')
        self.assertEqual(inv.classify('derivatives/group_mean_pupil.csv', 1000)[0], 'processed')
        self.assertEqual(inv.classify('docs/examples/demo.asc', 10**6)[0], 'example_or_fixture')
        self.assertEqual(inv.classify('raw/s1.asc', 300)[0], 'placeholder')
        self.assertEqual(inv.participant_id('Pupil_Data/pp07.asc'), '7')
        self.assertEqual(inv.participant_id('Behavioural_Data/07-results.mat'), '7')

    def test_header_details_from_asc(self):
        details = inv.asc_details(ASC.decode())
        self.assertEqual((details['pupil'], details['sample_rate_hz'], details['eyes'], details['trial_marker']), ('AREA', 1000.0, ['LEFT', 'RIGHT'], 'TRIALID'))


if __name__ == '__main__':
    unittest.main()
