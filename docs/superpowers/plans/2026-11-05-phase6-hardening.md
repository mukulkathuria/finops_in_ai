# Phase 6: Hardening and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver US-27 and US-30. Add load, race and failure tests and a restore drill, then
close the release audit.

- **US-27:** OIDC SSO that maps to existing users through `identities`, with local login off per
  org.
- **US-30:** Terraform that provisions and destroys dev and staging from empty.

**Architecture:**

- **SSO.** A control-plane SSO service implements the OIDC authorization code flow with PKCE
  against each org's identity provider. It keeps single-use login states, validates the ID
  token with PyJWT against the provider's JWKS, maps `(issuer, subject)` to a user through
  `identities`, and issues the usual session. The browser never sees tokens in a URL: the
  callback hands the web app a one-time code to exchange.
- **Terraform.** OpenTofu-compatible modules drive the Docker provider, so dev and staging are
  real environments a CI runner can build and tear down without cloud credentials.
- **Load tests.** k6 against the compose stack with the mock LLM.

**Tech Stack:** FastAPI, PyJWT[crypto], httpx2, PostgreSQL 19 (RLS), Terraform ≥ 1.9 /
OpenTofu ≥ 1.8 with kreuzwerker/docker, k6, GitHub Actions.

**Spec:** `../AI FinOps Gateway End-to-End Build Specification_v2.md`:

- section 7: US-27 and US-30;
- section 8: phase 6;
- section 11: load profiles and pass criteria;
- section 12: release audit checklist.

## Global Constraints

- US-27: "OIDC login maps to an existing user via `identities`; local login can be disabled per org".
- US-30: "A single documented command set provisions a dev and a staging environment from empty, and destroys it cleanly".
- Infrastructure as code: "Terraform (OpenTofu-compatible)".
- Pass criteria (section 11):
  - gateway overhead p95 ≤ 100 ms and p99 ≤ 250 ms;
  - error rate (excluding intended 429 and blocks) under 0.1%;
  - budget overshoot zero;
  - recovery after a spike or failure within SLO in under 2 minutes.
- Every new tenant table: forced RLS and a policy. Migrations reversible and rehearsed with data,
  as `migrator` (`tests/integration/test_migrations.py`).
- Secrets: never in the repo. The client secret is sealed with the org envelope, like provider
  keys. Gitleaks runs before every push.
- No real cloud credentials, GPUs or IdP are available. Everything must be provable with fakes in
  CI.

## Review Focus

1. **A stolen or replayed callback** (a state used twice, an expired state, a state from another
   browser). Expect: refused, nothing issued.
2. **An ID token** that is:
   - signed by another key, or with `alg: none` or HS256 keyed by the client secret;
   - issued for another audience or issuer;
   - expired;
   - carrying the wrong nonce.

   Expect: 401, audited.
3. **An IdP user whose email matches an org user** while `email_verified` is false, or whose
   domain is not allowed. Expect: no link and no session.
4. **Local login disabled.** Expect:
   - a correct password for a non-admin is refused only after the password is checked, so the
     refusal reveals nothing;
   - admins keep local login (break-glass).
5. **`terraform destroy`** must leave no container, volume or network behind, and `apply` must be
   repeatable (idempotent).

---

### Task 1: US-27 OIDC SSO

**Files:**

- Create:
  - `migrations/versions/0028_sso.py`:
    - `identities (id, org_id, provider, subject, user_id, email, created_at, last_login_at, UNIQUE (provider, subject))`;
    - `org_sso` (issuer, client id, the sealed client secret, allowed domains, `local_login_disabled`);
    - `sso_logins` (single-use states and one-time codes, hashed, with an expiry);
    - RLS on all three; pre-tenant lookups by hash only.
  - `gateway/domain/sso.py`: PKCE pair, state, claims checks (pure).
  - `gateway/adapters/sso/oidc.py`: discovery and JWKS (cached, refreshed on an unknown `kid`), code exchange, ID token verification.
  - `gateway/adapters/postgres/sso.py`.
  - `control_plane/services/sso.py`.
  - `control_plane/api/sso.py`.
  - The web login: an SSO button and the `/login/sso` exchange.
- Modify:
  - `control_plane/services/sessions.py`: local login off per org, admins exempt.
  - The container, `main.py`, `.env.example` and README.
- Test:
  - `tests/unit/test_sso.py`;
  - `tests/integration/test_us27_sso.py`, with a fake IdP on the app's HTTP client (`httpx2.MockTransport`).

**Interfaces:**

- `GET /v1/auth/sso/start?org=<slug>` → 303 to the provider. The state row holds the nonce and the PKCE verifier.
- `GET /v1/auth/sso/callback?code&state` → 303 to `{WEB_BASE_URL}/login/sso?code=<one-time>`.
- `POST /v1/auth/sso/exchange {code}` → `SessionOut`, the same as password login.
- `PUT|GET|DELETE /v1/admin/sso` (ORG_MANAGE): the configuration. The secret is write-only.

- [x] Write the unit tests: PKCE S256 (`code_challenge` = base64url(sha256(verifier))). The claims checks refuse a wrong iss, aud, azp, nonce, exp, nbf or alg.
- [x] Write the integration tests:
  - the full flow maps a verified email to an existing user and issues a session;
  - a second login uses the stored identity;
  - an unknown user, an unverified email or a foreign domain gets 401;
  - state reuse and an expired state get 401;
  - local login off refuses a member and keeps an admin.
- [x] Implement the migration, domain, adapter, repository, service and API; wire them in.
- [x] Run the gates. Commit `feat(US-27)`.

### Task 2: US-30 Terraform environments

**Files:**

- Create:
  - `infra/terraform/modules/finops_stack/{main,variables,outputs,versions}.tf`: network, volumes, PostgreSQL 19 (+ roles bootstrap), migrations, gateway, worker, mock LLM.
  - `infra/terraform/envs/{dev,staging}/main.tf` with `terraform.tfvars`.
  - `scripts/env.sh up|down|status <env>`: the one documented command set.
  - `.github/workflows/terraform.yml`: fmt, validate and tflint; then apply dev, check `/healthz` and `/readyz`, destroy, and assert nothing is left.
  - `docs/runbooks/deploy.md`.
- [x] Write the CI job, which fails until the module exists.
- [x] Implement the module and envs. Secrets come from `TF_VAR_*`, never from files in the repo.
- [x] Commit `feat(US-30)`.

### Task 3: Load, race and failure under load

**Files:** `tests/load/k6/{baseline,spike,soak,race}.js`; `.github/workflows/load.yml` (compose
stack with the mock LLM; short baseline and spike; budget race; a DB restart mid-run);
`docs/load-testing.md`.

- [x] Overhead = total latency − provider time (the mock's fixed delay, and the gateway's
  `server-timing`). Thresholds come from section 11.
- [x] Commit `test(phase-6)`.

### Task 4: Restore drill and the GA upgrade rehearsal

**Files:**

- `scripts/restore_drill.sh`: `pg_dump -Fc`, restore into a fresh database, then migrations at head, the RLS audit and row counts equal.
- A CI job.
- `docs/runbooks/restore.md`.
- `docs/runbooks/upgrade-postgres.md`: from RC1 to GA with `pg_upgrade --link` on a copy, and dump and restore as the fallback.

### Task 5: Release audit

**Files:**

- A license check in CI: no copyleft in the runtime closure without approval.
- `docs/audits/2026-11-05-release.md`:
  - a traceability matrix (stories → code → tests; threats → controls → tests);
  - the hot-query review with EXPLAIN;
  - configuration and database review;
  - residual and accepted risks.
- A two-reviewer cross-check of auth, budgets, cache and tenancy.
