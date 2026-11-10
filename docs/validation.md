# Validation performed for 0.2 (multi-source search and full text)

- **63 automated tests passed** (`python -m unittest discover -s tests -v`), with and without `pypdf` installed. New tests cover per-provider runs and batches, cross-source DOI and PMID consolidation, metadata filling, provider selection and strict reuse, unconfigured providers, fielded-query rejection, isolated provider failure, resume for non-Europe-PMC runs, the full-text fallback order (XML → known PDFs → identifier routes → locators), HTML-instead-of-PDF detection, lookup-discovered PMCIDs, failure auditing, unreadable PDFs, PDF download over HTTP, schema 1→2 migration, each normalizer, OpenAlex/Semantic Scholar paging and Semantic Scholar query translation.
- **Live providers (2026-10-22):** `scripts/live_smoke.py` succeeded for Europe PMC, PubMed, OpenAlex and Semantic Scholar search, and for Europe PMC, OpenAlex, Semantic Scholar and Crossref lookup. CORE and Unpaywall were not exercised (no key/email configured).
- **Live end-to-end:** one query (`pupillometry AND "working memory"`, cap 25 per source) across four sources stored 100 source records as 79 papers; 18 papers were found by two or more sources. Full-text retrieval succeeded via Europe PMC XML for a PMC paper and via OpenAlex-located PDFs (bioRxiv, Research Square) for papers Europe PMC could not supply. Two papers had no retrievable open copy, and every attempt was recorded. `pypdf` extracted readable text from both PDFs; the standard-library fallback correctly declined them.
- The 0.1 notes below are retained for history.

# Validation performed for 0.1

## Passing checks

- **40 automated tests passed**, executed using `python -m unittest discover -s tests -v` in an isolated temporary database for each test. No external API was required.
- Python compilation passed for application and utility scripts.
- `node --check orchard/static/app.js` passed.
- Started the actual local HTTP application and received **HTTP 200** from `/api/overview`.
- Eight main JavaScript page templates rendered against the actual local HTTP API in a Node VM with a minimal DOM stub: Overview, Search planner, Search history, Paper library, Duplicate review, Data & code, Project & criteria, and Audit & export. **This is a template/API smoke check, not a browser or visual test.**
- Imported the entire supplied source document: **42 proposed groups and 357 original term occurrences**, with all original text retained. These are heuristic candidate extractions, not 357 independently validated search terms. Screening guidance and unparsed lines remain in the original document for review.
- Saved **five unapproved drafts** generated from the source's actual Measurement and cognitive-domain groups. No draft was automatically approved or executed.
- Confirmed the delivered local database contains **zero papers, zero search runs, zero screening decisions**, and no synthetic test fixtures.

## Test coverage

Document validation, original wording/hash preservation, exact-duplicate normalization, overlapping concepts, reproducible query provenance, approval gating, prior-search reuse, explicit refresh, quoted query spacing, missing-prior reuse rejection, failed and partial requests, pagination, retrieval caps and cursor continuation, identifier and DOI consolidation, independent discovery histories, malformed records and their retained evidence, ambiguous duplicate review, related preprints, history-preserving merges, idempotent ingestion, missing metadata, criteria versions, stale criteria and screening rejection, exclusion reasons, inaccessible full text, contextual multi-link extraction, data-on-request statements, unverified-link states, URL-access failures, CSV/JSON export completeness, empty-table headers, CSV formula neutralization, database restart recovery, immutable audit/query history, real dashboard counts, metadata correction provenance, feasibility-assessment revisions, HTTP operations and cross-origin rejection, malformed provider responses, and local/private URL rejection.

## Blocked or not executed

- **Live Europe PMC search:** a direct network probe returned a 403 policy response; the adapter smoke request failed DNS resolution (`Temporary failure in name resolution`). Therefore, live success and a real-results demonstration are **not verified**. Run `python3 scripts/live_smoke.py` from the user's network, then run a small approved query in the UI.
- **Real full-text and link verification:** depend on external access and were not validated against live resources. Failure behavior and evidence extraction were tested with fixtures.
- **Real browser/visual testing:** Playwright was available, but its Chromium executable was missing. Launch failed before a page could be opened. Responsive layout, accessibility interactions, downloads, dialogs and end-to-end browser behavior need testing on the user's computer.
- **GitHub:** no push, pull request, or GitHub Actions run has occurred. The original ZIP contained only a README; the user can push the supplied source to a new branch.
- **Other providers:** (0.1 only; superseded in 0.2) no OpenAlex/Crossref/Semantic Scholar integration existed.

See `limitations.md` for the remaining requirements. Passing these checks does not establish that the full original V1 acceptance checklist has been satisfied.
