# Architecture and implementation record

## Scope and inputs

Inspected the supplied `orchard-main.zip`: only `orchard-main/README.md`, containing `# orchard` and a trailing blank line. Read the implementation specification and the complete supplied literature-search brainstorm. No Slack, Canvas, eyeris original paper, or external repository contents beyond that ZIP were accessed. Source-paper citations and seed candidates in the brainstorm remain unverified.

The research input includes pupil measurement terms, 18 cognitive-domain sections, example Boolean searches, repository queries, screening/feasibility guidance, and unverified seed references. The entire document is preserved locally. Its sample humans-only/NOT/open-data filters are not applied automatically. Five draft queries use the document's actual Measurement group and working-memory, episodic-memory, sustained-attention, emotion, and workload groups. Research question and final eligibility criteria remain unset for researcher input.

## Components

The local HTTP server serves a static JavaScript application and validated operation routes. `Service` coordinates the workflow; `Database` handles short-lived transaction-bound connections. SQLite uses foreign keys, WAL, and busy timeouts. A two-thread worker pool keeps searches off the HTTP request thread. Network requests have timeouts, bounded retries, rate limiting, and response-size limits. Host and Origin checks restrict the API to local browser use. Requests are JSON only; HTML output is escaped. This is not an authenticated hosted service.

Search providers (`EuropePMC`, `PubMed`, `OpenAlex`, `SemanticScholar`, `Core`) implement `name`, `version`, `configured` and `page(query, cursor, size, callback)`, returning a `Page` of raw records, an opaque cursor and the provider's reported total. Each has a normalizer in `NORMALIZERS` mapping raw records to a shared shape (title, abstract, authors, year, DOI, PMID, PMCID, arXiv ID, full-text links). Queries are stored in Europe PMC Boolean syntax; fielded terms are rejected for other providers rather than silently reinterpreted, and Semantic Scholar receives a `+`/`|`/`-` translation. Crossref is lookup-only because its relevance ranking ignores Boolean operators, so a hit count would be meaningless.

A search creates one run per selected provider, sharing a `batch_id`. Runs keep independent cursors, totals, failures and resume. Ingestion stores each raw record under its provider and matches papers through the shared `identifiers` table (`doi`, `pmid`, `pmcid`, `arxiv`, plus each provider's own ID), so a work found by several sources becomes one paper with several source records. Missing display fields are filled from later sources.

Full-text retrieval is an ordered fallback: Europe PMC JATS XML when a PMCID is known; PDF links already stored from search metadata; PMCID and arXiv PDF routes; then locators (`locate(ids)`) queried one at a time — Europe PMC, Unpaywall, OpenAlex, Semantic Scholar, CORE, Crossref. Locators can also return identifiers (e.g. a PMCID), which unlocks the XML route. A download counts only if it starts with `%PDF-`; HTML landing or sign-in pages are recorded as `not_pdf`. The first success wins. Every attempt is written to the audit log, and links found along the way are saved as resources whether or not retrieval succeeds.

The technical default requested FastAPI/React. The original repository had no established stack, and installing FastAPI failed in the build environment. A standard-library backend and static frontend provide a runnable, dependency-free foundation. A future FastAPI migration can reuse the workflow service and database, with a versioned migration plan.

## Provenance and persistence

Documents retain content/hash. Concepts and original term occurrences retain document membership and source lines. Query records are immutable; edits and approvals create new snapshots. Search fingerprints include provider, exact query, adapter version, retrieval limit and result type. Quoted whitespace and Boolean order are not changed; this deliberately favors missed reuse over falsely declaring two searches equivalent.

Provider source records are content-hashed immutable snapshots. Discovery rows link each snapshot to each run. Exact DOI or reliable provider identifiers select canonical records; conflicting identifiers and similar titles produce review candidates. Missing display values can be filled from later sources. Corrections retain raw values and audit the former display values. Ambiguous matches are never merged automatically.

Human-confirmed merges mark an alias without deleting its source records or screening history. Recursive family reads expose inherited records. Related reports remain separate. A merge is not reversible through the V1 UI; restore a stopped-app database backup if a correction is necessary.

Criteria are immutable document-level versions. Screening records are new events with a stage, decision, reason, reviewer and criteria-version ID. Paper revision numbers reject stale screening writes. The fulltext-inaccessible state is distinct from exclusion. Feasibility assessments are also historical snapshots.

Resource evidence and origins distinguish provider metadata from abstract or full-text extraction. No repository-search inference is currently performed. URL reachability alone does not verify a public dataset, license, or paper association. A researcher can record confirmation or rejection with a note.

## EyeLink finder

`orchard/eyelink.py` is dependency-free and uses the guarded `Transport` (public hosts only, redirects re-checked, rate limited). A scan is a background job like a search: `eyelink_scans` holds status, verdict, PDF provenance and an ordered step log; `repository_checks` holds each repository link with its evidence passage and listing status; `repository_files` holds `.edf`/`.asc` candidates and archives with their header classification. Repository links are also added to `resources`. Startup marks unfinished scans interrupted. Schema version 2 adds these tables additively.

## Failure recovery

Every retrieved record is committed independently. A cursor is advanced only after the current page has been ingested. Replaying a partially stored page is idempotent. Failure retains previous results, records the error, and produces failed/partial status. Retrieval caps are partial status; resume continues by another capped batch. Startup marks formerly queued/running jobs interrupted. A fresh search creates a separate run, preserving prior runs. Manual imports are labeled as imports, never as live searches.

## Schema and future migration

Schema version 2 adds the EyeLink finder tables; version 3 adds `runs.batch_id` and the `files` table. Initialization creates missing tables, adds the column to an older database, and preserves existing rows. Europe PMC search fingerprints are unchanged, so prior runs can still be reused. Future releases must check this version and apply explicit transactional, additive migrations, backing up the stopped database beforehand; destructive automatic changes are out of scope. Audit triggers reject event updates/deletes. Query-update and screening-update triggers protect histories, in addition to the append-only application paths.

## Delivery work log

1. Inspected archive, environment, source text and existing dependency availability.
2. Built normalized SQLite storage, term import, draft query generation and immutable approvals.
3. Built provider transport, ingestion, fingerprints, pagination and restart recovery.
4. Added duplicate review, resource extraction, manual screening and audit/export operations.
5. Added the browser workspace, Eyeris feasibility assessment and setup documentation.
6. Executed unit/integration checks, JavaScript syntax checks and real-network probes; fixed issues found.
7. Packaged source plus a local database containing only the supplied source document and unapproved drafts.
8. Supplied a GitHub branch/push workflow; no external push was attempted after the access limitation was established.

## Primary provider references

- Europe PMC REST overview: https://europepmc.org/RestfulWebService
- Europe PMC developers: https://europepmc.org/developers
- Search syntax: https://europepmc.org/help

The implementation requests `format=json`, `resultType=core`, `cursorMark`, and bounded `pageSize`. Search results and metadata must not be represented as complete coverage of all relevant literature.
