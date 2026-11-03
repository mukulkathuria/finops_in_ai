# Progress

Resume rule: read this file, the latest ADRs and open findings, then continue the current phase.

## Phase 0: Foundations (done)

| Exit criterion | Evidence |
| --- | --- |
| One command starts the stack | `docker compose up -d --build`: db healthy, migrate exited 0, gateway and mock-llm healthy, `/readyz` 200 |
| CI green on the empty app | `.github/workflows/ci.yml`: static, tests (PG19), security, image jobs |
| RLS-coverage and route-authorization scripts run | `scripts/audit_rls.py` (0 violations), `scripts/audit_routes.py` (0 violations), both also covered by tests |
| Skills reviewed | `agent-skills/` (12 skills from spec section 9) |

Also delivered: ADRs 0001-0005, threat model v1, PG19 RC1 image (pgvector 0.8.7, pg_partman 5.5.0),
schema v0 with the forced-RLS pattern, mock OpenAI-compatible provider with fault injection.

## Findings and deviations from the spec

| Id | Finding | Resolution |
| --- | --- | --- |
| D-1 | SQL/PGQ is not in PostgreSQL 19 (present in 19beta1, reverted by beta4; absent in RC1) | ADR 0004: GraphStore with joins and recursive CTEs, capability probe for a future PGQ store |
| D-2 | Fourth database role `worker` added for cross-tenant jobs | ADR 0002 |
| D-3 | `httpx2` instead of `httpx` (maintained continuation, required by OpenAI 3.x and MCP 2.x SDKs) | ADR 0001 |
| D-4 | `control-plane/` is `control_plane/` (Python package naming) | ADR 0001 |

## Next

Phase 1 (MVP): US-01 to US-10, US-28, US-29. See `docs/superpowers/plans/2026-11-03-finops-gateway.md`.

## Blocked

Nothing blocked. Real provider keys, GPUs for OCR sidecars and cloud credentials are not available
in the build environment; those adapters are verified with contract suites against recorded
fixtures and are marked as such.
