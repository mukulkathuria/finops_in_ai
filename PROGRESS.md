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
| US-29 liveness/readiness, Prometheus metrics, one trace per request with stage spans | (this commit) | `tests/integration/test_us29_observability.py`, `tests/unit/test_metrics_guard.py` |

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

## Next: Phase 2 (US-11..US-16: classifiers, guardrails, file validation, OCR, parse cache, ACL retrieval)

## Remaining stories

Phase 2 US-11..US-16; Phase 3 US-17..US-20, US-26; Phase 4 US-21,
US-22; Phase 5 US-24, US-25; Phase 6 US-27, US-30; web dashboard.

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
| D-8 | Soft-threshold alerts are recorded (`budget_alerts`, `GET /v1/admin/budget-alerts`), counted in `finops_budget_soft_alerts_total` and logged; e-mail or webhook delivery needs a notification channel | Delivery joins the outbox work |
| D-9 | Unused budget never rolls over (the spec makes rollover opt-in per org) | Not offered yet; the default behaviour is implemented |
| D-10 | Budget reservations key on a gateway-generated hold id, never the client-supplied `X-Request-ID` | Duplicate client request ids cannot collide or settle each other's holds |
| D-11 | An attempt that fails or is cancelled after partial output is billed for it and frees its Idempotency-Key, so a retry with that key runs (and is billed) again; only a completed request replays without a charge | The provider billed the partial attempt; replaying a truncated answer would be worse |
| D-12 | When the org timezone changes, current counters are rebuilt from the ledger for the new periods; a one-time grant stays with the period it was granted in | `docs/audits/2026-11-03-phase1-review.md` (M5) |
| D-13 | Members of several teams must send `X-FinOps-Team` on model calls (422 `team_required`); team and member budgets key on the team | Review finding C1 |

## Blocked

Nothing blocked. Real provider keys, GPUs for OCR sidecars and cloud credentials are not available
here; those adapters are verified with contract suites against recorded fixtures.
