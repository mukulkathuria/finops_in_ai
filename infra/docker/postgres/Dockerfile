# PostgreSQL 19.0 (released 2026-11-19, pinned by digest) + pgvector + pg_partman from PGDG.
# Re-pin on every minor release, then re-run the full suite (docs/runbooks/postgres-upgrade.md).
FROM postgres:19.0-trixie@sha256:bb6e70add742cd868fb62cb3b2f67d1dd2bdd2b65a521e778420748753687c60

ARG PGVECTOR_VERSION=0.8.7-1.pgdg13+2
ARG PARTMAN_VERSION=5.5.0-1.pgdg13+2

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      "postgresql-${PG_MAJOR}-pgvector=${PGVECTOR_VERSION}" \
      "postgresql-${PG_MAJOR}-partman=${PARTMAN_VERSION}" \
 && rm -rf /var/lib/apt/lists/*

COPY sql/ /opt/finops/sql/
COPY initdb/ /docker-entrypoint-initdb.d/
