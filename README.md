# Orchard

A local literature-search workspace for the `eyeris` project: import search terms, review exact queries, retrieve scholarly records, consolidate duplicates, screen papers, inspect data links, and export an auditable research record.

**Version 0.1 is a working local foundation, not a completed implementation of every feature in the original specification.** It has one provider adapter (Europe PMC). Live API access could not be verified in the build environment. No real papers or fake demonstration results are preloaded. The supplied download includes the user's original terms document and five unapproved query drafts in its local database; these are excluded from GitHub by `.gitignore`.

## Start on your computer

Requires **Python 3.10 or later**. No third-party Python or JavaScript packages are required.

1. Extract the downloaded ZIP.
2. Open Terminal and change into the extracted `orchard` folder.
3. Run:

```bash
python3 -m orchard
```

4. Open **http://127.0.0.1:8765** in your browser.
5. Leave Terminal running. Press **Control-C** there to stop the app.

On Windows, use `py -3 -m orchard` if `python3` is unavailable.

A second port or a separate workspace can be selected explicitly:

```bash
python3 -m orchard --port 8766 --data-dir ./my-research-data
```

There is no build or install step. Run commands from the directory containing this README. The app binds only to `127.0.0.1`; it is not a hosted website and has no multi-user authentication. Run only one app process per database.

## First research session

1. **Project & criteria:** enter the research question in your own words. Configure inclusion and exclusion criteria before screening. They are not inferred from example search restrictions.
2. **Search planner:** select the imported document, inspect its original wording and proposed concept groups. If starting from a clean Git clone, import the search terms `.txt` file through this page.
3. Choose terms from up to five groups, then generate a draft. OR joins terms within groups; AND joins the groups. You can also enter an exact Europe PMC query manually.
4. Review the exact query. Use **Approve as a new version**, then **Run search**. Default retrieval is capped at 300 records per attempt. No queries are run merely by importing a document or approving it.
5. **Search history:** inspect progress. A cap or failure is recorded as partial/failed, never complete. **Resume** continues an incomplete run; **Run fresh search** creates a new run. Reuse does not rerun a search.
6. **Paper library:** open a paper. Inspect its source records and discovery history. Screen title/abstract and full text separately. Record an exclusion reason when excluding.
7. **Duplicate review:** inspect similar titles. Confirm a duplicate, keep distinct reports, or link related versions. Merge history and both records' screening decisions are retained.
8. **Full text & data:** retrieve PMC text where available or paste authorized plain text. Candidate links retain supporting passages. A reachable URL is distinct from a researcher-verified dataset. Record licenses and reprocessing feasibility in the Eyeris assessment fields.
9. **EyeLink finder:** on a paper, choose **Find EyeLink files** (optionally paste a PDF URL), or queue many papers from the **EyeLink finder** page. Orchard downloads the PDF, extracts its text and embedded links, finds data-repository links, lists those repositories, and reports `.edf`/`.asc` files. See [EyeLink finder](#eyelink-finder).
10. **Audit & export:** download a ZIP containing per-table CSVs, complete JSON, and a readable search-strategy report.

When offline, you can still import documents, plan queries, import actual Europe PMC `core` JSON responses, screen records, and export. The record import accepts either `{"resultList":{"result":[...]}}` or a record array. Each record requires `source` and `id`. Missing metadata remains null. Importing a provider file does not claim that Orchard performed a live search.

## What is implemented

- [x] Original text and SHA-256 preservation; line-based provenance for extracted term occurrences.
- [x] Rule-based concept suggestions; conservative exact normalization; reproducible query generation.
- [x] Immutable query versions; explicit approval; repeat-search detection, reuse, and fresh runs.
- [x] Europe PMC `core` metadata, cursor pagination, bounded retries, and background search jobs.
- [x] SQLite persistence, interruption recovery, source snapshots, per-run discoveries, and append-only audit events.
- [x] Exact DOI/provider-ID consolidation; ambiguous-title review; related-report links and history-preserving merges.
- [x] Editable canonical display metadata with source values and correction history retained.
- [x] Separate title/abstract and full-text screening, versioned criteria, exclusions and concurrent-edit protection.
- [x] PMC full-text retrieval, authorized plain-text import, contextual dataset/code/supplement candidate extraction, and explicit URL checks.
- [x] `eyeris` reprocessing-feasibility assessment history.
- [x] EyeLink finder: PDF download, repository-link detection, repository file listings, remote ZIP directories and `.edf`/`.asc` header checks.
- [x] Filtered, paginated paper library; resource inventory; complete CSV/JSON/history export.
- [x] Automated workflow tests and a GitHub Actions test configuration.

## EyeLink finder

For each scanned paper Orchard:

1. **Downloads the PDF.** It tries, in order: a PDF URL you supply, Unpaywall (only if `ORCHARD_UNPAYWALL_EMAIL` is set), PDF links in the Europe PMC record, and the Europe PMC PDF render. The PDF is saved in `data/pdfs/` and its text plus embedded link annotations are stored as full text. A login, cookie or bot-check page is recorded as a failed attempt, not a PDF. If a PMCID exists, PMC XML full text is also retrieved.
2. **Finds repository links** in the PDF, full text, abstract and stored resources: OSF (including `view_only` links and OSF DOIs), Zenodo, Figshare, Dryad, Dataverse, OpenNeuro and GitHub. Links in a data-availability context are checked first, up to 15 per paper. GIN, Mendeley Data and GitLab links are detected and marked *unsupported* for manual checking.
3. **Lists each repository through its public API** (OSF recursively, including components and add-on storage) and flags `.edf`, `.asc` (also `.gz`) files.
4. **Looks inside ZIP archives** without downloading them, by reading only the ZIP directory with HTTP byte ranges.
5. **Reads the first bytes** of a few candidates per repository. **EyeLink header ✓** means the file starts with SR Research's `SR_RESEARCH` header (EDF) or EDF2ASC header lines (ASC). `.edf` is also the European Data Format used for EEG/PSG; those files are labelled *EEG/PSG EDF, not EyeLink*.

Each scan stores a verdict: `eyelink_confirmed`, `eyelink_candidates`, `archives_unchecked`, `none_found`, `no_repository_links`, or `incomplete` (a repository failed or a limit was hit). Every step, link, the evidence passage that produced it, and every candidate file are kept and exported. A scan is capped at 400 requests and 15 minutes; each repository at 150 requests and 4 minutes.

Optional environment variables: `ORCHARD_UNPAYWALL_EMAIL` (enables Unpaywall PDF lookup) and `ORCHARD_GITHUB_TOKEN` or `GITHUB_TOKEN` (raises GitHub's 60 requests/hour anonymous limit). Installing `pypdf` (`pip install pypdf`) improves PDF text extraction; without it a built-in reader handles common PDFs and always reads clickable link annotations.

The same check works without the app, on a PDF file, PDF URL, or repository link:

```bash
python3 -m orchard.eyelink paper.pdf
python3 -m orchard.eyelink https://osf.io/abcde/
```

## Data and privacy

The default database is **`data/orchard.sqlite3`**, relative to this project. It contains the imported document, extracted terms, query versions, raw provider records, full text, decisions and audit events. It survives restarts. The SQLite WAL/SHM files may exist while the app runs.

Exports are downloaded to the location your browser chooses. They include the complete research workspace, including original documents and full text; review them before sharing. Back up the entire `data` folder **after stopping the app**. Do not copy only the main SQLite file while writes are in progress.

`.gitignore` excludes `data/*`, exported files, databases, `.env`, caches, and credentials. `.env.example` is documentation only; the app does not automatically load it. No API keys are required for the implemented Europe PMC adapter.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Optional JavaScript syntax check (requires Node.js):

```bash
node --check orchard/static/app.js
```

Optional live API probe, which does not change the project:

```bash
python3 scripts/live_smoke.py
```

See [validation.md](docs/validation.md) for actual executed results and outstanding verification. Synthetic records exist only in isolated tests, never in the supplied live database.

## Structure

```text
orchard/
  __main__.py       CLI entry
  server.py         local HTTP API and static-file serving
  db.py             schema, transactions and audit helpers
  terms.py          document extraction and query construction
  providers.py      Europe PMC, retries and guarded HTTP requests
  service.py        search, ingestion, screening and exports
  eyelink.py        PDF reading, repository listings and EyeLink file detection
  static/           HTML, CSS and browser JavaScript
scripts/            source import and optional live API check
tests/              isolated synthetic workflow tests
docs/               architecture, API, limitations and validation
data/               local research database (ignored by Git)
.github/workflows/  automated test configuration
```
