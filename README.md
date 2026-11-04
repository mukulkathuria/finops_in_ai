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
curl http://127.0.0.1:8080/readyz      # {"status":"ok","checks":{"database":"ok","objects":"ok"}}
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

Soft-threshold alerts reach your systems through a signed webhook: `PUT /v1/admin/webhook`
`{"url": "https://..."}` returns the signing secret once. Each delivery is a POST with
`X-FinOps-Event`, `X-FinOps-Delivery` (dedupe on it: delivery is at least once),
`X-FinOps-Timestamp` and `X-FinOps-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>`.
Receivers must be public https hosts; failed deliveries back off for up to 10 attempts
(`GET /v1/admin/webhook/deliveries` shows their state).

Send `Idempotency-Key: <id>` on `/v1/chat/completions` or `/v1/embeddings` and a retry replays
the first response (`idempotent-replayed: true`) instead of being charged again. The `worker`
compose service (`python -m worker`) returns budget holds of crashed requests, rebuilds counters
from the ledger and purges old idempotency keys.

## Guardrails (PII, secrets, prompt injection)

Every chat and embedding request is inspected after its budget hold and before the provider
sees it; answers are inspected on the way back, streams included.

| Check | Default | Other settings |
| --- | --- | --- |
| PII (email, phone, card with Luhn, IBAN, SSN, Aadhaar, PAN, IP) | `redact`: `[EMAIL_1]`, the same value keeps its number | `block`, `off` |
| Secrets (cloud, GitHub, OpenAI, Anthropic, Slack, Stripe keys, JWTs, private keys, gateway keys) | `block` | `redact` (`[REDACTED_SECRET]`) |
| Prompt injection (rules first, then the classifier) | `block` at probability 0.8 | `flag` (recorded only), `off`; `injection_threshold` 0.5 to 0.99 |
| Classifier unavailable | `closed`: 503 `guardrail_unavailable` | `open` (recorded as `failed_open`) |
| Model output | `redact` secrets and PII | `off` |
| Hosted classifier (Jev) may see prompt text | `true` | `false`: the check is skipped and recorded |

`GET`/`PUT /v1/admin/guardrails` reads and changes the org policy (admins change it, auditors
read it; every change is audited). Obfuscation is handled: zero-width characters, fullwidth
digits, base64 payloads, leetspeak, text hidden in Unicode tag characters, and English, French,
Spanish, Portuguese, German, Russian, Chinese and Hindi phrasings. A blocked request gets
`400 guardrail_blocked` naming the rules, or `400 prompt_injection`; it is recorded in the ledger
as `blocked` and never charged. Each decision is logged and stored in the ledger row's
`flags.guardrails` with its rule, backend and version, never the matched text; the metric is
`finops_guardrail_decisions_total`.

Classifier backend (`.env`): `CLASSIFIER_BACKEND=rules|laya|jev`. Laya runs locally
(`pip install "laya[serve]"`, then `laya-serve` on port 8000; set `LAYA_BASE_URL`); the dev stack
points at the mock provider's stand-in. Jev is hosted and needs `JEV_API_KEY` (the gateway refuses
to start with `jev` and no key). Deterministic rules always run first and always win.

## Files (uploads)

`POST /v1/files` (multipart `file` and `purpose`, as with the OpenAI SDK's `files.create`) stores
a file for the uploader's team after checking it from its bytes; `GET /v1/files`,
`GET /v1/files/{id}`, `GET /v1/files/{id}/content` and `DELETE /v1/files/{id}` list, read,
download and delete. Only the uploader's team (and the uploading user) can see a file.

| Check | Refusal (`error.code`) |
| --- | --- |
| Type sniffed from the content must match the extension (PDF, PNG, JPEG, GIF, WebP, DOCX, XLSX, PPTX, TXT, MD, CSV, JSON, HTML) | 422 `unsupported_file_type`, `file_type_mismatch` |
| Size (`MAX_UPLOAD_BYTES`, 25 MiB) | 413 `file_too_large`, 422 `empty_file` |
| PDF pages and slides (`MAX_FILE_PAGES`), checked in an isolated, time-limited process | 422 `too_many_pages`, `encrypted_file`, `corrupt_file`, `unprocessable_file` |
| Office archives: entries, unpacked size and compression ratio, every member decompressed under caps | 422 `archive_bomb` |
| Image size from the header (`MAX_IMAGE_PIXELS`) | 422 `image_too_large` |
| Malware test signature, inside archives too; optional ClamAV (`CLAMAV_ADDRESS`, fails closed) | 422 `malware_detected`, 503 `scanner_unavailable` |
| Names with a path or control and invisible characters | 422 `invalid_filename` |

Originals live in the object store (`OBJECT_STORE_PATH`, a volume in the dev stack) under a key
made from the file id, never from its name. Downloads are attachments with `nosniff` and a
sandboxing CSP.

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
