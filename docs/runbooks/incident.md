# Runbook: incidents

First steps for every incident:

- open the incident channel and note the time;
- keep the `x-request-id` and `x-trace-id` of failing requests. Logs never hold prompt text, so
  search them by request id.

## A provider is down or slow

- **Signs:**
  - 502 or 503 answers naming one provider;
  - its latency rising in the `finops_*` metrics.
- **Act:**
  1. The gateway already retries rate limits, timeouts and outages on chat and embeddings.
     Image renders are retried only on a rate limit, because a render that timed out may still be
     billed. A failed request releases its budget hold.
  2. Callers name the model they want. Tell the affected teams to switch to another provider's
     model. If the org uses smart routing, point `large_model` and `small_model` at a healthy
     provider (`PUT /v1/admin/routing-settings`).
  3. Holds of requests that died with a process are returned by the worker's janitor once they
     expire (15 minutes). Nothing needs cleaning by hand.

## Spend is running away

- **Signs:**
  - budget alerts on the org's webhook, and `finops_budget_soft_alerts_total` rising;
  - one key or team at the top of the usage report.
- **Act:**
  1. Lower or zero the team's or member's budget (`PUT /v1/admin/budgets`). It applies to the next
     request.
  2. Revoke the key (`DELETE /v1/admin/teams/{team_id}/api-keys/{key_id}`). Revocation is
     immediate.
  3. Read the audit log (`GET /v1/admin/audit`) to find who changed what.

## The database is down or slow

- **Signs:** `/readyz` failing, 5xx on every route, and pool timeouts in the logs.
- **Act:**
  1. Restart PostgreSQL. The gateway reconnects by itself: the first requests fail, then the pool
     replaces its dead connections. The `load` workflow checks that answers are back within the SLO
     two minutes after a restart under load.
  2. If the data is damaged, follow `restore.md`.

## A key or secret may have leaked

| What leaked | Do now |
| --- | --- |
| An API key | Revoke it. The audit log shows what it was used for; the ledger shows what it spent |
| A provider key | Rotate it at the provider, then `PUT /v1/admin/providers/{provider}/key` |
| A user's password or session | Disable the user, below |
| `JWT_SIGNING_KEY` | Rotate it, below. Tokens signed with the old key stop working once its public half is removed |
| `MASTER_KEY` | Rotate it, below, and treat every sealed secret as exposed: rotate provider keys, the webhook secret and single sign-on client secrets |

### Disable a user

There is no API for this yet. A database administrator runs it as the superuser:

```sql
UPDATE users SET status = 'disabled' WHERE email = 'person@example.com';
UPDATE refresh_tokens SET revoked_at = now()
WHERE user_id = (SELECT id FROM users WHERE email = 'person@example.com') AND revoked_at IS NULL;
```

Every request checks the user's status, so access tokens already issued stop working at once.
Sign-in, refresh and single sign-on refuse the user too.

### Rotate the JWT signing key

1. Note the old key's public half:

   ```bash
   uv run python -c "import base64, sys; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K; from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat; k = K.from_private_bytes(base64.urlsafe_b64decode(sys.argv[1] + '=' * (-len(sys.argv[1]) % 4))); print(base64.urlsafe_b64encode(k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).rstrip(b'=').decode())" "$OLD_JWT_SIGNING_KEY"
   ```

2. Set a new `JWT_SIGNING_KEY` (32 random bytes, base64url) and a new `JWT_KEY_ID` (say `k2`).
3. Set `JWT_PREVIOUS_PUBLIC_KEYS=k1:<old public half>` and restart. Tokens already issued keep
   working.
4. After one access-token lifetime (`ACCESS_TOKEN_TTL_SECONDS`, 15 minutes by default), empty
   `JWT_PREVIOUS_PUBLIC_KEYS` and restart.

If the old key leaked, skip steps 1 and 3, and the wait: everyone signs in again.

### Rotate the master key

1. Set a new `MASTER_KEY` and `MASTER_KEY_ID`.
2. Move the old pair to `MASTER_PREVIOUS_KEYS` (`m1:<old key>`).
3. Restart.

New secrets are sealed with the new key, and older ones still open with the old. Stored secrets
are not re-sealed. To retire the old key, first seal each secret again with the new one:

- provider keys: `PUT /v1/admin/providers/{provider}/key`;
- the webhook secret: `POST /v1/admin/webhook/secret`. This issues a new secret, so update the
  receiver;
- single sign-on: `PUT /v1/admin/sso` with the client secret.

Then remove the old key. Cached answers sealed with it become cache misses.

## After the incident

Write the following into `docs/audits/incidents/<date>.md`:

- what happened, and when;
- the impact on requests, spend and orgs;
- the cause and the fix.

Add a test that fails without the fix.
