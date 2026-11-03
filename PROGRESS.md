# Progress

Resume rule: read this file, the latest ADRs and open findings, then continue the current phase.
Workflow that works in this environment: write files through a scratchpad bundle + unbundler,
run the local gate script (ruff, format, mypy --strict, import contracts, bandit, route audit,
pytest against PG19), rehearse migrations up/down/up, run the RLS audit, gitleaks-gated push.

## Done

| Item | Commit | Evidence |
| --- | --- | --- |
| Phase 0 foundations | `90a3839`..`dee23fd` | compose stack healthy, CI green, RLS and route audits 0 violations |
| US-01 signup, Argon2id, EdDSA JWT, JWKS | `c325aec` | `tests/integration/test_us01_signup.py`, `tests/unit/test_tokens.py` |
| US-28 append-only audit log, per-org hash chain | `f3c8372` | `tests/integration/test_us28_audit.py` (tamper, delete, truncate detected) |
| US-02 teams, single-use expiring invitations | `267f11d` | `tests/integration/test_us02_teams_invitations.py` (concurrent accept: one winner) |
| US-03 login, rotating refresh, family revocation, lockout, per-IP limits | `0c0a3b3` | `tests/integration/test_us03_sessions.py` |
| US-04 team API keys (hash only, instant revocation), data-plane Caller, /v1/whoami | `b5e594a` | `tests/integration/test_us04_api_keys.py` |

## In progress: US-05 OpenAI-compatible endpoint

Done so far (uncommitted until the contract suite lands):
- `gateway/domain/llm.py` canonical types; `Usage` invariants (input includes cached and
  cache-write subsets, output includes reasoning; never negative).
- `gateway/ports/llm.py` `LLMAdapter` port; `ProviderErrorKind` adds `bad_request` and
  `unavailable` to the spec's six kinds so retry and HTTP mapping stay exact.
- Adapters: `openai_compat` (httpx2), `anthropic` (official SDK 2.4.0, API version 2026-10-21:
  temperature/top_p are never sent), `gemini` (REST v1beta; thought signatures carried in tool-call
  ids as `call_<hex>.gts.<b64url>`).

Next, in order:
1. Contract suite `tests/contract/test_llm_contract.py` with recorded fixtures per adapter
   (text, stream, tool call, streamed tool call, cached/reasoning usage, 429 with Retry-After, 401,
   5xx/529, context overflow, content filter, timeout, malformed stream, embeddings).
2. Migration 0007: `model_catalog` (global, prices micro-USD per 1M tokens, effective_from,
   catalog_version = row id) seeded with mock models and Anthropic models (prices from the
   claude-api skill, cached 2026-10-06); `usage_events` partitioned ledger.
3. `config/gateway.yaml` providers (mock, openai, anthropic, gemini; api_key_env) and a provider
   registry; `create_app(settings, http_client=...)` seam for tests (ASGI mock provider).
4. `/v1/chat/completions` (JSON and SSE), `/v1/embeddings` (float and base64), `/v1/models`;
   cost headers; ledger write; retries with jitter on retryable kinds; ingress body limits.
5. Official `openai` SDK compatibility tests through an httpx2 ASGI transport.

## Remaining stories

US-06 provider keys (envelope encryption) and adapter switch (incl. optional LiteLLM);
US-07..US-10 budgets (reserve/commit, hierarchy, ledger views, 500-way race, idempotency);
US-29 metrics and tracing; Phase 2 US-11..US-16; Phase 3 US-17..US-20, US-26; Phase 4 US-21,
US-22; Phase 5 US-24, US-25; Phase 6 US-27, US-30; worker; web dashboard.

## Findings and deviations from the spec

| Id | Finding | Resolution |
| --- | --- | --- |
| D-1 | SQL/PGQ is not in PostgreSQL 19 (present in 19beta1, reverted by beta4; absent in RC1) | ADR 0004: GraphStore with joins and recursive CTEs |
| D-2 | Fourth database role `worker` for cross-tenant jobs | ADR 0002 |
| D-3 | `httpx2` instead of `httpx` (maintained continuation; OpenAI 3.x, MCP 2.x, anthropic 2.x use it) | ADR 0001 |
| D-4 | `control-plane/` is `control_plane/` (Python package naming) | ADR 0001 |
| D-5 | Anthropic API 2026-10-21 rejects temperature/top_p/top_k | Anthropic adapter drops them (capability `sampling_params=False`) |

## Blocked

Nothing blocked. Real provider keys, GPUs for OCR sidecars and cloud credentials are not available
here; those adapters are verified with contract suites against recorded fixtures.
