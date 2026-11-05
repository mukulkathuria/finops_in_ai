# Progress

Resume rule: read this file, the latest ADRs and open findings, then continue the current phase.
Workflow that works in this environment: write files through a scratchpad bundle + unbundler,
run the local gate script (ruff, format, mypy --strict, import contracts, bandit, route audit,
pytest against PG19), rehearse migrations up/down/up, run the RLS audit, gitleaks-gated push,
then check the GitHub Actions run (all four jobs, including the image scan).

## Done

| Item | Commit | Evidence |
| --- | --- | --- |
| Phase 0 foundations | `90a3839`..`dee23fd` | compose stack healthy, CI green, RLS and route audits 0 violations |
| US-01 signup, Argon2id, EdDSA JWT, JWKS | `c325aec` | `tests/integration/test_us01_signup.py`, `tests/unit/test_tokens.py` |
| US-28 append-only audit log, per-org hash chain | `f3c8372` | `tests/integration/test_us28_audit.py` (tamper, delete, truncate detected) |
| US-02 teams, single-use expiring invitations | `267f11d` | `tests/integration/test_us02_teams_invitations.py` (concurrent accept: one winner) |
| US-03 login, rotating refresh, family revocation, lockout, per-IP limits | `0c0a3b3` | `tests/integration/test_us03_sessions.py` |
| US-04 team API keys (hash only, instant revocation), data-plane Caller, /v1/whoami | `b5e594a` | `tests/integration/test_us04_api_keys.py` |
| US-05 OpenAI-compatible endpoint | `a3f7145`, `af21c2e` | `tests/contract/test_llm_contract.py` (53), `tests/integration/test_us05_openai_compat.py` (official OpenAI SDK) |
| US-06 org provider keys (envelope encryption), adapter switch | `dc17580` | `tests/integration/test_us06_provider_keys.py`, `tests/unit/test_envelope.py` |
| US-07 org and team budgets, reserve then commit, 429 naming the scope, one soft alert | `0c41fea`, `de42f0d` | `tests/integration/test_us07_budgets.py`, `tests/unit/test_budget_rules.py`, `tests/unit/test_pricing.py` |
| US-08 member limits under the team cap, strict allocation, budget requests | `e57992e` | `tests/integration/test_us08_member_limits.py` |
| US-09 own usage and remaining budget equal to the ledger | `663f03c` | `tests/integration/test_us09_own_usage.py` |
| US-10 zero overshoot under 500 parallel requests, Idempotency-Key, janitor | `15fd84a` | `tests/integration/test_us10_concurrency.py`, `tests/unit/test_idempotency.py` |
| US-29 liveness/readiness, Prometheus metrics, one trace per request with stage spans | `51565ac` | `tests/integration/test_us29_observability.py`, `tests/unit/test_metrics_guard.py` |
| US-13 an employee asking for another person's salary gets nothing from retrieval and the model never sees the row (direct, indirect, multi-turn, other language, encoded and injection prompts; every provider request recorded) | `feat(US-13)` | `tests/integration/test_us13_retrieval_acl.py`, `tests/unit/test_retrieval.py` |
| US-15 text PDFs and scans become Markdown with tokens before and after; low-confidence pages keep the original. US-16 OCR_BACKEND switches GLM-OCR and PaddleOCR-VL by config, backend stored per artifact | `feat(US-15, US-16)` | `tests/integration/test_us15_us16_parsing.py`, `tests/unit/test_parsing.py`, `sidecars/docparse/tests/test_docparse.py` (CI job `docparse`) |
| US-14 uploads validated from their bytes: type mismatch, oversize, page limit, zip bombs, malware test file, encrypted and corrupt PDFs, path names refused with a reason | `feat(US-14)` | `tests/unit/test_files.py`, `tests/integration/test_us14_files.py` |
| US-11 PII and secrets blocked or redacted per policy, logged without values; US-12 injection corpus blocked, benign corpus under 2% false positives | `feat(US-11, US-12)` | `tests/integration/test_us11_us12_guardrails.py`, `tests/unit/test_detectors.py`, `tests/unit/test_output_filter.py`, `tests/contract/test_classifier_contract.py` |
| US-20 Jev is optional: with `JEV_API_KEY` set and `CLASSIFIER_BACKEND=jev` decisions use it; without the key the gateway refuses to start with a clear error; Laya stays the default | `feat(US-11, US-12)` | `tests/contract/test_classifier_contract.py::test_jev_without_a_key_fails_closed_at_startup`, `tests/integration/test_us11_us12_guardrails.py` |
| US-17 every request records its baseline (the input as sent, at the org's baseline model, in full) and overhead (OCR, classifier calls, embeddings); the savings report by team, model, day and technique matches the ledger; dashboard Savings page | `feat(US-17)` | `tests/unit/test_savings.py`, `tests/integration/test_us17_savings.py`, `web/src/__tests__/format.test.ts` |
| US-18 exact and semantic response cache: an identical request hits, a similar single-turn question above the threshold hits, another org or permission scope never does (the key covers scope, parameters, system prompt, conversation and guardrail policy); hits charge nothing and count as savings | `feat(US-18)` | `tests/unit/test_cache.py` (hypothesis: keys differ across scopes), `tests/integration/test_us18_cache.py` |
| US-19 complexity router as a stage (off, shadow, on after a passed eval): easy prompts for the large tier go to the small tier of the same provider when it can take them; sampled requests are answered by the other tier too and the agreement is recorded; the routing report compares it with the tolerance; a team can opt out | `feat(US-19)` | `tests/unit/test_routing.py`, `tests/integration/test_us19_routing.py` |
| Provider prompt caching (Phase 3): Anthropic requests carry cache breakpoints (system prompt or tools; the newest turn of a conversation), on by default per org; reads and writes are priced and reported as the `provider_cache` technique | `feat(phase-3)` | `tests/contract/test_llm_contract.py`, `tests/unit/test_provider_cache.py`, `tests/integration/test_provider_cache.py` |
| Price sync job (Phase 3): the worker keeps `model_catalog` in step with `config/prices.json` or an https URL; changes are appended (at once or at a later `effective_from`), suspicious ones refused; one sync at a time across workers | `feat(phase-3)` | `tests/unit/test_price_sync.py`, `tests/integration/test_price_sync.py` |
| US-06 (switch part) and Phase 3: the optional LiteLLM adapter (pinned extra, OpenAI-compatible providers) reuses the native mapping on the shared HTTP client and passes the whole contract suite; the switch test answers the same requests through both adapters with the same answers, usage and cost; unservable selections fail at startup; filelock raised past its CVEs | `feat(US-06)` | `tests/contract/test_llm_contract.py` (`litellm` harness), `tests/integration/test_litellm_switch.py`, `tests/unit/test_envelope.py` |
| US-26 a stage registers by config (registry order, per-org mode), runs in shadow on a copy, reports to `stage_reports` with the ledger row, and goes `on` only after a passed eval run for its current version; a failing stage is skipped and reported | `feat(US-26)` | `tests/unit/test_stages.py`, `tests/integration/test_us26_stages.py` |
| US-21 RAG with relevance filtering: the `rag_relevance` stage (off, shadow, on after a passed eval) reranks the candidates, drops passages under the org's threshold and packs the rest within its token budget; pruned tokens in the response, the ledger and the savings report; a failing reranker keeps the vector order | `feat(US-21)` | `tests/unit/test_relevance.py`, `tests/integration/test_us21_rag_relevance.py` |
| US-22 (indexing and context packs) code projects shared like documents; Python indexer (`ast`) and Graphify adapter into `graph_vertex`/`graph_edge` with one context pack per module; `GraphStore` reads (find, neighbours, reachability) check the ACL in the same statement; `GET /v1/projects/{id}/context` serves the packs of the modules a question names within a token budget; chat requests take `code_context` (packs and the signatures one call away, screened like retrieved passages, reported as the `code_context` stage). Eval on this repository: 12 of 12 questions hit, context 19,952 tokens against 91,500 for the whole modules (21.8%). MCP tools at `POST /v1/mcp` (`get_code_context`, `search_documents`; protocol 2026-07-28 with header checks, and the 2025 `initialize` handshake) | `feat(US-22)` | `tests/unit/test_code_index.py`, `tests/unit/test_code_eval.py`, `tests/unit/test_graph.py`, `tests/integration/test_us22_code_context.py`, `tests/integration/test_us22_graph_store.py`, `tests/unit/test_mcp.py`, `tests/integration/test_us22_mcp.py`, `scripts/eval_code_context.py` |
| Phase 4 review loop: Python parsed in an isolated probe process (deadline, memory cap, two at a time, files over 512,000 characters left out); frame neutralizing linear; text reaching the database one clean line; `code_context` cost-neutral in the ledger; packs cut at a line end, never dropped for size; whole names found first; walks of 2-3 hops from at most 20 vertices; project names unique per owner and edge indexes covering `project_id` (migration 0023); MCP tools screened like chat and not cached; reranker scores held to 0-1; seeded latency benchmark (`bench` workflow) | `fix(phase-4)` | `docs/audits/2026-11-05-phase4-review.md`, `tests/unit/test_code_probe.py`, `tests/load/bench_context.py`, `tests/integration/test_bench_context.py` |
| Phase 3 review loop: hold released on a disconnect during the cache lookup; eval gate on the latest run with demotion; cached answers sealed and the worker limited to cleanup (janitor with expiry and per-org cap); cache key on the inspecting policy and rules version; price-sync errors without URLs, unpriced new models refused; LiteLLM without `.env` and imported at startup, streams closed; quality checks charged to the org; compaction leaves tool output and tabs alone; savings by technique reconcile for cache hits and unserved requests | `fix(phase-3)` | `docs/audits/2026-11-04-phase3-review.md`, `tests/integration/test_phase3_review.py`, `tests/unit/test_phase3_review.py` |
| Phase 2 review loop: per-credential and heavy-work limits, budget precheck before conversion and retrieval, blocked passages dropped, exact search behind HNSW, linear detectors, input size cap | `c8740df`, `4ce7d06`, `201a9a2` | `docs/audits/2026-11-04-phase2-review.md`, `tests/integration/test_phase2_review.py`, `tests/unit/test_relay.py` |

Budgets in one paragraph: one SQL statement holds the worst case (strict prompt-token upper bound,
tier-safe price bound) on every applicable budget with an ordered upsert, so `committed + reserved`
never passes a limit and locks are always taken in budget-id order. Settlement swaps the hold for
the actual cost and appends the ledger row in one transaction; failures release the hold; streams
reserve and open before the first byte. Periods are SQL functions in the org timezone, and the
ledger row carries the request clock, so counters and the ledger always agree. The janitor
(`python -m worker`, compose service `worker`) returns holds of crashed requests, rebuilds the
current counters from the ledger and purges day-old idempotency keys. Verified end to end on the
running compose stack (bootstrap signup, budget, idempotent replay, own usage, 429).

Observability: `/metrics` (per-app registry, route-template labels; bearer `METRICS_TOKEN`, dev/test
only without it), OpenTelemetry server span per request continuing W3C `traceparent`, child spans
`auth`, `budget.reserve`, GenAI client span `chat <model>` (usage, cost), `budget.settle`; the
trace id is returned as `x-trace-id` and logged; OTLP/HTTP export via `OTEL_EXPORTER_OTLP_ENDPOINT`.

Phase 1 review loop (spec section 12): two independent reviewers, every finding fixed with a
regression test and re-reviewed; no open Critical or High finding. See
`docs/audits/2026-11-03-phase1-review.md`.

Phase 1 also delivered: the basic dashboard (`web/`, `67109b9`; verified in a real browser) and
the transactional outbox with signed alert webhooks (`tests/integration/test_outbox_webhooks.py`).

Guardrails in one paragraph: a `Classifier` port with three backends (`rules`, `laya`, `jev`;
Laya and Jev share one SystemOne client). Input checks run after the budget hold: PII and
secrets by pattern plus checksum on the text as written, or on a cleaned form when cleaning
reveals more (zero-width characters, NFKC, base64); injection by rules first, then the
classifier over overlapping windows. Rules always win; a classifier failure follows the org's
policy (closed by default). Answers are filtered too; streams are released only where no value
can be cut in two, tool-call arguments included. Detection is linear in the text and the number
of findings, and inputs over 64 KiB are scanned off the event loop.

OCR evaluation: `scripts/ocr_eval.py` scores both backends (CER, WER, confidence, time) by language
and document type and names the backend to set ([docs/ocr-evaluation.md](docs/ocr-evaluation.md)).
The report itself needs the GPU-served models and your document set (none are available here).

Phase 2 review loop (spec section 12): the same two-reviewer loop over US-11 to US-16; every High
and Medium finding fixed with a regression test, re-reviewed twice; no open Critical, High or
Medium finding. See `docs/audits/2026-11-04-phase2-review.md` (accepted Lows: D-21).

Phase 3 review loop (spec section 12): two independent reviewers (security; correctness and
money); no Critical or High finding and no cross-tenant path; every Medium and most Lows fixed
with a regression test. See `docs/audits/2026-11-04-phase3-review.md` (accepted Lows: D-24).

## Next: Phase 4 (US-21, US-22: context engineering)

Plan: `docs/superpowers/plans/2026-11-04-phase4-context.md`. Phase 3's plan:
`docs/superpowers/plans/2026-11-04-phase3-savings.md`.

## Remaining stories

Phase 4: the seeded latency run at full scale (the `bench` workflow) and the review loop's re-review;
Phase 5 US-24, US-25; Phase 6 US-27, US-30.

## Findings and deviations from the spec

| Id | Finding | Resolution |
| --- | --- | --- |
| D-1 | SQL/PGQ is not in PostgreSQL 19 (present in 19beta1, reverted by beta4; absent in RC1) | ADR 0004: GraphStore with joins and recursive CTEs |
| D-2 | Fourth database role `worker` for cross-tenant jobs | ADR 0002 |
| D-3 | `httpx2` instead of `httpx` (maintained continuation; OpenAI 3.x, MCP 2.x, anthropic 2.x use it) | ADR 0001 |
| D-4 | `control-plane/` is `control_plane/` (Python package naming) | ADR 0001 |
| D-5 | Anthropic API 2026-10-21 rejects temperature/top_p/top_k | Anthropic adapter drops them (capability `sampling_params=False`) |
| D-6 | CI image job was red from `a3f7145` to `15fd84a` (tests, lint and SAST green): Trivy's secret scanner matched docstrings in the vendored Anthropic SDK that quote `"type": "service_account"` | `trivy-secret.yaml`: allow rule scoped to `site-packages/anthropic/` only; vulnerability scanning unchanged |
| D-7 | A budget created mid-period counts the spending its scope already made in that period (seeded from the ledger) | Keeps counters equal to the janitor's ledger rebuild at all times |
| D-8 | Soft-threshold alerts are recorded (`budget_alerts`), counted in `finops_budget_soft_alerts_total` and delivered through the transactional outbox to a signed org webhook; e-mail delivery is not built | Resolved by the outbox (migration 0012); chat tools (Slack, Teams) take the webhook directly |
| D-9 | Unused budget never rolls over (the spec makes rollover opt-in per org) | Not offered yet; the default behaviour is implemented |
| D-10 | Budget reservations key on a gateway-generated hold id, never the client-supplied `X-Request-ID` | Duplicate client request ids cannot collide or settle each other's holds |
| D-11 | An attempt that fails or is cancelled after partial output is billed for it and frees its Idempotency-Key, so a retry with that key runs (and is billed) again; only a completed request replays without a charge | The provider billed the partial attempt; replaying a truncated answer would be worse |
| D-12 | When the org timezone changes, current counters are rebuilt from the ledger for the new periods; a one-time grant stays with the period it was granted in | `docs/audits/2026-11-03-phase1-review.md` (M5) |
| D-13 | Members of several teams must send `X-FinOps-Team` on model calls (422 `team_required`); team and member budgets key on the team | Review finding C1 |
| D-14 | The `rules` backend uses native patterns with checksums (Luhn, IBAN mod-97, Verhoeff, SSN rules) instead of Presidio | Presidio's recognizers need spaCy language models (hundreds of MB) in the gateway image; the seeded PII and secrets are covered at lower latency. A Presidio adapter fits behind the same port |
| D-15 | Streamed answers are released at line or sentence ends (in long runs, at a separator 64 characters back), never inside a value; redaction placeholders are not mapped back to the original values in answers | `tests/unit/test_output_filter.py`; a value split across two output chunks can never leak |
| D-16 | For untrusted text over 100,000 characters the classifier sees overlapping windows, at most 8 (the first 4 and the last 4); the rules read all of it | Bounds classifier cost and latency on very long prompts |
| D-17 | Originals are stored by the local-disk `ObjectStore` (a volume, or a shared mount for several replicas); the S3-compatible adapter is not built yet. TIFF is not accepted; Office page limits apply to slides only | The port is in place; an S3 adapter needs a test target (Garage or SeaweedFS in the dev stack) |
| D-18 | The OCR sidecar recognises whole pages with each model's task prompt (GLM-OCR "Text Recognition:", PaddleOCR-VL "OCR:") through the vLLM OpenAI-compatible API, with confidence from token log-probabilities; the vendors' full pipelines add a layout model first | A layout-aware pipeline can replace the sidecar behind the same contract. Reload: switching OCR_BACKEND takes a restart (no reload endpoint) |
| D-19 | Kept pages are attached as images, which only vision-capable models accept; tokens before are estimates (1,600 per PDF page, the image formula for pictures). OCR time is recorded per artifact (`gpu_seconds`) but not yet charged to budgets | Savings reports (Phase 3) subtract the conversion cost |
| D-20 | The chunks column is `vector(1024)` (bge-m3): an embedder of another width needs a migration; one HNSW index serves all orgs (per-org partitions come with the load tests); chat retrieval queries with the last user message and has no reranker or relevance threshold until US-21 | Every row records its embedding model, and vectors of different models are never compared |
| D-22 | The savings report reads the ledger and the stage reports directly (both partitioned by month and indexed by org and time); the `savings_daily` rollup is added with the load tests if the report needs it. Overhead rates are configured, not measured; embeddings made while indexing documents are not part of any request's overhead | Totals come from the ledger itself, so the report always reconciles with it |
| D-21 | Phase 2 accepted Lows: detection costs about two seconds of CPU per million characters below the input cap; images, tool definitions and message names are not scanned; passages are screened by the rules only; search returns unredacted text to the readers the ACL allows; limits are per credential; the chunk quota is unlocked; documents indexed from a deleted file stay until deleted; a key glued to a letter is not detected | `docs/audits/2026-11-04-phase2-review.md` (each item with its reason and upgrade path) |
| D-24 | Phase 3 accepted Lows: eval runs are attested by an org admin (the gate records a decision); the price guard compares each sync with the current row only; the filelock override sits outside LiteLLM's declared range; cached answers (sealed, at most 30 days) have no per-user erasure; an org baseline model creates savings no technique explains; question embeddings stay unsealed for the semantic lookup; the per-org cache cap is soft and cleanup needs the worker | `docs/audits/2026-11-04-phase3-review.md` (each item with its reason) |
| D-23 | The router scores complexity by rules (length, code or tools, reasoning words, depth); the optional cascade (small first, escalate on low confidence) is not built. The judge measures how far the small tier's answer agrees with the large tier's (embedding cosine), not whether it is correct: it catches drift, not a fluent wrong fact. An LLM grader can replace `Verifier.agreement` | The router is a stage, so shadow mode, the eval gate and per-technique savings come from US-26 without new machinery |
| D-25 | The code indexer reads Python with the standard library's `ast`, standing in for tree-sitter (other languages come through the Graphify adapter). A question finds modules by the identifiers it names (the last part of a dotted name, CamelCase and snake_case split), not by embeddings | A tree-sitter adapter fits behind the same `Module` outline; packs are plain text, so an embedding lookup can be added beside the term index |
| D-26 | The MCP endpoint answers each request with one JSON response: no SSE streams, no sessions, no server-to-client requests. Versions 2026-07-28, 2025-11-25 and 2025-06-18 are served; 2025-03-26 clients (no version header) are not. Requests carrying an `Origin` header are refused, as no CORS origin is configured | The tools are request and response; streams and sessions add nothing to them |
| D-27 | Phase 4 accepted Lows: questions find code by the identifiers they name (short and very common names are not terms); an error answer to an unreadable request id carries no `id`; the one-hop signature lookup reads after the context's transaction; the reranker URL and answer size are operator configuration; whole-graph re-indexes leave dead rows for autovacuum; a team key's project stays with its team | `docs/audits/2026-11-05-phase4-review.md` (each item with its reason) |

## Blocked

Nothing blocked. Real provider keys, GPUs for OCR sidecars and cloud credentials are not available
here; those adapters are verified with contract suites against recorded fixtures.
