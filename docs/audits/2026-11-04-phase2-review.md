# Phase 2 review loop (spec section 12)

Scope: US-11 to US-16 (guardrails, files, parsing, retrieval) at commit `303a90a`, reviewed by
two independent reviewers with fresh context. One reviewed security: tenancy, ACLs, files and
guardrails. The other reviewed correctness, concurrency and performance. The author fixed every
High and Medium finding with a regression test that fails on the old code (`c8740df`, `4ce7d06`).
Both reviewers then re-reviewed the fixes. Two new Medium findings came from the fixes themselves
and were fixed in `201a9a2`, which both reviewers checked again. Result: no open Critical, High
or Medium finding. The accepted Lows are listed below and recorded in PROGRESS as D-21.

## Findings and resolution

S marks the security review, C the correctness and performance review, R the re-review.

| Id | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| C-H1 | High | Secret and e-mail patterns rescanned a run from every position (each "eyJ" of `eyJ-eyJ-...`, each letter before an "@"): seconds of CPU per megabyte with the GIL held, stalling every tenant | Key patterns start where a word starts; JWTs are found from their ".eyJ" and addresses from their "@" with bounded work per anchor. Every pattern is now linear: hostile runs cost 0.7 to 1.9 times ordinary text, best of three (single runs up to about 4.7 times on the reviewer's machine for dense IP addresses and PEM headers); `test_hostile_runs_cost_what_prose_costs` covers 25 shapes and the input cap bounds the total |
| C-M1 | Medium | A usage-only chunk released text the output guardrail was holding back, so a key split across events leaked on servers that send usage with every event | Only a finish ends the content (`tests/unit/test_relay.py`) |
| C-M2, S-M2 | Medium | No rate limit or quota on uploads, ingests, conversions and searches; embeddings unbudgeted | `RATE_LIMIT_PER_MINUTE` (every data-plane request) and `HEAVY_RATE_LIMIT_PER_MINUTE` (uploads, ingests, Markdown conversions) per API key or user; `MAX_INDEXED_CHUNKS_PER_ORG` per org |
| C-M3 | Medium | Attachments were converted and the question embedded before the budget hold; kept page images were inlined without a cap | A budget precheck (the worst-case hold, taken and rolled back) runs first; at most 20 MiB of page images per request |
| C-M4 | Medium | Indexing a scan silently embedded the "[page N is attached as an image]" stand-ins | Kept pages are left out and named in `warnings`; a document with nothing left is refused (`empty_document`) |
| C-M5, S-M3 | Medium | One tenant could open the shared parser circuit breaker for everyone (a page PDFium could not load answered 500; a 413 or an oversized page image counted as a sidecar failure); a huge MediaBox rendered without a pixel cap | A page PDFium cannot load is a 422; renders are capped at about 4 megapixels and 6 MiB; 413 and over-cap images are the document's fault and leave the breaker closed |
| C-M6 | Medium | The HNSW scan stops after `hnsw.max_scan_tuples`, so a caller who may read few of the org's chunks could get fewer than k hits | When the index scan returns fewer than k, an exact search over the readable chunks (through the ACL index) runs (`test_the_exact_search_finds_what_the_index_scan_gives_up_on`) |
| C-M7, S-L4 | Medium | Deleting a file left its Markdown and kept page images behind | The last copy of a content takes its artifacts and page images with it (migration 0016) |
| S-M1 | Medium | FastAPI parsed an upload's multipart body before authentication, so anyone could make the gateway spool up to `MAX_UPLOAD_BYTES` | The form is read inside the route, after authentication, with one file at most |
| S-M4 | Medium | The PDF probe process inherited the gateway's environment (database URL, keys) | It starts with an empty environment (SYSTEMROOT only on Windows) |
| S-M5 | Medium | A shared document holding an injection phrase, or a value the policy blocks, blocked every chat that retrieved it | Such passages are dropped from the prompt and recorded in the ledger (`retrieval.dropped`); the request goes on |
| R-N1 | Medium | The H1 fix (an anchor at the start of a token) let a "-" in front hide a key, and text glued in front hide an address | Word-start semantics for keys again; JWTs and addresses found from their anchors, so glued text hides neither (`test_a_jwt_is_found_with_anything_glued_in_front`, `test_an_address_behind_glued_text_is_still_found`) |
| R-R1 | Medium | The input checks still cost about two seconds of CPU per million characters, so a 10 MB request burns about 20 s | `GUARDRAIL_MAX_INPUT_CHARS` (2,000,000) caps the text read per request; more is refused with 413 `input_too_large` before any scan |
| S-L1, R-R4 | Low | Case, spacing, small or fullwidth brackets and invisible characters could close or forge the frames around documents and passages | The opening bracket of any such tag is escaped |
| S-L5 | Low | Narrow secret coverage | Hugging Face, SendGrid, npm, Twilio and Azure Storage keys added |
| C-L | Low | `chunk_text` copied the rest of an overlong sentence at every cut | Cut by offset |
| C-L | Low | Graceful shutdown waited for parses without a limit | 30 seconds |
| C-L | Low | Embedder answers were not checked one to one against the inputs | Indices must be exactly 0 to n-1 |
| C-L | Low | A cancelled upload released its PDF probe slot while the probe kept running | The probe keeps its slot until its process ends |
| C-L, R-R5 | Low | A stream that failed while the guardrail held text left that text out of the estimate, and was not charged if nothing had been released | Held text counts toward the estimate and makes the stream chargeable |

## Accepted risks (Low), recorded as D-21

- Detection costs about two seconds of CPU per million characters, below the cap. Each pattern
  pass holds the GIL, so a large prompt adds some latency to other requests in the same process.
  The upgrade path is a process or interpreter pool for very large inputs.
- Images (kept pages and uploaded pictures) are not scanned for PII, secrets or injections.
  Message names, tool-call ids and tool definitions are not scanned either. System and assistant
  roles are not injection-checked, by design.
- Passages are screened one at a time and by the rules only. A passage that only the classifier
  flags, or an injection split across passages, is caught by the check of the whole request, which
  blocks that request.
- `/v1/retrieval/search` returns chunk text unredacted to the readers the ACL allows, and ingestion
  does not scan. Chat retrieval does pass through the guardrails. The question is embedded
  unredacted, which is acceptable while the embedder is self-hosted.
- Rate limits count per credential, so an org with many keys has a larger allowance; an org-level
  bucket can be added. The counter is one upsert per request on a hot row per credential.
- The index quota counts the org's chunks on every ingest, without a lock, so concurrent ingests can
  pass the limit together by their own sizes.
- The exact-search fallback has no statement timeout. Its cost is bounded by the chunks the caller
  may read, which is small whenever the index scan comes back short.
- Documents indexed from a file stay searchable after the file is deleted. They are separate
  objects, deleted with `DELETE /v1/documents/{id}`. An idempotent replay can return an earlier
  answer for the same key within the replay window after access changes.
- Two parses of the same document can race at the single-flight cleanup; `ON CONFLICT` keeps one
  artifact. A cancellation inside a parse or an upload can leave an object without a row; an
  orphan is preferred to deleting a committed object.
- Embeddings are bounded by the limits but not charged to budgets (the embedder is self-hosted).
  A 400 from the parser sidecar still counts against the breaker, because it means a gateway
  misconfiguration.
- Document listings show each visible document's access groups (team and user ids) and file ids.
- A key glued to a letter (`Xsk-...`) is not detected, as with the original `\b` patterns:
  matching inside words would flag ordinary hyphenated text, so a model told to prefix a key with a
  letter can still slip it past the output filter. An address with more than 64 characters glued
  in front keeps the leading ones visible (the last 64 and the domain are redacted). A private key
  body over 16 KiB in streamed output is released in part after the hold cap. There is no generic
  `password=` rule, because it causes too many false positives.
