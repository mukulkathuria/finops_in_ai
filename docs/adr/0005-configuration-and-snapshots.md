# ADR 0005: Configuration layers and immutable snapshots

- Status: accepted
- Date: 2026-11-03
- Stories: US-06, US-16, US-20, US-26 (spec sections 3, 3A)

## Context
Backends are chosen by config at three levels (global, per org, per route or provider) and config
changes must not need a restart. Secrets live only in the environment.

## Decision
- `Settings` (pydantic-settings) reads the environment and `.env`: secrets, URLs and global switches
  such as `OCR_BACKEND`, `CLASSIFIER_BACKEND`, `PROVIDER_ADAPTER_DEFAULT`. It is frozen after start.
- `config/gateway.yaml` holds non-secret structure: providers, model routes, fallback chains, stage
  order and router tiers. Environment values win over YAML defaults where both exist.
- Per-org overrides live in the database (`orgs.settings`, policies, budgets).
- Policies, budgets, the price catalog and routing rules load into immutable snapshot objects that
  are swapped atomically (one reference assignment). Requests read the current snapshot without
  locks. An admin reload endpoint re-reads YAML and re-probes backends.
- In `production`, startup fails on unsafe defaults (missing keys, wildcard CORS, debug on).

## Consequences
- Every config value has one documented home (`.env.example` or `config/gateway.yaml`).
- Snapshot staleness is bounded by the reload trigger or the periodic refresh interval.
