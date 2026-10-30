"""Live browser check of one real repository project, start to finish.

Run a live repository search first (in the app or via orchard.discovery.start), then:

    python scripts/live_project_check.py --data-dir DATA --title "Concealed identity information detection" \
        --doi 10.1111/psyp.14155 --url https://osf.io/9uyzf/

It re-inspects the project against the live APIs, confirms the given DOI among the proposed
publications, retrieves the article, reads it in the embedded reader, saves a decision and checks
the export. Output is bounded text; no screenshots. Needs Playwright with Chromium.
"""
import argparse, io, re, sys, threading, time, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright, expect
from orchard.service import Service
from orchard.server import make_server

ap = argparse.ArgumentParser()
ap.add_argument('--data-dir', required=True)
ap.add_argument('--title', required=True, help='Part of the project title as listed in Orchard')
ap.add_argument('--doi', required=True, help='DOI of the publication to confirm')
ap.add_argument('--url', required=True, help='Original repository URL of the project')
args = ap.parse_args()
TARGET_DOI = args.doi
s = Service(Path(args.data_dir) / 'orchard.sqlite3')
server = make_server(s, 0)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
log = lambda *a: print('>>', *a, flush=True)
try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={'width': 1500, 'height': 1000}, accept_downloads=True)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        answers = ['Same title and same five authors as the OSF project; Psychophysiology 2022 version of the bioRxiv preprint.']
        page.on('dialog', lambda d: d.accept(answers.pop(0) if answers and d.type == 'prompt' else ''))
        go = lambda r: (page.goto(base + '/#' + r), page.reload(), page.wait_for_load_state('networkidle'))

        go('repository-sweep')
        job = page.locator('.d-job').first
        log('latest search:', re.sub(r'\s+', ' ', job.locator('h2').inner_text())[:80], '|', re.sub(r'\s+', ' ', job.locator('p').first.inner_text())[:160])

        go('settings')
        page.fill('#criteria-in', 'Human participants, psychological/cognitive task, pupil size recorded with EyeLink.')
        page.fill('#criteria-out', 'No pupil data; non-human; clinical light reflex only.')
        page.click('[data-act=save-criteria]'); page.wait_for_timeout(500)

        # Find the project in the list (found by the OSF file index during the live search).
        go('repository-projects')
        page.fill('#dp-q', args.title); page.click('[data-d=filter]'); page.wait_for_load_state('networkidle')
        row = page.locator('table.d-projects tr', has_text=args.title)
        expect(row).to_have_count(1)
        log('list row:', re.sub(r'\s+', ' ', row.inner_text())[:220])
        row.locator('a').first.click(); page.wait_for_load_state('networkidle')
        expect(page.locator('a.button:has-text("Open on")')).to_have_attribute('href', args.url)

        # Re-inspect with the current code (live OSF, Crossref, OpenAlex, Europe PMC).
        t = time.time()
        page.click('[data-d=inspect]')
        expect(page.locator('#toast')).to_contain_text('Inspection finished', timeout=900_000)
        log('re-inspection took', int(time.time() - t), 's')
        files = page.locator('#d-files')
        log('files card:', re.sub(r'\s+', ' ', files.locator('h2').inner_text()), '| groups:', [re.sub(r'\s+', ' ', x)[:60] for x in files.locator('details.d-group > summary').all_inner_texts()])
        log('relevance:', re.sub(r'\s+', ' ', page.locator('#d-left section:has(h2:text("Relevant context")) > p').first.inner_text())[:200])

        # Publication candidates: ranking and evidence.
        cands = page.locator('#d-publication .d-candidate')
        log('candidates shown:', cands.count())
        for i in range(min(4, cands.count())):
            c = cands.nth(i)
            log(f'  #{i+1}', re.sub(r'\s+', ' ', c.locator('strong').first.inner_text())[:80], '|', c.locator('.pill').first.inner_text(), '|', re.sub(r'\s+', ' ', c.locator('small').first.inner_text())[-60:])
        page.fill('#d-rev', 'Live check')
        target = cands.filter(has_text=TARGET_DOI)
        if page.locator('#d-publication .d-paper', has_text=TARGET_DOI).count():
            log('already linked in an earlier run; not confirming again')
        else:
            expect(target).to_have_count(1)
            target.locator('[data-d=confirm]').click()
        expect(page.locator('#d-publication')).to_contain_text('Publication confirmed', timeout=60_000)
        log('confirmed', TARGET_DOI)

        # Retrieve and read inside Orchard.
        t = time.time()
        page.locator('[data-d=retrieve]').first.click()
        expect(page.locator('#d-publication')).to_contain_text(re.compile('Read text here|Read PDF here|Retrieval attempts'), timeout=600_000)
        page.wait_for_timeout(1500)
        log('retrieval took', int(time.time() - t), 's; toast:', page.locator('#toast').inner_text()[:200])
        paper = page.locator('#d-publication .d-paper').first
        log('paper block:', re.sub(r'\s+', ' ', paper.inner_text())[:500])
        if paper.locator('[data-d=read-text]').count():
            paper.locator('[data-d=read-text]').first.click()
            pre = page.locator('#d-reader-body pre')
            expect(pre).not_to_be_empty(timeout=15_000)
            text = pre.inner_text()
            log('reader text:', len(text.split()), 'words; mentions pupil:', 'pupil' in text.lower(), '| start:', re.sub(r'\s+', ' ', text)[:200])
        if paper.locator('[data-d=read-pdf]').count():
            paper.locator('[data-d=read-pdf]').first.click()
            src = page.locator('#d-reader-body iframe.d-pdf').get_attribute('src').split('#')[0]
            r = page.request.get(base + src)
            log('reader PDF:', r.status, r.headers.get('content-type'), len(r.body()), 'bytes, starts', r.body()[:5])

        # Decision with cited evidence, then export.
        page.locator('.d-cite').first.check()
        page.select_option('#d-dec', 'include')
        page.fill('#d-reason', 'Live check: RSVP concealed-information task with EyeLink pupil recordings; article read in Orchard.')
        page.fill('#d-rev', 'Live check')
        page.click('[data-d=decide]')
        expect(page.locator('#d-decision')).to_contain_text(re.compile(r'History \(\d+ versions?\)'))
        go('repository-funnel')
        with page.expect_download() as dl:
            page.click('a.button:has-text("Download export ZIP")')
        z = zipfile.ZipFile(io.BytesIO(Path(dl.value.path()).read_bytes()))
        prisma = z.read('repository-prisma.txt').decode()
        log('export files:', len(z.namelist()), '| decision in CSV:', 'Live check' in z.read('project_decisions.csv').decode())
        for line in prisma.splitlines():
            if re.search(r'Records examined|Unique repository|Inspected|With .edf|EyeLink headers|Publication status|Article text|Human decisions|Reports|Independent', line):
                log('  ' + line.strip()[:160])
        log('JS errors:', errors or 'none')
        b.close()
finally:
    server.shutdown(); server.server_close(); s.close()
