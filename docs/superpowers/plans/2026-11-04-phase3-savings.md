# Phase 3 Savings Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every optimization is a pluggable stage with shadow mode and an eval gate, and every
request records what it would have cost without them (US-17 to US-19, US-26; US-20 is done).

**Architecture:** A `Stage` port (spec section 4) runs inside `ChatService` between the input
guardrails and the provider call. Stage reports are written in the settlement transaction, next
to the ledger row, so reports and money always agree. Per-org stage modes, the baseline model,
cache and routing settings live in `orgs.settings` behind a snapshot cache (the `PolicyCache`
pattern).

**Tech Stack:** as Phases 1 and 2; pgvector for the semantic cache; pg_partman for
`stage_reports`.

**Spec:** `../AI FinOps Gateway End-to-End Build Specification_v2.md` sections 4 (stage contract,
three rules), 5 (tables), 7 (US-17 to US-20, US-26), 8 (Phase 3 exit), 11 (eval and shadow).

**Execution:** native (decided for this build). Each task ends green on the local gate, then one
commit per story, a gitleaks-gated push, and the CI run checked.

## Global Constraints

- Money is integer micro-USD; a stage's `cost_delta` is micro-USD (negative is a saving).
- `savings = baseline - (cost + overhead)`. Baseline = the original input tokens (as the client
  sent them, attachments at their original estimate) plus the actual output tokens, priced at the
  org's baseline model (default: the requested model).
- Cache key = hash(org, scope, model, params, system prompt, normalized prompt); scope = the
  caller's team and groups. A hit never crosses an org or a scope (spec section 4, rule 2).
- A stage never breaks a request: an exception skips it (fail-open) and is reported.
- A stage runs for real (`on`) only after a passing eval run for its name and version. `shadow`
  runs it on a copy, reports what it would have saved and discards the output.
- New tables: `org_id`, ENABLE + FORCE RLS, `tenant_isolation`, explicit grants, UUIDv7 keys.

## Review Focus

1. A shadow stage mutating the live request: test that a shadow compression stage leaves the
   provider request byte-identical.
2. A cache entry served to another scope: the cross-scope test (two teams, same prompt) shows zero
   hits.
3. A stream served from the cache: the replay settles exactly once and bills nothing.
4. The router when the classifier fails: the requested model is used (fail-open).
5. The savings report reconciling with the ledger to the micro-dollar over a mixed day (hits,
   misses, routed and failed requests).

---

### Task US-26: Stage contract, registry, shadow mode, eval gate

**Files:** `gateway/domain/stages.py`, `gateway/pipeline/stages.py`,
`gateway/adapters/postgres/stages.py`, `control_plane/api/stages.py`,
`control_plane/services/stages.py`, migration `0017_stages_savings.py`; tests
`tests/unit/test_stages.py`, `tests/integration/test_us26_stages.py`.

- `StageReport(stage, version, phase, tokens_before, tokens_after, cost_delta_micro_usd,
  latency_ms, quality_risk, applied, shadow, decisions)`; a `Stage` protocol with `name`,
  `version`, `phase`, `default_mode` and `process(ctx) -> StageReport`.
- `RequestContext`: the canonical request, the attribution, the price entry and the org policy,
  with `fork()` for shadow runs.
- Registry by config, in order; the per-org mode lives in `settings.stages`.
- `PUT /v1/admin/stages/{name}` sets `off|shadow|on`. `on` needs an `eval_runs` row that passed
  for the stage's current version, else 409 `eval_gate_not_passed`.
  `POST /v1/admin/stages/{name}/evals` records a run (score, threshold, dataset). Both are
  audited.
- Tables `stage_reports` (partitioned monthly) and `eval_runs`; reports are inserted in `_end`.
- Tests: shadow output discarded; `on` without an eval refused; a failing stage skipped and
  reported; a new stage appears in reports with no dashboard change.

### Task US-17: Baseline versus actual savings

**Files:** `gateway/domain/savings.py`, `gateway/pipeline/chat.py` (`_end`), a reports API, the
worker rollup, `web/src/pages/Savings.tsx`; tests `tests/unit/test_savings.py`,
`tests/integration/test_us17_savings.py`.

- Baseline per request as in the constraints above. Overhead = OCR GPU seconds x
  `OCR_GPU_SECOND_MICRO_USD` + classifier calls x `CLASSIFIER_CALL_MICRO_USD` + embedding tokens
  at `EMBEDDING_MICRO_USD_PER_MTOK`.
- File conversion reports a `file_conversion` stage from the attachment reports.
- `GET /v1/reports/savings?from&to&group_by=team|model|technique` takes its totals from the
  ledger and the technique breakdown from `stage_reports`. The worker keeps a `savings_daily`
  rollup. Tests: the rollup equals the live totals, and the report equals the ledger sums.
- The dashboard page reads the report generically.

### Task US-18: Exact and semantic cache with scoped keys

**Files:** `gateway/domain/cache.py`, `gateway/adapters/postgres/cache.py`, the cache step in
`ChatService`; tests `tests/unit/test_cache_keys.py` (hypothesis: keys differ across scopes),
`tests/integration/test_us18_cache.py`.

- `cache_entries` with a `vector(1024)` HNSW index. The semantic lookup sits in the same statement
  as the org, scope, model and params filter. Entries have a TTL, and only `stop` answers without
  tool calls are stored.
- The lookup runs after the input guardrails. A hit settles with cost 0 and `cache_hit` set; the
  baseline is kept.

### Task US-19: Complexity router with a sampled judge

**Files:** `gateway/domain/routing.py`, `gateway/pipeline/routing.py`, an eval samples table and
a worker judge; tests `tests/unit/test_routing.py`, `tests/integration/test_us19_routing.py`.

- The score comes from the rules plus the classifier's complexity question, failing open to the
  requested model. `settings.routing` maps tiers to models; teams can opt out; `route_tier` is
  recorded.
- A sampled share of routed requests is replayed on the large tier and judged. `eval_samples` feed
  a quality report with a tolerance.

### Then

The compression stage (shadow by default), provider prompt caching markers (Anthropic), the price
sync job, and the optional LiteLLM adapter with the switch test.
