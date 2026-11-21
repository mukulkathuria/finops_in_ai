# ADR 0010: Apache AGE as an opt-in graph engine

- Status: accepted
- Date: 2026-11-21
- Stories: US-22 (spec sections 2, 3A, 5)
- Amends: ADR 0004 (its "Apache AGE" alternative)

## Context
PostgreSQL 19.0 ships without SQL/PGQ (ADR 0004), so the graph store answers with joins and
recursive CTEs. The owner asked for graph queries in the same PostgreSQL database through
Apache AGE, the extension that adds openCypher.

Evidence gathered on 2026-11-21:

- AGE has published no release for PostgreSQL 19. The newest release, 1.8.0, targets PG18
  (<https://github.com/apache/age/releases>). The repository has a `PG19` branch and a
  `PG19/v1.8.0-rc0` tag (commit `d03d1cb0`), built against 19beta1. Its maintainers said PG19 work
  would start "when a stable release is ready" (<https://github.com/apache/age/discussions/2305>).
- In every released AGE version, variable-length traversal (`-[*1..n]->`) and shortest paths
  load the graph with a direct heap scan, so they ignore row-level security and table privileges.
  The fix, <https://github.com/apache/age/pull/2561> (2026-09-29, with follow-ups in October), is
  on the main branch only. AGE labels are also child tables of `_ag_label_vertex` and
  `_ag_label_edge`, and a query through a parent table does not apply its children's policies.
- AGE stores object ids in its catalog (`ag_graph`, `ag_label`), which a dump does not carry over.

The owner chose AGE now, opt-in, over waiting for an AGE release or returning to PostgreSQL 18.

## Decision
- **Build AGE into the database image, but leave it disabled.** The image compiles AGE from the
  pinned commit of the `PG19/v1.8.0-rc0` tag (`infra/docker/postgres/Dockerfile`).
  `infra/docker/postgres/sql/age-setup.sql`, run once by a superuser, turns it on:
  - it creates the extension;
  - it lets `migrator` create graph schemas;
  - it preloads AGE for `app` in that database;
  - it builds every project's graph.

  The gateway then runs with `GRAPH_ENGINE=age`. The default, `sql`, never loads AGE.
- **One AGE graph per project**, named `g` + the project id's hex, with one vertex label (`node`)
  and one edge label (`link`). A traversal cannot leave its project's graph, whatever AGE's
  traversal reads.
- **The SQL tables stay the source of truth.** `GraphRepository.replace_graph` writes
  `graph_vertex` and `graph_edge`, then `graph_age_sync` rebuilds the AGE graph from them, in the
  same transaction. Readers therefore see the old graph or the new one. The function belongs to
  `migrator` and runs with its rights (creating a graph creates a schema). Row security still
  limits it to the caller's org. Context packs and finding vertices by name stay in SQL.
- **Reads are Cypher, inside PL/pgSQL functions** (migration 0030). The functions quote every
  value in the database. The project's Semgrep rule forbids building SQL in Python, and AGE needs
  the graph name as a literal.
  - Fixed depth (1 to 3 hops) is one `MATCH` (`graph_age_hops`).
  - Reachability is breadth first, one `MATCH` per level (`graph_age_reach`). It stops after
    the level that reaches the caller's limit. This avoids `-[*1..n]->`, which enumerates every
    path and is exponential on a dense graph, and which ignores row security in released AGE.
- **Tenancy is kept three ways.**
  - The gateway checks the project's ACL in the same statement.
  - The functions return only vertex ids, which the gateway joins to `graph_vertex` under its
    row security.
  - Every AGE table, parents included, has a `tenant_isolation` policy: its project must be
    visible to the session's org.
- **AGE is a derived copy.** Backups leave it out, and after a restore or a major upgrade
  `age-setup.sql` rebuilds it (`docs/runbooks/restore.md`, `postgres-upgrade.md`).

## Consequences
- Both engines pass the same graph store tests (`tests/integration/test_us22_graph_store.py`).
  CI builds AGE in its database image and fails, rather than skips, if AGE is missing.
- One difference: a Cypher path never reuses an edge, and the joins can. With edges a→b and b→a,
  a 3-hop walk from `a` reaches `b` through the joins and nothing through Cypher. Code context
  reads one hop, where both engines agree.
- Production runs an unreleased build of AGE when `GRAPH_ENGINE=age`, so the default stays `sql`.
  - When Apache publishes an AGE release for PostgreSQL 19, re-pin it.
  - With the row-security fix of #2561 in that release, variable-length Cypher becomes an option
    for ad-hoc queries.
- The latency gate (`bench` workflow) measures the `sql` engine only.
- No route deletes a project. A project deleted by hand leaves its AGE graph behind, which its
  policy hides from `app`; remove it with `ag_catalog.drop_graph(name, true)`.
