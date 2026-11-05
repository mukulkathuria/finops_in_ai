# Runbook: provision and destroy an environment (US-30)

Terraform (or OpenTofu) provisions a whole environment on a Docker host from empty, and destroys
it leaving nothing behind. An environment holds the same services as the dev compose stack:

- PostgreSQL 19 with pgvector and pg_partman;
- the migrations;
- the gateway and the worker;
- the dashboard;
- the document parser;
- the mock provider.

Two environments are defined:

| Environment | ENVIRONMENT | Gateway | Dashboard | Signup |
| --- | --- | --- | --- | --- |
| `dev` | `dev` | 127.0.0.1:18080 | 127.0.0.1:15173 | open |
| `staging` | `staging` (production-like: unsafe settings refused) | 127.0.0.1:28080 | 127.0.0.1:25173 | the first admin only |

## Commands

```bash
scripts/env.sh up dev      # build the images, provision from empty
scripts/env.sh check dev   # /healthz, /readyz, the dashboard and its /v1 proxy answer
scripts/env.sh down dev    # destroy it
scripts/env.sh gone dev    # prove no container, volume or network of it is left
```

The same commands work with `staging`. To use OpenTofu, set `TF=tofu`. `IMAGE_TAG` names the
image tag; the default is `local`.

## Secrets and settings

- **Generated per environment.** Terraform generates the database role passwords, the JWT
  signing seed and the master key (`random_password`, `random_bytes`). Nothing secret is in the
  repository.
- **Where they live.** They live in the Terraform state,
  `infra/terraform/envs/<env>/terraform.tfstate`, which is git-ignored. Keep that file private.
  To share an environment, move the state to a remote backend that encrypts at rest.
- **Real providers and other settings.** Pass them as a list:
  `TF_VAR_extra_env='["OPENAI_API_KEY=..."]'`. They are marked sensitive and never printed.
- **Single sign-on on staging.** It needs a public https origin:
  `TF_VAR_public_base_url=https://finops.example.com`.
- **The database.** It is never published to the host: only the gateway and the dashboard bind
  ports, on 127.0.0.1.

## What CI proves

The `terraform` workflow runs on every change to `infra/`, `scripts/env.sh` or itself. It:

1. checks formatting and validates both environments;
2. provisions `dev` from empty and checks it;
3. proves a second plan changes nothing;
4. destroys `dev` and proves nothing is left;
5. does the same for `staging`.

## Upgrading an environment

1. Build the new images with the same tag.
2. Run `scripts/env.sh up <env>` again.

The containers that run a changed image are replaced. The migrate container runs again first,
and a failed migration stops the apply before the gateway is replaced. Volumes, and with them
the database and the stored files, are kept.

Before an upgrade that changes the database, take a backup (`docs/runbooks/restore.md`).

## Scaling the gateway

- **One process per container.** Run each gateway container with `WEB_CONCURRENCY=1` (the
  default), and scale with replicas behind a proxy or load balancer that balances requests.
  Several uvicorn processes in one container share connections, not requests, so a few busy
  keep-alive connections can pin one process while another idles. The `load` workflow measures
  this topology: two replicas behind the dashboard's nginx.
- **Trust the proxy's address.** Add it to `FORWARDED_ALLOW_IPS`. Otherwise per-address limits
  on sign-in see the proxy as the only client.
- **Size the pools.** Each replica opens up to `DB_POOL_SIZE` plus `DB_MAX_OVERFLOW` connections
  (20 + 10 by default). Keep replicas × 30, plus the worker and the migrations, under
  PostgreSQL's `max_connections` (300 in compose).
- **One worker is enough** for the janitor and the outbox. A second is safe, because its jobs
  lock the rows they take (`FOR UPDATE SKIP LOCKED`).
