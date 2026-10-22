# Orchard

A local literature-search workspace for the `eyeris` project: import search terms, review exact queries, retrieve scholarly records, consolidate duplicates, screen papers, inspect data links, and export an auditable research record.

**Version 0.2 is a working local foundation, not a completed implementation of every feature in the original specification.** It searches Europe PMC, PubMed, OpenAlex, Semantic Scholar and CORE, consolidates their records into one paper per work, and looks for full text across all of them plus Unpaywall, Crossref and arXiv. No real papers or fake demonstration results are preloaded. The supplied download includes the user's original terms document and five unapproved query drafts in its local database; these are excluded from GitHub by `.gitignore`.

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
3. Choose terms from up to five groups, then generate a draft. OR joins terms within groups; AND joins the groups. You can also enter an exact query manually in Europe PMC Boolean syntax (quoted phrases, AND/OR/NOT, parentheses). Orchard translates it for each source; field tags such as `TITLE:` only run on Europe PMC.
4. Review the exact query. Use **Approve as a new version**, tick the sources to search, then **Run search**. Each source becomes its own run; records sharing a DOI, PMID, PMCID or arXiv ID become one paper that keeps every source's record. Default retrieval is capped at 300 records per attempt per source. No queries are run merely by importing a document or approving it.
5. **Search history:** inspect progress. A cap or failure is recorded as partial/failed, never complete. **Resume** continues an incomplete run; **Run fresh search** creates a new run. Reuse does not rerun a search.
6. **Paper library:** open a paper. Inspect its source records and discovery history. Screen title/abstract and full text separately. Record an exclusion reason when excluding.
7. **Duplicate review:** inspect similar titles. Confirm a duplicate, keep distinct reports, or link related versions. Merge history and both records' screening decisions are retained.
8. **Full text & data:** **Find full text (all sources)** tries, in order: Europe PMC XML, PDF links already known from search metadata, PMCID/arXiv PDF routes, then live lookups by identifier (Europe PMC, Unpaywall, OpenAlex, Semantic Scholar, CORE, Crossref). It stops at the first source that returns XML or a real PDF; every attempt is audited. Retrieved PDFs are stored and downloadable from the paper page. You can also paste authorized plain text. Candidate links retain supporting passages. A reachable URL is distinct from a researcher-verified dataset. Record licenses and reprocessing feasibility in the Eyeris assessment fields.
9. **Audit & export:** download a ZIP containing per-table CSVs, complete JSON, and a readable search-strategy report.

When offline, you can still import documents, plan queries, import actual Europe PMC `core` JSON responses, screen records, and export. The record import accepts either `{"resultList":{"result":[...]}}` or a record array. Each record requires `source` and `id`. Missing metadata remains null. Importing a provider file does not claim that Orchard performed a live search.

## What is implemented

- [x] Original text and SHA-256 preservation; line-based provenance for extracted term occurrences.
- [x] Rule-based concept suggestions; conservative exact normalization; reproducible query generation.
- [x] Immutable query versions; explicit approval; repeat-search detection, reuse, and fresh runs.
- [x] Europe PMC, PubMed, OpenAlex, Semantic Scholar and CORE search adapters with pagination, bounded retries, per-source rate limits, and background search jobs.
- [x] Cross-source consolidation by DOI/PMID/PMCID/arXiv ID; missing metadata filled from whichever source has it.
- [x] Full-text fallback chain across sources (XML, then PDFs located via Unpaywall, OpenAlex, Semantic Scholar, CORE, Crossref, arXiv), with PDF storage and best-effort text extraction.
- [x] SQLite persistence, interruption recovery, source snapshots, per-run discoveries, and append-only audit events.
- [x] Exact DOI/provider-ID consolidation; ambiguous-title review; related-report links and history-preserving merges.
- [x] Editable canonical display metadata with source values and correction history retained.
- [x] Separate title/abstract and full-text screening, versioned criteria, exclusions and concurrent-edit protection.
- [x] PMC full-text retrieval, authorized plain-text import, contextual dataset/code/supplement candidate extraction, and explicit URL checks.
- [x] `eyeris` reprocessing-feasibility assessment history.
- [x] Filtered, paginated paper library; resource inventory; complete CSV/JSON/history export.
- [x] Automated workflow tests and a GitHub Actions test configuration.

## Data and privacy

The default database is **`data/orchard.sqlite3`**, relative to this project. It contains the imported document, extracted terms, query versions, raw provider records, full text, decisions and audit events. It survives restarts. The SQLite WAL/SHM files may exist while the app runs.

Exports are downloaded to the location your browser chooses. They include the complete research workspace, including original documents and full text; review them before sharing. Back up the entire `data` folder **after stopping the app**. Do not copy only the main SQLite file while writes are in progress.

Retrieved PDFs are stored in **`data/files/`**, named by SHA-256. Exports list them in `files.csv` but do not include the PDFs themselves.

`.gitignore` excludes `data/*`, exported files, databases, `.env`, caches, and credentials. `.env.example` is documentation only; the app does not automatically load it. Set variables in your shell before starting:

| Variable | Effect |
|---|---|
| `ORCHARD_CONTACT_EMAIL` | Enables Unpaywall; sent to OpenAlex, Crossref and NCBI as a courtesy contact |
| `ORCHARD_CORE_API_KEY` | Enables CORE search and lookup (free key) |
| `ORCHARD_OPENALEX_API_KEY` | Private OpenAlex budget; deep paging beyond 10,000 results |
| `ORCHARD_S2_API_KEY` | Avoids Semantic Scholar's frequently rate-limited shared pool |
| `ORCHARD_NCBI_API_KEY` | Faster PubMed requests |

Europe PMC, PubMed, OpenAlex, Semantic Scholar and Crossref work without any of these.

PDF text extraction uses [`pypdf`](https://pypi.org/project/pypdf/) if it is installed (`pip install pypdf`). Without it, a standard-library fallback reads only simple PDFs; publisher PDFs are still stored and downloadable, just without extracted text.

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
  providers.py      search/lookup adapters, query translation, retries, guarded HTTP
  pdftext.py        best-effort PDF text extraction
  service.py        search, ingestion, screening and exports
  static/           HTML, CSS and browser JavaScript
scripts/            source import and optional live API check
tests/              isolated synthetic workflow tests
docs/               architecture, API, limitations and validation
data/               local research database (ignored by Git)
.github/workflows/  automated test configuration
```
