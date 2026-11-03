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

## Done in this session after the checkpoint

| US-05 OpenAI-compatible endpoint | adapters `a3f7145`, endpoints next commit | `tests/contract/test_llm_contract.py` (53), `tests/integration/test_us05_openai_compat.py` (official OpenAI SDK: chat, stream, base64 embeddings) |

US-05 details: canonical types; LLMAdapter port; openai_compat / anthropic (SDK 2.4.0, API 2026-10-21,
no sampling params) / gemini (thought signatures in tool-call ids) adapters; model_catalog (prices as
data, tiers, worst_case bound with rounding slack) and partitioned usage_events ledger (migration
0007); config/gateway.yaml providers keyed by env API keys; ChatService with retries before output
only, ledger row per request incl. errors and cancelled streams; /v1/chat/completions (JSON and SSE
with cost comment), /v1/embeddings (float and base64), /v1/models; ProviderError to HTTP mapping;
body limit 413; malformed JSON 400, schema errors 422.

## Next: US-06 then budgets

US-06 provider keys per org with envelope encryption (KeyManager port, AES-256-GCM, per-row DEK,
MASTER_KEY env) and adapter switch native|litellm (optional extra, contract suite on both).
US-07..US-10 budgets: budgets, budget_usage, budget_reservations, budget_requests, outbox, alerts;
reserve (worst_case per budget, consistent lock order) before the provider call, commit actual after,
janitor; 429 budget_exceeded naming the scope; 500-way race and idempotency tests.

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
