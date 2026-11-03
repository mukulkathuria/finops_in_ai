# AI FinOps Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the AI FinOps Gateway end to end (spec v2): OpenAI-compatible gateway with auth,
hierarchical budgets, guardrails, document conversion, caching, routing, RAG, code graph, agent
workspace, image generation, SSO and IaC, on PostgreSQL 19 with pgvector.

**Architecture:** Modular monolith (gateway + control plane in one image, worker separate) with
ports and adapters, a stage pipeline and immutable config snapshots (ADR 0001, 0005). One
PostgreSQL 19 database with forced RLS (ADR 0002), integer micro-USD money (ADR 0003) and a
GraphStore without SQL/PGQ (ADR 0004).

**Tech Stack:** Python 3.14, FastAPI 0.162, Pydantic 2.14, SQLAlchemy 2.1 async + psycopg 3.3,
Alembic 1.20, httpx2 2.16, argon2-cffi, PyJWT (EdDSA), cryptography, valkey, OpenTelemetry,
prometheus-client; PostgreSQL 19 RC1 + pgvector 0.8.7 + pg_partman 5.5.0; React 19 + Vite +
Tailwind + shadcn/ui + Recharts; Terraform; Docker Compose.

**Spec:** `../AI FinOps Gateway End-to-End Build Specification_v2.md` (sections referenced as S<n>).

## Global Constraints

- Python 3.14+, `mypy --strict`, `ruff`, `import-linter` contracts green on every commit.
- Money is `bigint` micro-USD; prices micro-USD per 1M tokens; ceil rounding (ADR 0003).
- Every tenant table: `org_id`, ENABLE + FORCE RLS, `tenant_isolation` policy, explicit grants.
- Primary keys UUIDv7 (`uuidv7()` in SQL, `uuid.uuid7()` in Python).
- Secrets never in repo, logs, prompts or fixtures; `.env.example` placeholders only.
- Vendor SDK types never leave their adapter; callers depend on ports.
- Every story: tests for each acceptance criterion plus at least one negative case.
- Access token TTL about 15 minutes; refresh rotation with family revocation on reuse (US-03).
- Revoked API keys fail within 5 seconds (US-04): no key cache longer than that.
- 500 parallel requests never overshoot a hard limit (US-10).
- Error bodies follow the OpenAI shape `{"error": {"message", "type", "code", "param"}}`.

## Review Focus

1. Client disconnect mid-stream: reservation must be committed or released exactly once, billing
   only what was used. Test in US-05 (streaming) with a cancelled client.
2. Limit lowered below current usage and negative or huge limit values: 422, counters unchanged.
   Test in US-07 and US-08.
3. Duplicate `Idempotency-Key` sent concurrently (50x): exactly one charge. Test in US-10.
4. Tenant context leakage through pooled connections: `set_config(..., true)` must not survive a
   transaction. Test in US-09 (sequential requests from two orgs on one connection).
5. Invalid UTF-8, deep JSON and oversized bodies on every public endpoint: 4xx, no crash. Test in
   US-05 (ingress limits).

---

## Phase 1: Core gateway and budgets (MVP)

### Task US-01: Signup with local login
- Files: `migrations/versions/0002_identity.py`, `gateway/domain/passwords.py`,
  `gateway/adapters/security/{passwords,jwt}.py`, `control_plane/api/auth.py`,
  `control_plane/services/identity.py`, tests `tests/unit/test_passwords.py`,
  `tests/integration/test_us01_signup.py`.
- Interfaces: `password_problems(password: str, email: str) -> list[str]`;
  `PasswordHasher.hash/verify` (Argon2id, off the event loop); `TokenIssuer.issue_access(user) -> str`,
  `TokenIssuer.verify(token) -> AccessClaims`; `GET /.well-known/jwks.json`.
- Tests: valid signup creates org + admin + JWT; weak password 422; duplicate email 409; stored
  hash starts with `$argon2id$`; `alg: none` and wrong-key tokens rejected.

### Task US-02: Teams and invitations
- Tables `teams`, `team_members(role)`, `invitations(token_hash, expires_at, used_at)`.
- Tests: invite then accept sets password and joins team; expired link 410; reused link 410;
  non-admin cannot invite (403).

### Task US-03: Sign in and refresh
- `refresh_tokens(token_hash, family_id, rotated_at, revoked_at, expires_at)`; lockout after
  repeated failures with backoff.
- Tests: access token `exp - iat == 900`; refresh rotates; replaying an old refresh token revokes
  the whole family (subsequent refresh with the newest token fails); audit entry written.

### Task US-04: Team API keys
- `api_keys(prefix, key_hash, team_id, user_id, scopes, revoked_at)`; key format
  `fo_<prefix8>_<secret>`; shown once.
- Tests: key returned once and never again; DB holds only SHA-256; revoked key 401 on the next
  request (no cache); lead of another team cannot create or revoke (403/404).

### Task US-05: OpenAI-compatible endpoint
- Canonical types in `gateway/domain/llm.py`; `LLMAdapter` port; native adapters
  `openai_compat`, `anthropic`, `gemini`; contract suite with recorded fixtures; pipeline runner;
  `/v1/chat/completions` (JSON and SSE), `/v1/embeddings`, `/v1/models`; usage and cost headers.
- Tests: official `openai` SDK against the in-process app (chat, stream, embeddings); headers
  `x-finops-cost-micro-usd` present; ingress limits (413, invalid UTF-8, deep JSON).

### Task US-06: Provider keys and adapter switch
- `provider_credentials` with envelope encryption (`KeyManager` port, AES-256-GCM, per-row DEK);
  `PROVIDER_ADAPTER_DEFAULT` and per-provider `adapter: native | litellm`.
- Tests: ciphertext in DB, API never returns the key; switching adapter by config passes the
  shared contract suite for both.

### Task US-07 / US-08 / US-09 / US-10: Budgets
- Tables `budgets`, `budget_usage`, `budget_reservations`, `budget_requests`, `usage_events`
  (partitioned), `outbox`, `alerts`; `BudgetService.reserve/commit/release`; janitor job.
- Tests: hard limit 429 naming the scope; soft alert once per period; lead limits within cap,
  foreign team 403, above cap 422 unless overcommit; member sees own usage matching ledger and not
  others; 500 parallel requests never overshoot; idempotency key replay charges once.

### Task US-28: Audit trail
- `audit_log` partitioned, append-only (no UPDATE/DELETE grants plus trigger), per-org hash chain
  via `audit_append(...)`; `GET /v1/admin/audit` and `/verify`.
- Tests: login, key, limit and policy changes and denials recorded; tampering breaks verification.

### Task US-29: Health, metrics, tracing
- `/healthz`, `/readyz`, `/metrics` (Prometheus), OpenTelemetry spans per request and stage.
- Tests: metrics expose request and stage histograms; in-memory exporter sees one trace with
  stage child spans.

## Phase 2: Safety and files (US-11 to US-16)
- Classifier port + `rules` (regex, Luhn, secrets), `laya` and `jev` (SystemOne protocol client);
  input and output guardrails with fail-open/closed per route; file validation; DocumentParser
  port with `glm_ocr` and `paddleocr_vl` sidecar clients, `markitdown` and PDF text layer; parse
  cache; ACL-aware retrieval for the salary scenario.

## Phase 3: Savings engine (US-17 to US-20, US-26)
- Stage contract with shadow mode and eval gate; exact and semantic cache (L1 LRU, L2 Valkey,
  L3 Postgres + pgvector) with scope-hashed keys; compression; complexity router with cascade and
  sampled judge; baseline-versus-actual savings reports; optional Jev; optional LiteLLM adapter;
  price sync.

## Phase 4: Context engineering (US-21, US-22)
- ACL-aware RAG ingestion, reranking, relevance threshold, token packing; code indexer
  (tree-sitter, Graphify import) into `graph_vertex`/`graph_edge`; context packs; GraphStore; MCP
  context tool.

## Phase 5: Agents and image generation (US-24, US-25)
- Workspace tables and MCP context broker with graph memory of facts; dedupe and loop detection;
  per-agent budgets; image generation with quotas, moderation and per-image cost.

## Phase 6: Hardening and release (US-27, US-30)
- OIDC SSO via `identities`; Terraform dev and staging environments; load, race and soak tests;
  audit report under `docs/audits/`.
