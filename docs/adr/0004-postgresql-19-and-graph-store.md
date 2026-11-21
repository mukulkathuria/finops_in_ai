# ADR 0004: PostgreSQL 19, and the graph store without SQL/PGQ

- Status: accepted
- Date: 2026-11-03
- Stories: US-22, US-25 (spec sections 2, 3A, 5, 8 phase 4)

## Context
The spec says to develop on the PostgreSQL 19 pre-release and to write graph reads behind a
`GraphStore` port with two implementations: recursive CTEs (variable depth) and `Pgq19GraphStore`
using SQL/PGQ (`CREATE PROPERTY GRAPH`, `GRAPH_TABLE ... MATCH`) for fixed-depth patterns.

Evidence gathered on 2026-11-03 against the official Docker images:

| Image | `CREATE PROPERTY GRAPH` | `pg_propgraph*` catalogs |
| --- | --- | --- |
| `postgres:19beta1-trixie` | parsed (feature present) | 16 |
| `postgres:19beta4-trixie` | `syntax error at or near "PROPERTY"` | 0 |
| `postgres:19rc1-trixie` (sha256:a4193f06...) | `syntax error at or near "PROPERTY"` | 0 |

The official PostgreSQL 19 release notes do not list property graphs. SQL/PGQ was therefore
reverted during the 19 beta cycle; it is absent from RC1 and cannot return in GA (release
candidates do not add features). The blog post cited by the spec describes beta behaviour.

## Decision
- Pin `postgres:19rc1-trixie` by digest with PGDG `pgvector 0.8.7` and `pg_partman 5.5.0`
  (`infra/docker/postgres/Dockerfile`). Move to RC2 and GA when published and re-run everything.
- `GraphStore` is the only way business code reads the graph. The shipped implementation
  answers fixed-depth patterns with explicit indexed joins (what the PostgreSQL SQL/PGQ rewriter
  produced) and variable depth with recursive CTEs. It runs on PG16+ including PG19.
- A capability probe (`to_regclass('pg_catalog.pg_propgraph_element')`) runs at startup and is
  reported by `/readyz`. When a future PostgreSQL release ships SQL/PGQ, a `SqlPgqGraphStore` can be
  registered behind the same port and selected only where the probe is true. We do not ship that
  implementation now: it could not be executed on any supported server, so it would be untested.

### Confirmation (2026-11-04)
Primary sources confirm the probe: Peter Eisentraut committed "Revert SQL Property Graph Queries
(SQL/PGQ)" to `REL_19_STABLE` on 2026-09-07
(<https://www.postgresql.org/message-id/E1x3f62-00000003sxD-0vdk@gemulon.postgresql.org>), and
Daniel Gustafsson confirmed on pgsql-general on 2026-10-07 that SQL/PGQ will not ship in
PostgreSQL 19 (<https://www.postgresql.org/message-id/D84BE2C2-6DD9-4FDB-BCC2-F64866BCAC49%40yesql.se>);
it is expected back in PostgreSQL 20. A January 2026 hackers report of `GRAPH_TABLE` "bypassing
row-level security" turned out to be table inheritance (a child table's policies do not apply
through its parent), which our graph tables do not use; the cross-tenant graph tests still run
against every `GraphStore`.

### PostgreSQL 19.0 (2026-11-21)
PostgreSQL 19 was released on 2026-11-19 (<https://www.postgresql.org/about/news/postgresql-19-released-3449/>).
Its release notes (<https://www.postgresql.org/docs/19/release-19.html>) do not mention SQL/PGQ,
property graphs or `GRAPH_TABLE`, so 19.0 ships without them, as this ADR expected. The decision
stands:
- The image is re-pinned to `postgres:19.0-trixie` (`sha256:bb6e70ad...`), with the PGDG
  rebuilds of the same pgvector 0.8.7 and pg_partman 5.5.0.
- RC1, RC2 and 19.0 share catalog version `202610061`, so environments created on RC1 move by a
  swap of binaries (`docs/runbooks/postgres-upgrade.md`).
- `test_the_graph_store_reads_within_the_project_acl` (`tests/integration/test_us22_graph_store.py`)
  now runs on 19.0 and asserts the probe finds no SQL/PGQ. When a release ships it, that test fails first, and a
  `SqlPgqGraphStore` can then be added behind the port, gated by the contract suite.

## Alternatives considered
- Develop on `19beta1` to keep SQL/PGQ: rejected, it is an abandoned pre-release behaviour.
- Apache AGE: a second query language and extension to operate; the spec mandates PostgreSQL-only
  SQL and an interface we can already satisfy with joins and CTEs.

## Consequences
- Graph latency depends on the covering indexes on both edge directions and on precomputed
  context packs (single-row reads), exactly as the speed plan in the spec says.
- The `GraphStore` contract suite is the acceptance gate for any future SQL/PGQ implementation.
