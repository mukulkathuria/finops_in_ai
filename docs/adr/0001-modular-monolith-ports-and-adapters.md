# ADR 0001: Modular monolith with ports and adapters

- Status: accepted
- Date: 2026-11-03
- Stories: all (spec sections 2, 3, 3A)

## Context
The spec asks for a stateless gateway, a control plane, a worker and model sidecars, with every
swappable capability behind an interface, a registry, a config switch and contract tests. Section 2
asks for a modular monolith in v1 (gateway plus control plane in one deployable, split later only
if load requires it). Section 3A fixes the pattern: hexagonal core, registry and factory, a stage
pipeline, anti-corruption layer per vendor, immutable config snapshots, constructor injection and
enforced layers (`import-linter`, `mypy --strict`, `ruff`).

## Decision
One Python distribution (`finops-gateway`, Python 3.14) with three top-level packages:

| Package | Contents |
| --- | --- |
| `gateway.domain` | Pure logic (money, pricing, budgets, policy, cache keys, router rules). No I/O imports. |
| `gateway.ports` | `Protocol` interfaces for every swappable thing. Depends only on the domain. |
| `gateway.adapters` | Postgres, LLM providers, parsers, classifiers, caches, stores. Vendor SDKs stay here. |
| `gateway.pipeline` | The `Stage` contract runner and built-in stages. |
| `gateway.api` | Data-plane HTTP routes (OpenAI-compatible) and shared FastAPI dependencies. |
| `gateway.app` | Composition root: settings, registry, container, app factory. |
| `control_plane` | Admin API (orgs, teams, budgets, keys, reports, audit). |
| `worker` | Async jobs (outbox, janitor, rollups, parsing, indexing, price sync). |

`gateway.app.main` is the only module that mounts both the data plane and the control plane. The
contracts in `pyproject.toml` (`[tool.importlinter]`) fail CI on a forbidden import.

The spec directory `control-plane/` is named `control_plane/` because Python packages cannot
contain hyphens. Sidecars (`sidecars/*`) are separate deployables with their own dependencies so
heavy vendor stacks (vLLM, Paddle, torch) never enter the core lockfile.

The HTTP client is `httpx2` (Pydantic's maintained continuation of httpx, same API). The official
OpenAI 3.x and MCP 2.x SDKs already depend on it, so using it keeps one HTTP stack.

## Alternatives considered
- Separate distributions per deployable in a uv workspace: cleaner packaging, but the control plane
  needs the same domain, adapters and wiring, so it would be a third "core" package for no gain in v1.
- Microservices from day one: rejected by the spec (split only when load requires it).

## Consequences
- Splitting the control plane later means moving `gateway.app.main` wiring, not rewriting modules.
- Every new adapter is one file plus one registry line plus passing the family contract suite.

## Assumptions
- One process type (uvicorn) can host both API surfaces at v1 scale; revisit with load-test data.
