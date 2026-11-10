"""Relevant-context screening for repository projects.

Method (deterministic, no machine learning): text from the repository (description, README,
wiki, citation files) and from any retrieved article is split into passages of about two
sentences. Each passage is checked against configurable concept lists. The assessment rests on
*passages*, not isolated words: a passage that mentions a pupil measure together with a
psychological or cognitive task concept is the evidence for "likely relevant". Exclusion
concepts (for example the pupillary light reflex alone, or non-human subjects) produce
"possible exclusion" with the passage shown, never an automatic exclusion.

Limitations: wording outside the concept lists is missed; negations ("we did not measure pupil
size") are not understood; a README can describe a different study than its files. The same
text and concept version always give the same result, and every result records both.
"""
import hashlib
import json
import re
import unicodedata

METHOD = 'orchard-passage-rules'
METHOD_VERSION = '1.0'
MIN_WORDS = 40

# Default concept lists, taken from docs/search-terms.txt (the project's literature-search terms).
# EEG is deliberately not an exclusion concept: simultaneous EEG and pupillometry is common, and EEG-format
# .edf files are recognised from their headers instead.
DEFAULT_CONCEPTS = {
    'measurement': ['pupillometr*', 'pupil size', 'pupil diameter', 'pupil area', 'pupil dilation', 'pupillary dilation',
                    'pupil dilatation', 'pupillary response', 'pupil response', 'pupil constriction', 'task-evoked pupil*',
                    'event-related pupil*', 'phasic pupil', 'tonic pupil', 'baseline pupil', 'pupil-linked', 'pupil dynamics',
                    'TEPR', 'pupil old/new', 'pupil-indexed', 'pupil data', 'pupil trace', 'pupil signal'],
    'acquisition': ['eyelink', 'eye tracker', 'eye-tracker', 'eye tracking', 'eyetracking', 'eye-tracking', 'SR Research', 'oculometry'],
    'tasks': {
        'Working and short-term memory': ['working memory', 'short-term memory', 'memory load', 'memory capacity', 'digit span', 'operation span', 'n-back', 'Sternberg', 'change detection', 'delayed recall', 'delayed estimation', 'retro-cue', 'set size'],
        'Episodic and recognition memory': ['episodic memory', 'memory encoding', 'encoding phase', 'memory retrieval', 'retrieval phase', 'recognition memory', 'recollection', 'familiarity', 'old/new', 'subsequent memory', 'associative memory', 'source memory', 'free recall', 'cued recall', 'forgetting'],
        'Attention': ['attention', 'attentional', 'sustained attention', 'vigilance', 'mind wandering', 'attentional lapse', 'covert attention', 'spatial attention', 'visual search', 'attentional capture', 'Posner', 'attentional blink', 'RSVP', 'cueing', 'attentional field'],
        'Emotion and affect': ['emotion*', 'affective', 'valence', 'emotional arousal', 'IAPS', 'emotional faces', 'emotion regulation', 'threat', 'fear conditioning', 'aversive'],
        'Cognitive control': ['cognitive control', 'executive function', 'inhibition', 'Stroop', 'flanker', 'Simon task', 'go/no-go', 'stop-signal', 'antisaccade', 'task switching', 'conflict'],
        'Mental effort and workload': ['mental effort', 'cognitive effort', 'cognitive load', 'mental workload', 'task difficulty', 'mental arithmetic', 'dual task', 'listening effort'],
        'Decision-making and learning': ['decision*', 'confidence', 'metacognition', 'evidence accumulation', 'choice', 'random-dot motion', 'prediction error', 'surprise', 'oddball', 'novelty', 'reversal learning', 'statistical learning', 'reward', 'incentive', 'motivation', 'bandit'],
        'Language and listening': ['lexical decision', 'semantic processing', 'sentence comprehension', 'reading comprehension', 'speech perception', 'speech-in-noise', 'syntactic'],
        'Arousal and fatigue': ['arousal', 'locus coeruleus', 'noradrenergic', 'norepinephrine', 'mental fatigue', 'time-on-task', 'sleepiness', 'alertness'],
        'Social cognition and imagery': ['social attention', 'joint attention', 'gaze cueing', 'face processing', 'mental imagery', 'visual imagery', 'perceptual awareness', 'binocular rivalry'],
        'General experimental task': ['experiment', 'participants', 'trials', 'task', 'condition*', 'stimulus', 'stimuli', 'cognitive', 'psycholog*'],
    },
    'exclusion': {
        'Pupillary light reflex or ophthalmic measurement only': ['pupillary light reflex', 'neurological pupil index', 'NPi', 'anisocoria', 'ophthalmolog*', 'refractive', 'myopia', 'cataract'],
        'Clinical or pharmacological monitoring': ['anesthesia', 'anaesthesia', 'intensive care', 'ICU', 'opioid', 'sedation', 'traumatic brain injury'],
        'Non-human participants': ['mice', 'mouse', 'rat', 'rats', 'macaque*', 'monkey*', 'rhesus', 'non-human primate*', 'zebrafish', 'drosophila'],
        'Not eye-tracking data (other EDF/ASC use)': ['polysomnograph*', 'sleep staging', 'ascii grid', 'raster', 'elevation', 'bathymetr*', 'X-ray', 'diffraction', 'spectroscop*'],
        'Software, simulation or test material': ['unit test', 'test file*', 'test data', 'example data', 'simulated data', 'synthetic data', 'demo data', 'toy data'],
    },
}

GENERIC_TASK_GROUP = 'General experimental task'
# A passage linking pupil measurement to one of these groups is the strongest relevance evidence.
# The generic group still counts, but only as supporting evidence.


def normalize(text):
    text = unicodedata.normalize('NFKC', str(text or ''))
    return re.sub(r'\s+', ' ', text)


def term_pattern(term):
    """Regex for a configured term: case-insensitive, tolerant of hyphen/space/underscore/no-space
    variants (pupil size ~ pupil-size ~ pupil_size ~ pupilsize) and plurals; '*' is a suffix wildcard."""
    term = term.strip()
    wildcard = term.endswith('*')
    words = re.split(r'[\s\-_]+', term.rstrip('*'))
    parts = [re.escape(w) for w in words if w]
    body = r'[\s\-_]?'.join(parts)
    if wildcard:
        body += r'\w*'
    elif re.fullmatch(r'[A-Za-z]+', words[-1]) and len(words[-1]) > 3:
        body += r'(?:s|es)?'
    if len(term) <= 4 and term.isupper():
        return re.compile(r'(?<![A-Za-z0-9])' + body + r'(?![A-Za-z0-9])')
    return re.compile(r'(?<![A-Za-z0-9])' + body + r'(?![A-Za-z0-9])', re.I)


def compile_concepts(concepts):
    groups = {'measurement': {'Pupil measurement': concepts.get('measurement', [])},
              'acquisition': {'Eye-tracking acquisition': concepts.get('acquisition', [])},
              'tasks': concepts.get('tasks', {}), 'exclusion': concepts.get('exclusion', {})}
    return {kind: {label: [(t, term_pattern(t)) for t in terms if t.strip()] for label, terms in g.items()} for kind, g in groups.items()}


def passages(text, size=2):
    """Split text into overlapping two-sentence windows with character offsets.
    Sentence ends, blank lines, Markdown headings and table rows all end a sentence."""
    text = re.sub(r'[ \t\r\f\v]+', ' ', unicodedata.normalize('NFKC', str(text or '')))
    spans, start = [], 0
    for match in re.finditer(r'(?<=[.!?])\s+(?=[A-Z0-9("\[#])|\n\s*\n|\n(?=\s*(?:#|\||[-*] ))|\|\s*\n', text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    spans = [(a, b) for a, b in spans if text[a:b].strip()]
    out = []
    for i in range(len(spans)):
        a, b = spans[i][0], spans[min(i + size, len(spans)) - 1][1]
        if b - a > 1200:
            b = a + 1200
        out.append((a, b, text[a:b].strip()))
    return out


def find(groups, passage):
    hits = {}
    for label, terms in groups.items():
        found = sorted({m.group(0) for _, pattern in terms for m in [pattern.search(passage)] if m})
        if found:
            hits[label] = found
    return hits


def assess(sources, concepts=None, files=None, article_status=None):
    """Assess relevance from text sources.

    ``sources``: list of {'id', 'kind', 'title', 'text'}; ``files``: inventory rows with role/header;
    ``article_status``: 'readable' | 'inaccessible' | 'not_identified' | None.
    Returns status, reason, evidence passages and a reproducibility record.
    """
    concepts = concepts or DEFAULT_CONCEPTS
    compiled = compile_concepts(concepts)
    evidence, counts, words = [], {}, 0
    seen = set()
    for source in sources:
        text = source.get('text') or ''
        words += len(re.findall(r'\w+', text))
        for a, b, chunk in passages(text):
            hits = {kind: find(compiled[kind], chunk) for kind in compiled}
            hits = {k: v for k, v in hits.items() if v}
            if not hits:
                continue
            specific = {k: v for k, v in hits.get('tasks', {}).items() if k != GENERIC_TASK_GROUP}
            if hits.get('measurement') and specific:
                kind = 'pupil_and_task'
            elif hits.get('exclusion'):
                kind = 'exclusion'
            elif hits.get('measurement') and hits.get('tasks'):
                kind = 'pupil_and_general_task'
            elif hits.get('measurement'):
                kind = 'pupil_only'
            elif specific:
                kind = 'task_only'
            elif hits.get('acquisition'):
                kind = 'acquisition'
            else:
                continue
            key = (kind, chunk[:160])
            if key in seen:
                continue
            seen.add(key)
            counts[kind] = counts.get(kind, 0) + 1
            evidence.append({'kind': kind, 'source_id': source.get('id'), 'source_kind': source.get('kind'), 'source_title': source.get('title'),
                             'offset': a, 'passage': chunk, 'concepts': hits})
    files = files or []
    confirmed = [f for f in files if f.get('header') == 'eyelink']
    pupil_header = [f for f in confirmed if (f.get('header_details') or {}).get('pupil')]
    not_eyelink = [f for f in files if f.get('role') == 'not_eyelink']
    fixtures = [f for f in files if f.get('role') == 'example_or_fixture']
    recordings = [f for f in files if f.get('role') in ('recording', 'recording_excluded_group')]
    exclusion_groups = sorted({label for e in evidence for label in e['concepts'].get('exclusion', {})})
    strong = counts.get('pupil_and_task', 0)
    notes = []
    if pupil_header:
        notes.append(f'{len(pupil_header)} file header(s) record a pupil channel (acquisition evidence, not task context).')
    if not recordings and (not_eyelink or fixtures):
        notes.append('No study recordings remain after header checks and example/test-file rules.')
    if words < MIN_WORDS and not evidence:
        status, reason = 'insufficient_information', f'Only {words} words of readable documentation or article text; relevance cannot be judged.'
    elif strong and exclusion_groups:
        status, reason = 'needs_review', f'{strong} passage(s) link pupil measurement to a task, but exclusion concepts also appear ({"; ".join(exclusion_groups)}).'
    elif strong:
        status, reason = 'likely_relevant', f'{strong} passage(s) link pupil measurement to a psychological or cognitive task.'
    elif exclusion_groups or (not recordings and (not_eyelink or fixtures)):
        status = 'possible_exclusion'
        reason = ('Exclusion concepts: ' + '; '.join(exclusion_groups) + '.') if exclusion_groups else 'Files do not look like study recordings.'
    elif evidence:
        status, reason = 'needs_review', 'Some relevant wording, but no passage links pupil measurement to a task (' + ', '.join(f'{k}: {v}' for k, v in sorted(counts.items())) + ').'
    else:
        status, reason = 'insufficient_information', f'{words} words read; none match the configured concepts. Absence of matching words is not evidence of irrelevance.'
    if article_status == 'inaccessible':
        notes.append('The associated article could not be retrieved; relevance rests on repository text only.')
    elif article_status == 'not_identified':
        notes.append('No associated article identified; relevance rests on repository text only.')
    record = {'method': METHOD, 'method_version': METHOD_VERSION, 'concepts_sha256': concepts_hash(concepts),
              'inputs_sha256': hashlib.sha256(json.dumps([[s.get('id'), s.get('text')] for s in sources], sort_keys=True).encode()).hexdigest(),
              'words_read': words, 'semantic_model': None}
    order = {'pupil_and_task': 0, 'pupil_and_general_task': 1, 'exclusion': 2, 'pupil_only': 3, 'task_only': 4, 'acquisition': 5}
    evidence.sort(key=lambda e: order[e['kind']])
    return {'status': status, 'reason': reason, 'notes': notes, 'counts': counts, 'evidence': evidence[:60], 'reproducibility': record}


def concepts_hash(concepts):
    return hashlib.sha256(json.dumps(concepts, sort_keys=True).encode()).hexdigest()


def parse_concepts(text):
    """Editable format: '[measurement]', '[acquisition]', '[tasks]', '[exclusion]' sections;
    in tasks/exclusion, one 'Group: term; term' line per group."""
    out = {'measurement': [], 'acquisition': [], 'tasks': {}, 'exclusion': {}}
    section = None
    for n, raw in enumerate(str(text or '').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'\[(measurement|acquisition|tasks|exclusion)\]', line, re.I)
        if match:
            section = match.group(1).lower()
            continue
        if section is None:
            raise ValueError(f'Line {n}: start with a [measurement], [acquisition], [tasks] or [exclusion] section.')
        if section in ('measurement', 'acquisition'):
            out[section] += [t.strip() for t in re.split(r'[;,]', line) if t.strip()]
        else:
            if ':' not in line:
                raise ValueError(f'Line {n}: use "Group name: term; term" in the [{section}] section.')
            label, terms = line.split(':', 1)
            out[section][label.strip()] = [t.strip() for t in re.split(r'[;,]', terms) if t.strip()]
    if not out['measurement'] or not out['tasks']:
        raise ValueError('Provide at least one measurement term and one task group.')
    return out


def format_concepts(concepts):
    lines = ['[measurement]', '; '.join(concepts.get('measurement', [])), '', '[acquisition]', '; '.join(concepts.get('acquisition', [])), '', '[tasks]']
    lines += [f'{k}: {"; ".join(v)}' for k, v in concepts.get('tasks', {}).items()]
    lines += ['', '[exclusion]'] + [f'{k}: {"; ".join(v)}' for k, v in concepts.get('exclusion', {}).items()]
    return '\n'.join(lines)


def concepts_from_document(concept_rows):
    """Build task groups from an imported literature-search terms document (documents/concepts/terms)."""
    tasks, measurement = {}, []
    for row in concept_rows:
        terms = [t['original'] for t in row['terms'] if t['original'].strip() and not re.search(r'[\[\]():]', t['original'])]
        if not terms:
            continue
        if re.search(r'(?i)pupil|measurement|baseline|changes in pupil', row['label']):
            measurement += terms
        elif not re.search(r'(?i)open.?data|data availability|repositor|doi|acquisition|software|boolean', row['label']):
            tasks[row['label']] = terms
    return measurement, tasks
