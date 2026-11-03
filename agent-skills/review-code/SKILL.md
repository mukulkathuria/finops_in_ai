---
name: review-code
description: Independent line-by-line review against the section 12 checklist with severity-ranked findings.
---
# review-code

**Trigger:** every pull request; run by a reviewer who did not write the code (fresh context).

**Checklist**
- Correctness: matches the story and acceptance criteria; typed errors; no silent exception swallowing.
- Authorization: identity, role and scope checked on every route and object; deny by default; no IDOR.
- Tenancy: `org_id` present and filtered; RLS forced; caches and indexes scoped.
- Money: integer micro-USD; reserve and commit atomic; idempotency honoured; no float math.
- Data layer: parameterised SQL; indexes with EXPLAIN evidence; reversible migrations.
- Security: no secrets in code, logs or errors; vetted crypto; SSRF and file handling safe; rate limits.
- Interfaces: no vendor types outside adapters; contract tests pass; failure policy defined.
- Concurrency: no unprotected shared mutable state; no blocking calls on the event loop; timeouts everywhere.
- Privacy: no raw prompts in logs or metrics; redaction applied; retention respected.
- Tests: assert real outcomes; negative cases present; no sleeps.
- Dependencies: exists on the registry, maintained, licensed, pinned.

**Output:** findings as `id, severity, file:line, issue, evidence, suggested fix`. Do not fix.
Severity: Critical (cross-tenant leak, auth bypass, secret exposure, budget bypass, RCE), High
(privilege escalation, injection, missing authorization, data loss), Medium, Low, Info.
