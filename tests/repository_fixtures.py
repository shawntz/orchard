"""A small, realistic repository world for offline tests and the browser test.

Response shapes follow the documented APIs (OSF v2 + SHARE trove, Zenodo records, Dryad v2,
GitHub REST, Crossref, OpenAlex, Europe PMC, DataCite). Titles and the contributor name of the
OSF project match the regression example in the task; README text and recordings are synthetic.
"""
import io
import json
import re
import zipfile
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fakeweb import FakeWeb, Reply

FIX = Path(__file__).parent / 'fixtures'
ASC = (FIX / 'eyelink_binocular_head.asc').read_bytes()
README_CTD34 = (FIX / 'attention_pupillometry_README.md').read_bytes()
OSF_SHELL = (FIX / 'osf_project_shell.html').read_bytes()
CLOUDFLARE = (FIX / 'cloudflare_challenge.html').read_bytes()
EDF_EYELINK = b'SR_RESEARCH_1000FILE\n** DATE: Tue Mar  5 2024\n** TYPE: EDF_FILE BINARY EVENT SAMPLE TAGGED\n** VERSION: EYELINK II 1\n** SOURCE: EYELINK CL\n' + bytes(range(256)) * 50
EDF_EEG = b'0       ' + b'Patient X                                                                        ' + b' ' * 168 + bytes(4000)
GIS_ASC = b'ncols 100\nnrows 100\nxllcorner 0\nyllcorner 0\ncellsize 30\n' + b'1 2 3\n' * 500
LFS_POINTER = b'version https://git-lfs.github.com/spec/v1\noid sha256:' + b'a' * 64 + b'\nsize 52428800\n'
OSF = 'https://api.osf.io/v2'
TITLE_CTD34 = 'Revealing the hidden spatial distribution of visual attention through pupillometry'
ARTICLE_DOI = '10.5555/oddball.2024.17'
NBACK_DOI = '10.5555/nback.2023.4'


def make_pdf(lines):
    """Minimal PDF whose text the standard-library extractor can read."""
    content = b'BT /F1 10 Tf 72 720 Td ' + b' T* '.join(b'(' + l.encode('latin-1').replace(b'(', b'\\(').replace(b')', b'\\)') + b') Tj' for l in lines) + b' ET'
    packed = zlib.compress(content)
    obj = b'1 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n' % len(packed) + packed + b'\nendstream\nendobj\n'
    return b'%PDF-1.7\n' + obj + b'%%EOF\n'


ARTICLE_LINES = ['Pupil-linked arousal tracks surprise in an auditory oddball task'] + [
    f'Methods. Participants performed an auditory oddball task while pupil size was recorded with an EyeLink 1000 at 1000 Hz. Trial {i} deviant tones evoked larger pupil dilation than standards, consistent with surprise and prediction error accounts of arousal.'
    for i in range(1, 40)] + ['Results. The task-evoked pupil response scaled with surprise. Discussion. Pupil-linked arousal indexes surprise.']
ARTICLE_PDF = make_pdf(ARTICLE_LINES)


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


ZENODO_ZIP = zip_bytes({'data/sub01.asc': ASC, 'data/sub02.asc': ASC, 'practice/example_trial.asc': ASC, 'code/analysis.py': b'print(1)\n'})


def osf_file(path, size, download, sha=None):
    name = path.rsplit('/', 1)[-1]
    return {'attributes': {'kind': 'file', 'name': name, 'materialized_path': '/' + path, 'size': size, 'extra': {'hashes': {'sha256': sha or 'b' * 64, 'md5': 'c' * 32}}},
            'links': {'download': download}}


def osf_folder(node, path):
    return {'attributes': {'kind': 'folder', 'name': path.rstrip('/').rsplit('/', 1)[-1], 'materialized_path': '/' + path},
            'relationships': {'files': {'links': {'related': {'href': f'{OSF}/nodes/{node}/files/osfstorage/{path}'}}}}, 'links': {}}


def page(items, next_url=None):
    return {'data': items, 'links': {'next': next_url, 'meta': {'total': len(items)}}}


def osf_project(web, guid, title, description, contributor, tree, wiki_text='', preprints=()):
    """Register an OSF project. ``tree`` maps folder path ('' for root) to list of (path, bytes)."""
    web.exact(f'{OSF}/guids/{guid}/?resolve=false', {'data': {'relationships': {'referent': {'data': {'type': 'nodes', 'id': guid}}}}})
    web.exact(f'{OSF}/nodes/{guid}/?embed=license', {'data': {'id': guid, 'attributes': {'title': title, 'description': description, 'tags': ['pupil'], 'date_created': '2025-03-11T12:03:01'},
                                                             'relationships': {}, 'embeds': {}}})
    web.exact(f'{OSF}/nodes/{guid}/identifiers/', {'data': [{'attributes': {'category': 'doi', 'value': f'10.17605/OSF.IO/{guid.upper()}'}}]})
    web.exact(f'{OSF}/nodes/{guid}/contributors/?embed=users&page[size]=50', {'data': [{'attributes': {'bibliographic': True}, 'embeds': {'users': {'data': {'attributes': {'full_name': contributor}}}}}]})
    web.exact(f'{OSF}/nodes/{guid}/preprints/', {'data': list(preprints)})
    web.exact(f'{OSF}/nodes/{guid}/files/', {'data': [{'attributes': {'name': 'osfstorage'}, 'relationships': {'files': {'links': {'related': {'href': f'{OSF}/nodes/{guid}/files/osfstorage/'}}}}}], 'links': {'next': None}})
    web.exact(f'{OSF}/nodes/{guid}/children/', page([]))
    web.exact(f'{OSF}/nodes/{guid}/wikis/', {'data': [{'attributes': {'name': 'Home'}, 'links': {'download': f'{OSF}/wikis/{guid}w/content/', 'iri': f'https://osf.io/{guid}w'}}]})
    web.exact(f'{OSF}/wikis/{guid}w/content/', wiki_text.encode())
    folders = {}
    for folder, files in tree.items():
        items = []
        for path, data in files:
            url = f'https://osf.io/download/{guid}/{path}'
            items.append(osf_file(path, len(data), url, __import__('hashlib').sha256(data).hexdigest()))
            web.exact(url, data)
        folders[folder] = items
    for folder in tree:
        if folder:
            parent = folder.rstrip('/').rsplit('/', 1)[0] + '/' if '/' in folder.rstrip('/') else ''
            folders.setdefault(parent, []).insert(0, osf_folder(guid, folder))
    for folder, items in folders.items():
        web.exact(f'{OSF}/nodes/{guid}/files/osfstorage/{folder}', page(items))


def trove_card(md):
    return {'type': 'index-card', 'attributes': {'resourceMetadata': md}}


def trove_page(cards, total, next_url=None):
    return {'data': {'attributes': {'totalResultCount': total}, 'relationships': {'searchResultPage': {'data': [], 'links': {'next': {'href': next_url} if next_url else {}}}}},
            'included': cards}


def file_card(guid, project, title, path):
    return trove_card({'@id': f'https://osf.io/f{abs(hash(path)) % 9999}', 'fileName': [{'@value': path.rsplit('/', 1)[-1]}], 'filePath': [{'@value': '/' + path}],
                       'isContainedBy': [{'@id': f'https://osf.io/{project}', 'title': [{'@value': title}]}]})


def project_card(guid, title):
    return trove_card({'@id': f'https://osf.io/{guid}', 'title': [{'@value': title}], 'resourceType': [{'@id': 'Project'}]})


def build_web(crossref_search=None):
    web = FakeWeb()
    trove = 'https://share.osf.io/trove/index-card-search'
    # ---- OSF: the regression project (found by metadata only; its files are not in the file index)
    ctd34_tree = {
        '': [('README.md', README_CTD34)],
        'Experiment 1/': [('Experiment 1/AnalysisExp1OSF.py', b'import eyelinkparser\n'), ('Experiment 1/LME_Experiment1.R', b'library(lme4)\n')],
        'Experiment 1/Pupil_Data/': [(f'Experiment 1/Pupil_Data/pp0{i}.asc', ASC) for i in (1, 2, 4)],
        'Experiment 1/Pupil_Data/Not_included/': [('Experiment 1/Pupil_Data/Not_included/pp03.asc', ASC)],
        'Experiment 1/Behavioural_Data/': [(f'Experiment 1/Behavioural_Data/0{i}-results.mat', b'MATLAB 5.0 MAT-file' + bytes(300)) for i in (1, 2, 4)],
        'Experiment 1/Behavioural_Data/Not_Included/': [('Experiment 1/Behavioural_Data/Not_Included/03-results.mat', b'MATLAB 5.0 MAT-file' + bytes(300))],
        'Experiment 2/': [('Experiment 2/Behavioural_Data_21', b'')],
    }
    osf_project(web, 'ctd34', TITLE_CTD34, '', 'Marleen Abbestee', ctd34_tree, wiki_text='')
    # ---- OSF: a project found through the file index, with EyeLink and EEG .edf files
    nback_readme = (b'N-back working memory and pupil dilation.\n\nWe recorded pupil diameter with an EyeLink 1000 while 30 participants '
                    b'performed 0-back, 1-back and 2-back working memory blocks. Pupil dilation increased with memory load.\n\n'
                    b'Please cite: Doe, J. & Roe, R. (2023). Pupil dilation tracks working memory load. Journal of Examples. https://doi.org/' + NBACK_DOI.encode() + b'\n'
                    b'The analysis uses eyelinkparser (https://doi.org/10.5555/software.eyelinkparser).\n')
    osf_project(web, 'nb4ck', 'Pupil dilation during n-back working memory', 'Raw EyeLink data, EEG and scripts.', 'Jane Doe', {
        '': [('README.txt', nback_readme)],
        'data/': [('data/sub-01.edf', EDF_EYELINK), ('data/sub-02.edf', EDF_EYELINK)],
        'eeg/': [('eeg/sub-01_eeg.edf', EDF_EEG)],
    }, wiki_text='Working memory n-back study. Pupil size recorded at 1000 Hz.')

    def trove_route(url, headers):
        q = parse_qs(urlsplit(url).query)
        text = (q.get('cardSearchText') or [''])[0]
        if 'page[cursor]' in q:
            return trove_page([file_card('x', 'nb4ck', 'Pupil dilation during n-back working memory', 'data/sub-02.edf')], 2)
        if q.get('cardSearchFilter[resourceType]') == ['File']:
            if text == 'edf':
                return trove_page([file_card('x', 'nb4ck', 'Pupil dilation during n-back working memory', 'data/sub-01.edf')], 2, trove + '?page%5Bcursor%5D=abc&cardSearchText=edf')
            return trove_page([], 0)
        cards = [project_card('ctd34', TITLE_CTD34)] if re.search(r'(?i)pupil|revealing', text) else []
        return trove_page(cards, len(cards))
    web.add(r'^https://share\.osf\.io/trove/index-card-search', trove_route)

    # ---- Zenodo: record with an explicit link to its article, files inside a ZIP; and a GIS record with .asc grids
    zen_files = [{'key': 'data.zip', 'size': len(ZENODO_ZIP), 'checksum': 'md5:' + __import__('hashlib').md5(ZENODO_ZIP).hexdigest(), 'links': {'self': 'https://zenodo.org/api/records/1234567/files/data.zip/content'}},
                 {'key': 'README.md', 'size': 200, 'checksum': 'md5:x', 'links': {'self': 'https://zenodo.org/api/records/1234567/files/README.md/content'}}]
    zen = {'id': 1234567, 'doi': '10.5281/zenodo.1234567', 'created': '2024-05-01T10:00:00', 'links': {'self_html': 'https://zenodo.org/records/1234567'},
           'metadata': {'title': 'Data for: Pupil-linked arousal tracks surprise in an auditory oddball task', 'description': '<p>EyeLink recordings (ASC) and analysis code for an auditory oddball experiment.</p>',
                        'creators': [{'name': 'Smith, Alex'}, {'name': 'Lee, Bo'}], 'license': {'id': 'cc-by-4.0'}, 'access_right': 'open', 'resource_type': {'type': 'dataset'},
                        'related_identifiers': [{'identifier': ARTICLE_DOI, 'relation': 'isSupplementTo', 'resource_type': 'publication-article', 'scheme': 'doi'},
                                                {'identifier': '10.5281/zenodo.1111111', 'relation': 'isVersionOf', 'resource_type': 'dataset', 'scheme': 'doi'}]},
           'files': zen_files}
    gis = {'id': 7654321, 'doi': '10.5281/zenodo.7654321', 'created': '2023-01-01', 'links': {'self_html': 'https://zenodo.org/records/7654321'},
           'metadata': {'title': 'Elevation grids for coastal flooding', 'description': 'ESRI ASCII raster digital elevation model.', 'creators': [{'name': 'Grid, G.'}],
                        'access_right': 'open', 'resource_type': {'type': 'dataset'}},
           'files': [{'key': 'dem.asc', 'size': len(GIS_ASC), 'checksum': 'md5:y', 'links': {'self': 'https://zenodo.org/api/records/7654321/files/dem.asc/content'}}]}

    def zenodo_search(url, headers):
        q = parse_qs(urlsplit(url).query)
        query, number = q['q'][0], int(q.get('page', ['1'])[0])
        if 'filetype' in query:
            return {'hits': {'total': 2, 'hits': [gis, zen][number - 1:number] if number <= 2 else []},
                    'links': {'next': url.replace(f'page={number}', f'page={number + 1}')} if number < 2 else {}}
        return {'hits': {'total': 1 if re.search('(?i)pupil', query) else 0, 'hits': [zen] if re.search('(?i)pupil', query) and number == 1 else []}}
    web.add(r'^https://zenodo\.org/api/records\?', zenodo_search)
    web.exact('https://zenodo.org/api/records/1234567', zen)
    web.exact('https://zenodo.org/api/records/7654321', gis)
    web.exact('https://zenodo.org/api/records/1234567/files/data.zip/content', ZENODO_ZIP)
    web.exact('https://zenodo.org/api/records/1234567/files/README.md/content', b'# Oddball pupillometry\nParticipants heard standard and deviant tones while pupil size was recorded. See the article (doi:' + ARTICLE_DOI.encode() + b').\n')
    web.exact('https://zenodo.org/api/records/7654321/files/dem.asc/content', GIS_ASC)

    # ---- Dryad: metadata search only; file downloads need a token
    dryad = {'identifier': 'doi:10.5061/dryad.abc123', 'title': 'Pupil size and memory load', 'abstract': '<p>Pupil diameter during a Sternberg working memory task.</p>',
             'authors': [{'firstName': 'Kim', 'lastName': 'Park'}], 'relatedWorks': [{'relationship': 'primary_article', 'identifierType': 'DOI', 'identifier': 'https://doi.org/10.5555/dryad.paper.1'}],
             'license': 'https://creativecommons.org/publicdomain/zero/1.0/', 'publicationDate': '2022-02-02', '_links': {'stash:version': {'href': '/api/v2/versions/999'}}}
    web.add(r'^https://datadryad\.org/api/v2/search\?', lambda u, h: {'total': 1, '_embedded': {'stash:datasets': [dryad] if 'pupil' in u.lower() else []}})
    web.exact('https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.abc123', dryad)
    web.exact('https://datadryad.org/api/v2/versions/999/files', {'_embedded': {'stash:files': [
        {'path': 'sub1.asc', 'size': 1234567, 'digest': 'd' * 32, 'digestType': 'md5', '_links': {'stash:download': {'href': '/api/v2/files/1/download'}}},
        {'path': 'README.md', 'size': 900, '_links': {'stash:download': {'href': '/api/v2/files/2/download'}}}]}, '_links': {}})

    # ---- GitHub: a software package with test fixtures and an LFS pointer
    gh_tree = [{'path': 'README.md', 'type': 'blob', 'size': 120, 'sha': '1' * 40}, {'path': 'pyproject.toml', 'type': 'blob', 'size': 50, 'sha': '2' * 40},
               {'path': 'tests/data/sample.asc', 'type': 'blob', 'size': len(ASC), 'sha': '3' * 40}, {'path': 'data/raw/p01.asc', 'type': 'blob', 'size': len(LFS_POINTER), 'sha': '4' * 40}]
    web.add(r'^https://api\.github\.com/search/repositories\?', lambda u, h: {'total_count': 1, 'items': [{'full_name': 'lab/pupil-tools', 'html_url': 'https://github.com/lab/pupil-tools', 'description': 'Pupillometry preprocessing toolbox'}]} if 'pupil' in u.lower() else {'total_count': 0, 'items': []})
    web.exact('https://api.github.com/repos/lab/pupil-tools', {'full_name': 'lab/pupil-tools', 'html_url': 'https://github.com/lab/pupil-tools', 'description': 'Pupillometry preprocessing toolbox with example data',
                                                               'default_branch': 'main', 'license': {'spdx_id': 'MIT'}, 'owner': {'login': 'lab'}, 'topics': ['pupillometry'], 'created_at': '2021-01-01T00:00:00Z'})
    web.exact('https://api.github.com/repos/lab/pupil-tools/git/trees/main?recursive=1', {'tree': gh_tree, 'truncated': False})
    web.exact('https://api.github.com/repos/lab/pupil-tools/readme', {'path': 'README.md', 'html_url': 'https://github.com/lab/pupil-tools#readme', 'content': __import__('base64').b64encode(b'# pupil-tools\nPreprocessing functions for pupil size data. Example data in tests/data are for unit tests only.').decode()})
    web.exact('https://raw.githubusercontent.com/lab/pupil-tools/main/tests/data/sample.asc', ASC)
    web.exact('https://raw.githubusercontent.com/lab/pupil-tools/main/data/raw/p01.asc', LFS_POINTER)

    # ---- Publication sources
    article = {'DOI': ARTICLE_DOI, 'type': 'journal-article', 'title': ['Pupil-linked arousal tracks surprise in an auditory oddball task'], 'author': [{'given': 'Alex', 'family': 'Smith'}, {'given': 'Bo', 'family': 'Lee'}],
               'issued': {'date-parts': [[2024]]}, 'container-title': ['Journal of Examples'], 'link': [{'URL': 'https://publisher.example/oddball.pdf', 'content-type': 'application/pdf'}]}
    nback = {'DOI': NBACK_DOI, 'type': 'journal-article', 'title': ['Pupil dilation tracks working memory load'], 'author': [{'given': 'Jane', 'family': 'Doe'}, {'given': 'R.', 'family': 'Roe'}],
             'issued': {'date-parts': [[2023]]}, 'container-title': ['Journal of Examples'], 'link': []}
    web.exact(f'https://api.crossref.org/works/{ARTICLE_DOI}', {'message': article})
    web.exact(f'https://api.crossref.org/works/{NBACK_DOI}', {'message': nback})
    web.exact('https://api.crossref.org/works/10.5555/dryad.paper.1', {'message': {'DOI': '10.5555/dryad.paper.1', 'type': 'journal-article', 'title': ['Pupil size indexes memory load'], 'author': [{'given': 'Kim', 'family': 'Park'}], 'issued': {'date-parts': [[2022]]}}})
    web.exact('https://api.datacite.org/dois/10.5555/software.eyelinkparser', {'data': {'attributes': {'types': {'resourceTypeGeneral': 'Software'}, 'titles': [{'title': 'eyelinkparser'}], 'creators': [{'name': 'Mathôt, S.'}], 'publicationYear': 2020}}})
    unrelated = [{'DOI': '10.31234/osf.io/n7uvj', 'type': 'posted-content', 'title': ['Interactions Between Visual Working Memory, Attention, and Color Categories: a Pupillometry Study'],
                  'author': [{'given': 'Thomas', 'family': 'Wilschut'}, {'given': 'Sebastiaan', 'family': 'Mathot'}], 'issued': {'date-parts': [[2021]]}},
                 {'DOI': '10.4324/9780203769362-18', 'type': 'book-chapter', 'title': ['Revealing the hidden'], 'author': [], 'issued': {'date-parts': [[2004]]}}]
    web.add(r'^https://api\.crossref\.org/works\?', crossref_search or (lambda u, h: {'message': {'items': [] if 'query.author' in u else unrelated}}))
    web.add(r'^https://api\.openalex\.org/works\?', {'meta': {'count': 1}, 'results': [{'doi': 'https://doi.org/10.1234/covert', 'display_name': 'Uncovering distinct drivers of covert attention', 'type': 'article',
                                                                                           'authorships': [{'author': {'display_name': 'Other Person'}}], 'publication_year': 2025}]})
    web.add(r'^https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search\?', {'hitCount': 0, 'resultList': {'result': []}})
    web.exact('https://publisher.example/oddball.pdf', ARTICLE_PDF)
    web.add(r'^https://doi\.org/', Reply(CLOUDFLARE, 403))
    return web


def zip_central_directory_supported(web):
    return any('data.zip' in u and r for u, r in web.calls)
