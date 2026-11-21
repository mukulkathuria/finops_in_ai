# PostgreSQL 19.0 (released 2026-11-19, pinned by digest) + pgvector + pg_partman from PGDG.
# Re-pin on every minor release, then re-run the full suite (docs/runbooks/postgres-upgrade.md).
ARG POSTGRES_IMAGE=postgres:19.0-trixie@sha256:bb6e70add742cd868fb62cb3b2f67d1dd2bdd2b65a521e778420748753687c60

# Apache AGE (ADR 0010), built from source: AGE has published no release for PostgreSQL 19, so
# this pins the commit of its PG19/v1.8.0-rc0 tag. Installed, not enabled: age-setup.sql enables
# it on a database. The server headers follow the PGDG minor; extensions build across minors.
FROM ${POSTGRES_IMAGE} AS age
ARG AGE_COMMIT=d03d1cb07aab5919bb6b6019863143d0e46db024
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bison build-essential ca-certificates flex git "postgresql-server-dev-${PG_MAJOR}" \
 && git init --quiet /age \
 && git -C /age fetch --quiet --depth 1 https://github.com/apache/age.git "${AGE_COMMIT}" \
 && git -C /age checkout --quiet FETCH_HEAD \
 && make -C /age with_llvm=no \
 && make -C /age with_llvm=no install DESTDIR=/out

FROM ${POSTGRES_IMAGE}

ARG PGVECTOR_VERSION=0.8.7-1.pgdg13+2
ARG PARTMAN_VERSION=5.5.0-1.pgdg13+2

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      "postgresql-${PG_MAJOR}-pgvector=${PGVECTOR_VERSION}" \
      "postgresql-${PG_MAJOR}-partman=${PARTMAN_VERSION}" \
 && rm -rf /var/lib/apt/lists/*

COPY --from=age /out/ /
COPY sql/ /opt/finops/sql/
COPY initdb/ /docker-entrypoint-initdb.d/
