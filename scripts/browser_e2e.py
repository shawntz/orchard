"""Drive Orchard in a real browser: search → project → documentation → confirmed paper → reader → decision → export.

Fixture mode (default) serves recorded-shape API responses from tests/repository_fixtures.py, so the
run is deterministic and offline. ``--live`` uses the real OSF, Crossref, OpenAlex and Europe PMC
APIs for the regression project (OSF ctd34) and a public Zenodo dataset with a linked article.

Requires Playwright with Chromium:  pip install playwright && python -m playwright install chromium
Usage:  python scripts/browser_e2e.py [--live] [--screenshots DIR] [--headed]
"""
import argparse
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from orchard import discovery as D, repositories as R  # noqa: E402
from orchard.server import make_server  # noqa: E402
from orchard.service import Service  # noqa: E402

LIVE_PHRASE = 'Revealing the hidden spatial distribution of visual attention'
LIVE_ZENODO_PHRASE = 'pupil EyeLink'


def build_service(tmp, live):
    if live:
        return Service(Path(tmp) / 'orchard.sqlite3'), None
    from test_repository_discovery import make_service
    for key in ('ORCHARD_GITHUB_TOKEN', 'GITHUB_TOKEN', 'ORCHARD_ZENODO_TOKEN', 'ORCHARD_DRYAD_TOKEN', 'ORCHARD_CONTACT_EMAIL'):
        os.environ.pop(key, None)
    return make_service(tmp)


class Run:
    def __init__(self, page, base, shots):
        self.page, self.base, self.shots, self.steps, self.dialog_answers = page, base, shots, [], []
        page.on('dialog', self.on_dialog)

    def on_dialog(self, dialog):
        answer = self.dialog_answers.pop(0) if self.dialog_answers else ''
        dialog.accept(answer) if dialog.type == 'prompt' else dialog.accept()

    def step(self, name):
        self.steps.append(name)
        print(f'[{len(self.steps):02d}] {name}', flush=True)
        if self.shots:
            self.page.screenshot(path=str(Path(self.shots) / f'{len(self.steps):02d}-{re.sub(r"[^a-z0-9]+", "-", name.lower())[:50]}.png'), full_page=True)

    def go(self, route):
        # Reload so a same-hash navigation still renders fresh data.
        self.page.goto(self.base + '/#' + route)
        self.page.reload()
        self.page.wait_for_load_state('networkidle')

    def toast_ok(self):
        toast = self.page.locator('#toast')
        if toast.is_visible() and 'error' in (toast.get_attribute('class') or ''):
            raise AssertionError('Error toast: ' + toast.inner_text())


def wait_for(predicate, timeout=600, interval=1.0, what='condition'):
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = predicate()
        except (TypeError, KeyError):
            value = None  # the record does not exist yet
        if value:
            return value
        time.sleep(interval)
    raise AssertionError('Timed out waiting for ' + what)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--screenshots')
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright, expect
    if args.screenshots:
        Path(args.screenshots).mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='orchard-e2e-')
    s, web = build_service(tmp, args.live)
    server = make_server(s, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    print('Orchard at', base, 'data in', tmp, '(live APIs)' if args.live else '(fixture APIs)')
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={'width': 1500, 'height': 1000}, accept_downloads=True)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            r = Run(page, base, args.screenshots)

            # 1. Eligibility criteria (required before include/exclude decisions).
            r.go('settings')
            page.fill('#criteria-in', 'Human participants performing a psychological or cognitive task with pupil size recorded by an EyeLink tracker.')
            page.fill('#criteria-out', 'Non-human participants; no pupil measurement; clinical or light-reflex-only studies.')
            page.click('[data-act=save-criteria]')
            page.wait_for_timeout(500)
            r.step('Criteria saved')

            # 2. Repository search.
            r.go('repository-sweep')
            expect(page.locator('h1')).to_contain_text('Find EyeLink data')
            page.fill('#d-reviewer', 'E2E reviewer')
            if args.live:
                for box in page.locator('.d-source').all():
                    if box.get_attribute('value') not in ('osf', 'zenodo'):
                        box.uncheck()
                page.uncheck('.d-ext[value=edf]')
                page.fill('#d-phrases', LIVE_PHRASE + '\n' + LIVE_ZENODO_PHRASE)
                page.fill('#d-pages', '1')
                page.fill('#d-max', '0')
            else:
                page.fill('#d-phrases', 'pupillometry')
            page.click('[data-d=start]')
            wait_for(lambda: s.db.one("SELECT status FROM discovery_jobs ORDER BY created DESC LIMIT 1")['status'] in ('completed', 'partial', 'failed', 'cancelled'), what='search job')
            r.go('repository-sweep')
            job = page.locator('.d-job').first
            expect(job).to_contain_text('records examined')
            job.locator('summary').click()
            expect(job).to_contain_text('GitHub code search requires a token')
            r.step('Search finished with per-source coverage')
            records = s.db.one('SELECT COUNT(*) n FROM discovery_observations')['n']
            assert records > 0 and f'{records} records examined' in job.inner_text(), 'records examined shown from stored observations'
            expect(job).not_to_contain_text('running')

            # 3. The regression project: inspect (live) or open (fixture).
            ctd = wait_for(lambda: s.db.one("SELECT * FROM sweep_projects WHERE repository='osf' AND external_id='ctd34'"), 30, what='ctd34 project')
            r.go('project/' + ctd['id'])
            if args.live:
                page.click('[data-d=inspect]')
                page.wait_for_selector('text=Inspection log', timeout=600_000)
            expect(page.locator('a.button:has-text("Open on OSF")')).to_have_attribute('href', 'https://osf.io/ctd34/')
            files = page.locator('#d-files')
            expect(files).to_contain_text('Recording in an exclusion folder')
            expect(files).to_contain_text('Not_included')
            expect(files).to_contain_text('eyelink')
            r.step('Project record with file inventory and excluded participants kept separate')

            # 4. Documentation in the embedded reader.
            page.locator('#d-left button[data-d=read-doc]').first.click()
            expect(page.locator('#d-reader-body pre')).to_contain_text('attention', timeout=15_000)
            r.step('README read inside Orchard')
            pub = page.locator('#d-publication')
            expect(pub).not_to_contain_text('This is the publication')
            expect(pub).to_contain_text('No publication candidate')
            page.locator('#d-publication summary:has-text("Search record")').click()
            expect(pub).to_contain_text('Rejected search results')
            page.locator('#d-publication summary:has-text("No publication identified")').click()
            page.fill('#d-none-reason', 'Checked README, OSF wiki, Crossref, OpenAlex and Europe PMC searches (E2E).')
            page.fill('#d-rev', 'E2E reviewer')
            page.click('[data-d=none]')
            expect(pub).to_contain_text('No publication identified (reviewed)')
            expect(page.locator('#d-left')).to_contain_text('Likely relevant')
            r.step('No article invented; no-publication decision recorded')

            # 5. Decision with cited evidence, then a second version.
            page.locator('.d-cite').first.check()
            page.select_option('#d-dec', 'include')
            page.fill('#d-reason', 'README describes two visual attention experiments with EyeLink pupil recordings (E2E).')
            page.click('[data-d=decide]')
            expect(page.locator('#d-decision')).to_contain_text('History (1 version)')
            page.select_option('#d-dec', 'needs_review')
            page.fill('#d-reason', 'Second look: confirm luminance manipulation details with the authors (E2E).')
            page.click('[data-d=decide]')
            expect(page.locator('#d-decision')).to_contain_text('History (2 versions)')
            page.locator('#d-decision summary:has-text("History")').click()
            expect(page.locator('#d-decision')).to_contain_text('E2E reviewer')
            r.step('Decision saved and edited as versioned history')

            # 6. Download selected recordings with size, destination, verification and manifest.
            if not args.live:
                page.locator('table.d-files[data-role=recording] .d-file').first.check()
                page.locator('details.d-group:has(table[data-role=behavioral]) > summary').click()
                page.locator('table.d-files[data-role=behavioral] .d-file').first.check()
                page.click('[data-d=plan-download]')
                dialog = page.locator('#dialog')
                expect(dialog).to_contain_text('downloads/osf_ctd34')
                r.step('Download plan shows sizes and destination')
                page.click('[data-d=start-download]')
                wait_for(lambda: s.db.one("SELECT status FROM download_batches ORDER BY created DESC LIMIT 1")['status'] in ('completed', 'partial'), 60, 0.5, 'download')
                r.go('project/' + ctd['id'])
                expect(page.locator('#d-files')).to_contain_text('verified')
                expect(page.locator('#d-files')).to_contain_text('orchard-manifest.json')
                r.step('Downloads verified with manifest')

            # 7. A project with an explicitly linked article: confirm, retrieve, read in Orchard.
            if args.live:
                zen = wait_for(lambda: s.db.one("SELECT p.* FROM sweep_projects p WHERE repository='zenodo' ORDER BY rowid LIMIT 1"), 30, what='Zenodo project')
            else:
                zen = s.db.one("SELECT * FROM sweep_projects WHERE repository='zenodo' AND external_id='1234567'")
            r.go('project/' + zen['id'])
            if args.live:
                page.click('[data-d=inspect]')
                page.wait_for_selector('text=Inspection log', timeout=600_000)
            button = page.locator('[data-d=confirm]').first
            if button.count():
                r.dialog_answers.append('Repository records this article as isSupplementTo (E2E).')
                button.click()
                expect(page.locator('#d-publication')).to_contain_text('Publication confirmed')
                r.step('Publication confirmed and linked to the Paper library')
                page.locator('[data-d=retrieve]').first.click()
                # Orchard's CSP forbids eval, so wait with a locator instead of wait_for_function.
                expect(page.locator('#d-publication')).to_contain_text(re.compile('Read PDF here|Retrieval attempts'), timeout=180_000)
                pdf = page.locator('#d-reader-body iframe.d-pdf')
                if pdf.count():
                    src = pdf.get_attribute('src').split('#')[0]
                    resp = page.request.get(base + src)
                    assert resp.headers['content-type'] == 'application/pdf' and resp.body()[:5] == b'%PDF-', 'reader serves the PDF'
                    r.step('Article PDF embedded in the reader')
                    text_button = page.locator('[data-d=read-text]').first
                    if text_button.count():
                        text_button.click()
                        expect(page.locator('#d-reader-body pre')).not_to_be_empty()
                        r.step('Extracted article text shown beside the screening controls')
                else:
                    expect(page.locator('#d-publication')).to_contain_text('Retrieval attempts')
                    r.step('Article not retrievable: attempts listed, nothing stored as text')
                paper_link = page.locator('#d-publication .d-paper a[href^="#paper/"]').first.get_attribute('href')
                r.go(paper_link[1:])
                expect(page.locator('main')).to_contain_text('Repository projects')
                r.step('Paper library record links back to the repository project')
            else:
                r.step('No explicitly linked article on this Zenodo record (live data); confirmation flow covered in fixture mode')

            # 8. Funnel and export.
            r.go('repository-funnel')
            expect(page.locator('main')).to_contain_text('Records examined')
            expect(page.locator('main')).to_contain_text('Independent studies')
            r.step('Funnel with distinct project, report and study counts')
            with page.expect_download() as dl:
                page.click('a.button:has-text("Download export ZIP")')
            data = Path(dl.value.path()).read_bytes()
            z = zipfile.ZipFile(io.BytesIO(data))
            names = set(z.namelist())
            for needed in ('repository-prisma.txt', 'project_decisions.csv', 'sweep_files.csv', 'discovery_tasks.csv', 'publication_decisions.csv'):
                assert needed in names, needed
            decisions = z.read('project_decisions.csv').decode()
            assert 'E2E reviewer' in decisions and decisions.count('E2E') >= 2
            print(z.read('repository-prisma.txt').decode()[:1500])
            r.step('Export downloaded and checked')
            assert not errors, 'JavaScript errors: ' + '; '.join(errors)
            browser.close()
        print('\nBROWSER E2E PASSED:', len(r.steps), 'steps', '(live)' if args.live else '(fixtures)')
    finally:
        server.shutdown()
        server.server_close()
        s.close()


if __name__ == '__main__':
    main()
