# ADR 0004: PostgreSQL 19 RC, and the graph store without SQL/PGQ

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

## Alternatives considered
- Develop on `19beta1` to keep SQL/PGQ: rejected, it is an abandoned pre-release behaviour.
- Apache AGE: a second query language and extension to operate; the spec mandates PostgreSQL-only
  SQL and an interface we can already satisfy with joins and CTEs.

## Consequences
- Graph latency depends on the covering indexes on both edge directions and on precomputed
  context packs (single-row reads), exactly as the speed plan in the spec says.
- The `GraphStore` contract suite is the acceptance gate for any future SQL/PGQ implementation.
