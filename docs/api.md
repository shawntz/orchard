# Local API reference

Base URL: `http://127.0.0.1:8765/api`. Local Host/Origin checks apply; no CORS access. POST bodies must be JSON objects, at most 12 MB. Errors use `{"error":"…"}`: 400 validation, 404 missing record, 409 conflict. The application is local and single-user, not an authenticated public API.

| Method / route | Body or query | Result |
|---|---|---|
| GET `/overview` | — | Project, current criteria, actual DB counts and recent runs |
| POST `/project` | `name`, `question` | Save project wording with audit history |
| GET `/documents` | — | Imported document metadata |
| POST `/documents` | `name`, `text` | Import ID or exact-content reuse |
| GET `/documents/{id}` | — | Original text, concepts, terms, source lines and unparsed lines |
| GET `/queries` | — | All immutable versions |
| POST `/queries` | `label`, `query`, optional `term_ids`, `document_id`, `parent_id`, `approved`, `rationale` | New query version |
| POST `/generate` | `term_groups` (array of term-ID arrays), `document_id`, `label` | Unapproved draft |
| POST `/search` | `query_id`, `action` (`new`, `reuse`, `refresh`), `max_records` (1–5000) | Run snapshot, `reused` flag |
| GET `/runs` | — | Search/import histories |
| POST `/resume` | `id` | Queue incomplete provider run from its checkpoint |
| POST `/records` | `name`, `records` (core response or array) | Explicit manual-import run |
| GET `/papers` | `q`, `page`, `status`, `year`, `source`, `resource_kind` | `items`, `total`, `page`, `pages`; 25/page |
| GET `/papers/{id}` | — | Citation, raw sources, discoveries, resources, decisions, assessments |
| POST `/metadata` | `id`, `fields` (`title`, `abstract`, `authors`, `year`, `publication_type`) | Corrected display record; raw sources unchanged |
| POST `/criteria` | `inclusion`, `exclusion` | New criteria-version ID |
| POST `/screen` | `id`, `stage`, `decision`, `reason`, `notes`, `reviewer`, `revision`, optional `criteria_id` | Updated paper; stale version yields 409 |
| POST `/assess` | `id`, `fields` | New Eyeris feasibility assessment |
| GET `/duplicates` | — | Candidate pairs with comparison metadata |
| POST `/duplicates` | `id`, `action` (`merge`, `distinct`, `related`), `reason` | Resolution; canonical ID |
| POST `/fulltext` | `id`, `text`, `origin` | Store authorized text and extract candidate resources |
| POST `/fetch-fulltext` | `id` | Attempt PMC XML retrieval; failures logged |
| POST `/fetch-pdf` | `id` | Retrieve an open-access PDF (PMC article dataset, then provider-listed PDF links); `{pdf, reused}`; failures logged |
| POST `/papers/{id}/pdf` | Raw PDF body, `Content-Type: application/pdf`, ≤60 MB; query `name` | Store an uploaded PDF; `{pdf, reused}` |
| GET `/pdfs/{id}` | — | Stored PDF, served inline for the reader |
| GET `/resources` | — | Resource inventory with evidence and status |
| POST `/resource` | `id`, `action` (`verify`, `confirm`, `reject`), `note` | Resource status; confirm/reject require notes |
| GET `/audit` | `page`, `q` | Newest-first events, 100/page |
| GET `/export` | optional `format=json` | ZIP (CSV + JSON + report), or JSON |

Title/abstract decisions: `include`, `exclude`, `uncertain`, `needs_information`.
Full-text decisions: `include`, `exclude`, `uncertain`, `awaiting_fulltext`, `inaccessible`.
`include` at title/abstract means advance to full-text review, not final study inclusion.

Feasibility keys: `target_finding`, `dataset_identity`, `data_level`, `timing_metadata`, `acquisition`, `original_workflow`, `confounds`, `reuse_feasibility`.

POST operations that return no additional payload respond `{"ok":true}`. Exports include complete raw source material; audit export creation is recorded.
