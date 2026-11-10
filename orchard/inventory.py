"""File roles, header probes and the Eyeris reuse checklist for a repository project.

Roles are rule-based and always carry a reason. A path ending in .edf or .asc is only a
*candidate* recording: .edf is also the European Data Format (EEG/PSG) and ESRF image format,
and .asc is used for GIS grids and many other text exports. A header read is the evidence.
"""
import re
import zlib

from . import eyelink

EYE_EXT = re.compile(r'\.(edf|asc)(\.gz)?$', re.I)
ARCHIVE = re.compile(r'\.(zip|tar|tgz|tar\.gz|tar\.bz2|tar\.xz|7z|rar)$', re.I)
CODE = re.compile(r'\.(py|r|m|ipynb|jl|rmd|qmd|sh|js|stan|sas|sps|do|c|cpp|psyexp|osexp|ebs2|es3|exp)$', re.I)
DOC = re.compile(r'(?i)(^|/)(readme|codebook|data[ _-]?dictionary|citation|license|licence|notes?|protocol|methods?)[^/]*$|\.(md|rst|pdf|docx?|odt)$')
TABULAR = re.compile(r'\.(csv|tsv|txt|xlsx?|mat|rds|rdata|sav|json|pkl|pickle|feather|parquet|h5|hdf5|npy|npz|dat|log|edat2?|txt\.gz)$', re.I)
BEHAVIOR = re.compile(r'(?i)(^|[/_\-\s])(beh|behav|behaviou?r(al)?|events?|trials?|responses?|answers?|logs?|psychopy|eprime|e-prime|conditions?|ratings?|questionnaires?|demographics?)([/_\-\s.]|$)')
PROCESSED = re.compile(r'(?i)(^|[/_\-\s])(processed|preprocessed|clean(ed)?|epoch(ed|s)?|binned|averaged?|means?|summary|summaries|aggregated?|group|grand|derivatives?|stats?|figures?|output|results?[_\-\s]?(table|summary))([/_\-\s.]|$)')
FIXTURE = re.compile(r'(?i)(^|/)(tests?|testing|test[_-]?data|test[_-]?files|fixtures?|unit[_-]?tests?|testthat|spec)(/|$)')
EXAMPLE = re.compile(r'(?i)(^|/)(examples?|demos?|samples?|sample[_-]?data|tutorials?|vignettes?|toy|dummy)(/|$)|(^|/)[^/]*(example|sample|demo|dummy|toy)[^/]*$')
EXCLUDED = re.compile(r'(?i)(^|/)([^/]*(not[\s_\-]?included|excluded|exclusions?|rejected|discard(ed)?|outliers?|removed|dropped|unused|bad[\s_\-]?(subjects?|participants?|data|sessions?)|not[\s_\-]?used|incomplete)[^/]*)(/|$)')
PARTICIPANT = re.compile(r'(?i)(?:^|[/_\-\s])((?:sub|subj|subject|pp|p|s|participant|part|vp)[\-_]?\d{1,4}|\d{1,4}(?=[\-_](?:results?|data|beh)))')

ROLE_LABELS = {
    'recording': 'Eye-tracking recording (candidate)',
    'recording_excluded_group': 'Recording in an exclusion folder',
    'behavioral': 'Behavioral / event data',
    'code': 'Analysis or task code',
    'documentation': 'Documentation',
    'processed': 'Processed or summary data',
    'example_or_fixture': 'Example file or software test fixture',
    'placeholder': 'Placeholder (empty file or pointer)',
    'not_eyelink': 'Same extension, not EyeLink data',
    'archive': 'Archive',
    'other': 'Other file',
}


def exclusion_group(path):
    """Folder (or file) name that marks participants excluded from the original analysis."""
    match = EXCLUDED.search(path or '')
    if not match:
        return None
    end = match.end(2)
    return (path[:end]).strip('/')


def participant_id(path):
    name = (path or '').rsplit('/', 1)[-1]
    match = PARTICIPANT.search('/' + name)
    if not match:
        return None
    digits = re.search(r'\d+', match.group(1))
    return str(int(digits.group(0))) if digits else None


def classify(path, size=None, header=None, has_package_files=False):
    """Return (role, reason). ``header`` is the probe result when available."""
    path = path or ''
    low = path.lower()
    eye = bool(EYE_EXT.search(low))
    group = exclusion_group(path)
    if size == 0:
        return 'placeholder', 'Zero-byte file.'
    if header in ('git_lfs_pointer', 'git_annex_pointer'):
        return 'placeholder', 'Header is a ' + header.replace('_', ' ') + '; the recording itself is stored elsewhere.'
    if eye and header in ('european_data_format',):
        return 'not_eyelink', 'Header is European Data Format (EEG/PSG), not SR Research EyeLink.'
    if eye and header == 'unrecognized':
        return 'not_eyelink', 'Header read, but it does not match EyeLink EDF/ASC output.'
    if eye and size is not None and size < 1024:
        return 'placeholder', f'Only {size} bytes; too small to be a recording.'
    if eye and FIXTURE.search(path):
        if has_package_files:
            return 'example_or_fixture', 'Inside a software test folder of a package; likely a test fixture, not study data.'
        return 'example_or_fixture', 'Inside a folder named as a test; likely pilot or test sessions rather than study recordings. Check the README.'
    if eye and EXAMPLE.search(path):
        return 'example_or_fixture', 'Path names it an example, sample, demo or tutorial file.'
    if eye:
        confirmed = ' Header confirms EyeLink output.' if header == 'eyelink' else ' Header not checked.' if header in (None, 'not_probed') else f' Header check: {header}.'
        if group:
            return 'recording_excluded_group', f'Inside "{group}", which marks participants excluded from the original analysis.' + confirmed
        return 'recording', '.edf/.asc file outside example, test and exclusion folders.' + confirmed
    if ARCHIVE.search(low):
        return 'archive', 'Archive; members are listed separately when its directory can be read.'
    if CODE.search(low):
        return 'code', 'Script or task-program file extension.'
    if DOC.search(path):
        return 'documentation', 'README, codebook, licence or document file.'
    if TABULAR.search(low) and BEHAVIOR.search(path):
        return 'behavioral', 'Data file in a behavioral/event/trial folder or with such a name.'
    if TABULAR.search(low) and PROCESSED.search(path):
        return 'processed', 'Name or folder indicates processed, summary or derived data.'
    if FIXTURE.search(path) or EXAMPLE.search(path):
        return 'example_or_fixture', 'In an example or test folder.'
    if TABULAR.search(low):
        return 'other', 'Data file; its role is not clear from its name.'
    return 'other', 'No rule matched.'


# --------------------------------------------------------------------------- headers

def asc_details(text):
    """Acquisition details visible in an EyeLink header excerpt (ASC text or EDF preamble)."""
    details = {}
    for key, pattern in (('converted_from', r'\*\* CONVERTED FROM (.+?) using'), ('recorded', r'\*\* DATE:\s*(.+)'),
                         ('tracker', r'\*\* VERSION:\s*(.+)'), ('source', r'\*\* SOURCE:\s*(.+)'),
                         ('pupil', r'^PUPIL\s+(AREA|DIAMETER)'), ('samples', r'^SAMPLES\s+(.+)'),
                         ('start', r'^START\s+\d+\s+(.+)')):
        match = re.search(pattern, text, re.M)
        if match:
            details[key] = ' '.join(match.group(1).split())
    rate = re.search(r'RATE\s+([\d.]+)', details.get('samples', ''))
    if rate:
        details['sample_rate_hz'] = float(rate.group(1))
    eyes = [e for e in ('LEFT', 'RIGHT') if e in details.get('samples', details.get('start', ''))]
    if eyes:
        details['eyes'] = eyes
    details['messages'] = len(re.findall(r'^MSG\s+\d+', text, re.M))
    trial = re.search(r'^MSG\s+\d+\s+(?:-?\d+\s+)?(TRIALID|TRIAL_?START|SYNCTIME|START_TRIAL|!V TRIAL_VAR)[^\n]*', text, re.M | re.I)
    if trial:
        details['trial_marker'] = trial.group(1)
    details['sample_lines'] = len(re.findall(r'^\d+\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+', text, re.M))
    return details


def lfs_pointer(head):
    text = head[:400].decode('ascii', 'replace')
    oid, size = re.search(r'oid sha256:([0-9a-f]{64})', text), re.search(r'\nsize (\d+)', text)
    return {'oid': oid.group(1) if oid else None, 'size': int(size.group(1)) if size else None}


def probe(client, url, kind, budget=None, limit=65_536):
    """Read the start of a remote .edf/.asc file. Returns (header, details)."""
    if budget:
        budget.spend()
    data, _, _ = client.get(url, headers={'Range': f'bytes=0-{limit - 1}'}, limit=limit, truncate=True)
    raw = data
    if raw[:2] == b'\x1f\x8b':
        try:
            raw = zlib.decompressobj(31).decompress(raw, limit)
        except zlib.error:
            return 'unrecognized', {'note': 'gzip stream could not be read'}
    header = eyelink.header_kind(raw, kind)
    details = {}
    if header == 'eyelink':
        details = asc_details(raw.decode('latin-1'))
        if kind == 'edf':
            # EDF binary files only reveal their text preamble; samples need conversion (edf2asc).
            details.pop('sample_lines', None)
    elif header == 'git_lfs_pointer':
        details = lfs_pointer(raw)
    elif header == 'european_data_format':
        details = {'note': raw[8:88].decode('latin-1', 'replace').strip()}
    if data[:5] == b'%PDF-' or data[:15].lower().startswith(b'<!doctype html') or data[:6].lower() == b'<html>':
        header, details = 'unrecognized', {'note': 'Server returned a web page or document instead of the file.'}
    return header, details


def kind_of(path):
    match = EYE_EXT.search(path or '')
    return match.group(1).lower() if match else None


def probe_plan(files, included=3, excluded=2):
    """Pick a few recordings to read: some from the analysed sample, some from exclusion folders, and suspicious sizes."""
    eye = [f for f in files if kind_of(f['path']) and f.get('probe_url') and '!/' not in f['path']]
    small = [f for f in eye if f.get('size') is not None and f['size'] < 4096]
    normal = [f for f in eye if not exclusion_group(f['path']) and f not in small]
    groups = [f for f in eye if exclusion_group(f['path']) and f not in small]
    pick = small[:2] + normal[:included] + groups[:excluded]
    return list({id(f): f for f in pick}.values())


# --------------------------------------------------------------------------- Eyeris checklist

def checklist(files, documents_text, license_name, probes):
    """What can and cannot yet be said about reprocessing this dataset with eyeris.

    Each item is 'observed' (seen in file headers or listings), 'documented' (stated in the
    repository's own text) or 'unverified'. Nothing here establishes compatibility.
    """
    text = documents_text.lower()
    eyelink = [p for p in probes if p['header'] == 'eyelink']
    recordings = [f for f in files if f['role'] in ('recording', 'recording_excluded_group')]
    items = []

    def item(key, label, status, detail):
        items.append({'key': key, 'label': label, 'status': status, 'detail': detail})

    pupils = sorted({p['details'].get('pupil') for p in eyelink if p['details'].get('pupil')})
    if pupils:
        item('pupil_channel', 'Pupil channel', 'observed', f'Header reports PUPIL {", ".join(pupils)} in {len(eyelink)} probed file(s). Other files not read.')
    elif eyelink:
        item('pupil_channel', 'Pupil channel', 'unverified', 'EyeLink headers read, but the PUPIL line was outside the bytes read (EDF binaries need edf2asc).')
    else:
        item('pupil_channel', 'Pupil channel', 'unverified', 'No EyeLink header has been read yet.')
    if re.search(r'pupil[^.\n]{0,80}\b(mm|millimet)', text):
        item('units', 'Pupil units', 'documented', 'Documentation mentions millimetres; check how values were calibrated.')
    else:
        item('units', 'Pupil units', 'unverified', 'EyeLink pupil values are in arbitrary units unless a calibration is documented.')
    rates = sorted({p['details'].get('sample_rate_hz') for p in eyelink if p['details'].get('sample_rate_hz')})
    item('sampling', 'Sampling rate and eyes', 'observed' if rates else 'unverified',
         (f'{", ".join(f"{r:g} Hz" for r in rates)}; eyes: ' + ', '.join(sorted({e for p in eyelink for e in p['details'].get('eyes', [])}) or ['not stated'])) if rates else 'Not read from any header yet.')
    stamps = [p for p in eyelink if p['details'].get('sample_lines') or p['details'].get('messages')]
    item('timestamps', 'Timestamps', 'observed' if stamps else 'unverified',
         'Millisecond tracker timestamps on samples/messages in the probed excerpt.' if stamps else 'Not seen in a header excerpt.')
    markers = sorted({p['details'].get('trial_marker') for p in eyelink if p['details'].get('trial_marker')})
    beh = [f for f in files if f['role'] == 'behavioral']
    item('events', 'Event alignment', 'observed' if markers else 'unverified',
         (f'Trial messages ({", ".join(markers)}) in probed excerpts' if markers else 'No trial messages in the probed excerpts')
         + (f'; {len(beh)} behavioral/event file(s) listed' if beh else '; no behavioral/event files identified') + '. Alignment across files is not verified.')
    rec_ids = {participant_id(f['path']) for f in recordings} - {None}
    beh_ids = {participant_id(f['path']) for f in beh} - {None}
    if rec_ids:
        overlap = len(rec_ids & beh_ids)
        item('participants', 'Participant identifiers', 'observed',
             f'{len(rec_ids)} participant IDs in recording names' + (f'; {overlap} also appear in behavioral file names' if beh_ids else '') + '. Mapping is inferred from names only.')
    else:
        item('participants', 'Participant identifiers', 'unverified', 'No participant pattern in recording names.')
    item('licence', 'Licence', 'documented' if license_name else 'unverified', license_name or 'No licence stated in repository metadata; reuse terms unknown.')
    code = [f for f in files if f['role'] == 'code']
    item('analysis', 'Original analysis', 'observed' if code else 'unverified',
         f'{len(code)} script file(s) listed; not executed or reviewed.' if code else 'No analysis code listed.')
    edfs = [f for f in recordings if kind_of(f['path']) == 'edf']
    if edfs and not [f for f in recordings if kind_of(f['path']) == 'asc']:
        item('format', 'File format', 'observed', 'Only binary .edf files; eyeris reads EyeLink .asc, so conversion with SR Research edf2asc is needed.')
    elif recordings:
        item('format', 'File format', 'observed', 'EyeLink .asc text files are listed; whether each parses cleanly is not verified.')
    return items
