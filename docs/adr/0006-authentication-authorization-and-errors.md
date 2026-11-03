# ADR 0006: Local authentication, RBAC as data, and typed errors

- Status: accepted
- Date: 2026-11-03
- Stories: US-01, US-02, US-03, US-04, US-27, US-28 (spec sections 1, 6, 10)

## Context
The spec fixes local email and password with Argon2id plus JWT now and SSO (OIDC, SAML, SCIM)
later through an `identities` table. JWTs must be short-lived EdDSA with `kid`, the algorithm
pinned, and refresh tokens rotating with reuse detection. Authorization is deny by default with
roles org admin, finance, team lead, member and auditor.

## Decision
- Passwords: Argon2id (argon2-cffi defaults = RFC 9106 low-memory profile). Hashing runs in a
  thread behind a semaphore (`PASSWORD_HASH_CONCURRENCY`) and misses verify a dummy hash so timing
  does not reveal whether an email exists. Policy: 12-128 characters, not a common password, at
  least 6 distinct characters, must not contain the email name or the org name.
- Tokens: EdDSA (Ed25519) access tokens, 15 minutes, claims `iss aud sub org typ iat nbf exp jti`.
  The signing key is a base64url 32-byte seed in `JWT_SIGNING_KEY`; `JWT_PREVIOUS_PUBLIC_KEYS`
  keeps old public keys verifiable during rotation and in `/.well-known/jwks.json`. Verification
  rejects unknown `kid`, any algorithm other than EdDSA (so `none` and HS256 key confusion fail),
  and missing claims.
- One org per user in v1, so email is globally unique and login needs no org hint.
- Signup modes: `bootstrap` (only the first org, then closed; the default for self-hosting),
  `open` (multi-tenant SaaS) and `disabled`. The bootstrap flag flips atomically in the same
  transaction that creates the org.
- RBAC is data: `roles`, `permissions`, `role_permissions`, `role_assignments`. Team-scoped grants
  (team lead) apply to one team only; every route declares `require(permission)` and use cases
  re-check object scope with `principal.can(permission, team_id)`.
- Tenant child tables reference parents through composite `(org_id, id)` foreign keys, so a row
  can never point at another org's row even if application code passes a foreign id.
- Errors: expected failures are `DomainError` subclasses (typed, carrying a stable `code`) that the
  API maps to status codes and OpenAI-shaped bodies. This is the Python-idiomatic form of the
  spec's "typed results for expected failures"; the request pipeline itself (budget, guardrails)
  returns typed decision values. Anything else is a bug and becomes a generic 500.

## Consequences
- Moving a user between orgs is not supported in v1 (would need re-keying by org).
- Changing role permissions is a data migration, picked up by the role catalog cache.
