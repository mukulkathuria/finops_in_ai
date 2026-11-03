# Phase 1 review loop (spec section 12)

Scope: US-01 to US-10, US-28, US-29 at commit `51565ac`, reviewed by two independent reviewers with
fresh context. One reviewed authentication, authorization and tenancy. The other reviewed the
budget engine, money and concurrency. The author fixed every finding with a regression test
(`tests/integration/test_phase1_review.py`). Both reviewers re-reviewed the fixes (`d7a7550` and
the follow-up commit). Result: no open Critical or High finding.

## Findings and resolution

| Id | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| C1 | Critical (budget bypass) | A JWT user in several teams who sent no `X-FinOps-Team` was attributed to no team, so team and member budgets were skipped | Model calls require the header (422 `team_required`); verified by both reviewers |
| H1 | High | A stream closed before its first read never settled: no ledger row, the hold kept for 15 minutes, upstream left generating | `_Relay` settles exactly once on end, error or close; `_SettlingStream` always closes it; verified |
| M1 | Medium | Cancelling a request during settlement rolled the charge back | Settlement runs as its own task behind `asyncio.shield` |
| M2 | Medium | A transient database failure during settlement lost the charge | Settlement retries transient failures; the ledger insert is idempotent per hold |
| M3 | Medium | A stream charged for partial output frees its Idempotency-Key, so a retry is charged again | Accepted and documented (D-11): the failed attempt's partial output was billed by the provider |
| M4 | Medium | Strict allocation could be exceeded when a team's first cap raced member limits | Advisory lock per team cap before the allocation checks |
| M5 | Medium | Changing the org timezone restarted current counters at zero | Counters are rebuilt from the ledger after the change, one budget per transaction; a one-time grant stays with the period it was granted in (D-12) |
| S-M1 | Medium | A team lead could mint API keys bound to (and billed to) another user | Leads bind keys only to themselves |
| S-M2 | Medium | An unset `ENVIRONMENT` ran with ephemeral keys and public docs | Unset means production, which refuses missing secrets |
| S-M3 | Medium | A locked account answered 429, revealing that it exists | A lock answers exactly like a wrong password; targeted lockout remains an accepted risk until SSO (US-27) |
| S-L1 | Low | Invitations revealed whether a team or account exists before authorization | Authorization first |
| S-Low | Low | A lead could remove another lead of the same team | Only org admins remove a team lead |
| F-1 | Low | A reserve retried after a lost commit acknowledgement collided with its own hold | A fresh hold id per attempt |
| F-2 | Low | The timezone rebuild held every counter lock in one transaction | One transaction per budget, after the change commits |
| F-3 | Low | A ledger row with a hold but no request time could not be deduplicated | Refused with a clear error |

## Accepted risks and backlog (Low)

- Targeted account lockout (an attacker can keep an account locked with wrong passwords): mitigated
  by per-IP limits and Argon2id cost; revisit with SSO (US-27) and admin unlock.
- Denials are audited without a rate limit, so a low-privilege user can grow the audit log; add
  sampling or a per-user limit with the outbox work.
- Access tokens stay valid up to 15 minutes after logout (account status is re-checked per request).
- Open signup mode lets anyone check whether an e-mail is registered; bootstrap mode is the default.
- Behind a proxy, set `FORWARDED_ALLOW_IPS` to the proxy address so per-IP limits see real clients.
- One-time budget grants are not checked against the team cap; the team budget still bounds spend.
- After a 15-minute stale takeover of an Idempotency-Key, the original attempt can still complete it.
