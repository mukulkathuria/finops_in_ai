# AI FinOps Gateway

A self-hostable AI control plane that every LLM, embedding, OCR and image-generation call passes
through, so an organization can **govern, optimize and account for AI spend**. It is
OpenAI-compatible inbound, multi-provider outbound, and backed by one PostgreSQL 19 database
(relational data, pgvector embeddings and a graph store).

> Specification: `AI FinOps Gateway End-to-End Build Specification_v2.md` (v2, 2026-11-02).
> Progress, open items and evidence: [`PROGRESS.md`](PROGRESS.md). Decisions: [`docs/adr/`](docs/adr).

## Architecture

```text
 clients (OpenAI SDK, apps, agents, dashboard)
        |
        v
 +--------------------------- gateway (stateless, scale horizontally) ---------------------------+
 |  data plane /v1/*            control plane /v1/admin/*, /v1/auth/*                            |
 |  ingress -> authn -> authz -> budget reserve -> guardrails -> cache -> context -> router       |
 |          -> provider (fallback chain) -> output guardrails -> commit (ledger + outbox)         |
 |  ports: LLMAdapter | DocumentParser | Classifier | Embedder | CacheStore | GraphStore | ...    |
 +-----------------------------------------------------------------------------------------------+
        |                 |                                   |
        v                 v                                   v
 PostgreSQL 19     providers / sidecars                 worker (outbox, janitor,
 (RLS, pgvector,   (OpenAI-compatible, Anthropic,        rollups, price sync)
  graph, ledger)    Gemini, mock, OCR, Laya, embedder)
```

Ports and adapters (ADR 0001): `gateway/domain` is pure logic, `gateway/ports` holds the
interfaces, `gateway/adapters` holds every vendor integration, `gateway/app` wires them from
config. `import-linter` enforces the direction of dependencies in CI.

## Quick start (dev stack)

Prerequisites: Docker with Compose v2, [uv](https://docs.astral.sh/uv/) (Python 3.14 is fetched
automatically), Node 24+ for the dashboard.

```bash
uv sync                                # Python dependencies (locked in uv.lock)
uv run python scripts/init_env.py      # writes .env with fresh random secrets (never overwrites)
docker compose up -d --build           # PostgreSQL 19 RC1, mock LLM, migrations, gateway
curl http://127.0.0.1:8080/readyz      # {"status":"ok","checks":{"database":"ok"}}
open http://127.0.0.1:5173            # dashboard (sign in with the bootstrap admin)
```

If port 5432 is taken on your host, create the env file with
`POSTGRES_PORT=55432 uv run python scripts/init_env.py`. Every published port binds to
`127.0.0.1` only.

## Development

| Task | Command |
| --- | --- |
| Lint and format | `uv run ruff check . && uv run ruff format --check .` |
| Types | `uv run mypy` (strict) |
| Layer contracts | `uv run lint-imports` |
| Unit and contract tests | `uv run pytest tests/unit tests/contract` |
| Integration tests (real PostgreSQL 19) | `uv run pytest -m integration` (needs `TEST_DATABASE_URL` in `.env`) |
| Route authorization audit | `uv run python scripts/audit_routes.py` |
| RLS coverage audit | `AUDIT_DATABASE_URL=... uv run python scripts/audit_rls.py` |
| Migrations | `uv run alembic upgrade head` (uses `MIGRATOR_DATABASE_URL`) |
| Dashboard (dev server, proxies `/v1` to the gateway) | `cd web && npm ci && npm run dev` |
| Dashboard checks | `cd web && npm run typecheck && npm test && npm run build` |

Integration tests create a disposable database per session, apply the same setup SQL as the
container bootstrap, migrate it, and connect through the real `app`, `worker` and
`readonly_analytics` roles, so Row-Level Security is tested exactly as it runs in production.

## Budgets and spend control

Every model call reserves its worst-case cost against each budget that applies (member, team,
org) before the provider is called, and settles the actual cost with the ledger row afterwards.
Concurrent requests can never push spending past a hard limit; a request that would gets
`429 budget_exceeded` naming the tightest scope and a link to ask for more.

| Endpoint | Who | Purpose |
| --- | --- | --- |
| `PUT /v1/admin/budgets` | org admin; team lead for member limits in their team | create or change a limit (day, week, month; usd, tokens, images) |
| `GET /v1/admin/budgets`, `DELETE /v1/admin/budgets/{id}` | admin, finance (read), team lead (own teams) | limits with committed, reserved and remaining for the current period |
| `GET /v1/admin/budget-alerts` | as above | the one soft-threshold alert per budget and period |
| `GET`/`PUT /v1/admin/budget-settings` | org admin | org timezone for periods; `overcommit` or `strict` allocation |
| `POST /v1/budget-requests`, `GET`, `POST /{id}/decision` | members ask; leads (admins for team budgets) decide once or permanently | requests for more budget |
| `GET /v1/me/usage` | any user, or an API key bound to a user | own usage and remaining budget, nobody else's |

Send `Idempotency-Key: <id>` on `/v1/chat/completions` or `/v1/embeddings` and a retry replays
the first response (`idempotent-replayed: true`) instead of being charged again. The `worker`
compose service (`python -m worker`) returns budget holds of crashed requests, rebuilds counters
from the ledger and purges old idempotency keys.

## Repository layout

| Path | Purpose |
| --- | --- |
| `gateway/` | Data plane, shared domain, ports, adapters, pipeline and the composition root |
| `control_plane/` | Admin API: orgs, teams, users, keys, budgets, policies, reports, audit |
| `worker/` | Outbox consumer and background jobs |
| `migrations/` | Alembic migrations (explicit SQL, RLS on every tenant table) |
| `sidecars/` | Separately deployed services (mock LLM, OCR, Laya classifier) |
| `infra/` | Dockerfiles, Terraform, Helm, observability config |
| `web/` | React 19 dashboard |
| `docs/adr/` | Architecture decision records |
| `docs/threat-model.md` | Threat model with control and test traceability |
| `agent-skills/` | Written procedures for recurring engineering tasks |
| `scripts/` | Audits and operational scripts |
| `tests/` | Unit, contract, integration, end-to-end and load tests |

## Important notes

- **PostgreSQL 19 and SQL/PGQ.** SQL/PGQ property graphs were reverted during the PostgreSQL 19
  beta cycle and are absent from 19 RC1 (verified, see ADR 0004). Graph reads go through the
  `GraphStore` port using indexed joins and recursive CTEs, which run on PG16+ including PG19.
- **Secrets** live only in `.env` (gitignored) or your secret manager. `.env.example` holds
  placeholders only. Provider keys are stored with envelope encryption and never returned.
- This is engineering guidance, not legal advice; have counsel review compliance claims.
