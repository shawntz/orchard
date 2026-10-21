# Known limits and next steps

This is a usable local foundation. It does not meet every acceptance criterion of the full original specification. The following are explicit unfinished areas, not hidden completion claims.

| Area | Current state / limitation | Next step |
|---|---|---|
| Live scholarly results | Europe PMC adapter implemented and fixture-tested; real request blocked in build environment | Run `scripts/live_smoke.py` and a small approved search from the user's network |
| Browser validation | HTTP routes and JS syntax verified; no browser executable available for visual/interaction QA | Exercise desktop/mobile views in a real browser; add Playwright E2E tests |
| Provider coverage | Europe PMC only; no direct PubMed, OpenAlex, Crossref, Semantic Scholar, CORE, Unpaywall or repository API | Add provider-specific adapters and cross-provider identity tests |
| Input formats | UTF-8 text/Markdown and Europe PMC core JSON | Add validated PDF/DOCX/CSV/RIS/BibTeX imports |
| Extraction | Deterministic list/heading heuristics, no semantic model; unparsed text remains visible | Validate candidate groups and add broader parsers with provenance |
| Source terms | Original term occurrences immutable; selection/manual query editing supported; no concept-renaming editor | Add explicit term/concept revision UI |
| AI screening | Not implemented; no model configured | Optional evidence-cited recommendations, distinct from human decisions |
| Criteria | Versioned inclusion/exclusion text blocks, not individually identified criteria | Add structured criteria IDs and per-criterion decision evidence |
| Study mapping / PRISMA | Report records and actions available; study count unknown; no complete flow diagram | Model studies/reports and reconcile stage counts before rendering |
| Dataset discovery | Contextual URLs from retrieved text/metadata; no independent repository searches | Add explicit repository inference, license checks and raw-data validation |
| Resource metadata | Evidence and access status captured; no structured license/format/download manifest | Add those fields and maintain unknown vs verified states |
| Full text | Europe PMC XML or user-supplied authorized plain text; no PDF parsing or all-publisher retrieval | Expand legal retrieval routes and robust full-text extraction |
| XML | Entity/DOCTYPE declarations rejected; some valid article XML may therefore require manual import | Use hardened XML parsing if dependency availability permits |
| Link checks | Public URL and redirect checks; an access failure is not proof of a broken link | Distinguish provider outages, restrictions, HTTP errors and transient failures more granularly |
| Metadata | Rich fields preserved in raw JSON; canonical display exposes a subset | Normalize affiliations, retractions, controlled vocabulary and journal details |
| Large datasets | Paper list paginated; some inventories/export and duplicate-title scan load full sets | Add indexed duplicate candidate search, paginated inventories and streaming exports |
| Multiple users | Single local user; stale screening protected; no reviewer accounts/adjudication | Add authentication and reviewer-specific workflows only if needed |
| Merge correction | History retained but no unmerge UI | Add explicit reversible merge events |
| Database recovery | Resume interrupted searches; manual full-db backup; no JSON restore import | Add validated backup/restore and migration tooling |
| Query equivalence | Exact query fingerprints, no Boolean-expression equivalence parser | Safely canonicalize only well-understood syntax |
| Deployment | Local Python server, no hosted website or container deployment | Evaluate hosting separately with authentication and operational hardening |
| GitHub | Source archive and push commands provided; no repository write access | User pushes a new branch and reviews a PR |

No raw pupil datasets have been downloaded or reprocessed with eyeris. No reported findings have yet been rechecked. Those are downstream research activities, outside this search platform foundation.
