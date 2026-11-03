# PostgreSQL 19 pre-release (pinned by digest) + pgvector + pg_partman from PGDG.
# Re-pin on every RC and on GA, then re-run the full suite (docs/runbooks/postgres-upgrade.md).
FROM postgres:19rc1-trixie@sha256:a4193f06aaf4cf10140225461dea49cf0d65a885221a07a90a9d410cdf469d7b

ARG PGVECTOR_VERSION=0.8.7-1.pgdg13+1
ARG PARTMAN_VERSION=5.5.0-1.pgdg13+1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      "postgresql-${PG_MAJOR}-pgvector=${PGVECTOR_VERSION}" \
      "postgresql-${PG_MAJOR}-partman=${PARTMAN_VERSION}" \
 && rm -rf /var/lib/apt/lists/*

COPY sql/ /opt/finops/sql/
COPY initdb/ /docker-entrypoint-initdb.d/
