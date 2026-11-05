# CI only: the database image with an older PostgreSQL installed beside 19, for the pg_upgrade
# rehearsal (scripts/upgrade_rehearsal.sh). pg_upgrade needs every extension in both
# installations, so the older one gets the same pinned pgvector and pg_partman.
ARG BASE=finops-postgres:ci
FROM ${BASE}

ARG OLD_MAJOR=18
ARG PGVECTOR_VERSION=0.8.7-1.pgdg13+1
ARG PARTMAN_VERSION=5.5.0-1.pgdg13+1

USER root
# The base image may list only its own major's packages (or the pre-release suite): add PGDG's
# stable suite, signed by the key the base image already trusts.
RUN echo "deb [ signed-by=/usr/local/share/keyrings/postgres.gpg.asc ] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
      > /etc/apt/sources.list.d/pgdg-stable.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      "postgresql-${OLD_MAJOR}" \
      "postgresql-${OLD_MAJOR}-pgvector=${PGVECTOR_VERSION}" \
      "postgresql-${OLD_MAJOR}-partman=${PARTMAN_VERSION}" \
 && rm -rf /var/lib/apt/lists/*
USER postgres
