"""Phase 4 latency gate (spec section 11) on seeded data, through the gateway's own queries as
the `app` role under row-level security:

- code context pack read: p95 under 10 ms
- fixed-depth graph query of 2 and 3 hops: p95 under 30 ms
- filtered vector search at 1M chunks: p95 under 50 ms

    uv run python -m tests.load.bench_context [--chunks N] [--edges N] [--samples N] [--report F]

It seeds a disposable database (TEST_DATABASE_URL and the FINOPS_*_PASSWORD role passwords, as
for the integration tests): orgs, documents and chunks with clustered 1024-dimension vectors
(70% shared with the org, 30% with one team), and projects with their graph and one pack per
module. The chunk indexes are rebuilt after the load, as a bulk import would. Each query then runs
one at a time from a single client. The report gives p50, p95 and p99 with one EXPLAIN (ANALYZE,
BUFFERS) per query, and the exit code is 1 when a p95 misses its target. The full run is the
`bench` workflow; the integration tests run it at a tiny scale."""

import argparse
import asyncio
import hashlib
import json
import os
import random
import statistics
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg import sql
from sqlalchemy import text

from gateway.adapters.postgres import documents as documents_sql
from gateway.adapters.postgres import graph as graph_sql
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.documents import DocumentRepository
from gateway.adapters.postgres.graph import GraphRepository, PostgresGraphStore
from gateway.domain.retrieval import ORG, team_group
from tests.support.database import create_test_database, drop_test_database

TARGETS: Final = {
    "pack read": 10.0,
    "graph 2 hops": 30.0,
    "graph 3 hops": 30.0,
    "vector search": 50.0,
}
MODEL: Final = "bench-embedder"
DIMENSIONS: Final = 1024
CENTROIDS: Final = 256
CHUNKS_PER_DOCUMENT: Final = 10
OUT_DEGREE: Final = 4
BATCH_DOCUMENTS: Final = 5_000
KINDS: Final = ("calls", "imports")
GROUPS: Final = frozenset({ORG})

# The seed SQL uses mod(), not the % operator: psycopg reads a bare % as a placeholder. 70% of the
# documents are shared with their whole org, 30% with one of three teams of it.
_SEED_ORGS: Final = """
INSERT INTO orgs (id, name, slug)
SELECT md5('bench-org-' || i)::uuid, 'Bench org ' || i, 'bench-org-' || i
FROM generate_series(1, %(orgs)s) i
"""
_SEED_DOCUMENTS: Final = """
INSERT INTO documents (id, org_id, title, acl_groups, embedding_model, chunk_count, token_count)
SELECT md5('bench-doc-' || d)::uuid,
       md5('bench-org-' || (mod(d - 1, %(orgs)s) + 1))::uuid,
       'Document ' || d,
       CASE WHEN mod(d, 10) < 7 THEN ARRAY['org']
            ELSE ARRAY['team:' || md5('bench-team-' || (mod(d - 1, %(orgs)s) + 1)
                                       || '-' || mod(d, 3))::uuid::text]
       END,
       %(model)s, 10, 1200
FROM generate_series(1, %(documents)s) d
"""
_SEED_CENTROIDS: Final = """
CREATE TABLE bench_centroids AS
SELECT c,
       (SELECT array_agg(random() - 0.5)::real[] FROM generate_series(1, %(dims)s)
        WHERE c >= 0)::vector AS v
FROM generate_series(0, %(centroids)s - 1) c
"""
# A chunk lies near one of the centroids; its noise is drawn per row (the subquery is correlated).
_SEED_CHUNKS: Final = """
INSERT INTO chunks (id, org_id, document_id, ordinal, content, token_count, acl_groups,
                    embedding_model, embedding)
SELECT md5('bench-chunk-' || d || '-' || o)::uuid,
       md5('bench-org-' || (mod(d - 1, %(orgs)s) + 1))::uuid,
       md5('bench-doc-' || d)::uuid,
       o,
       'Passage ' || o || ' of document ' || d || '. ' || repeat('lorem ipsum dolor ', 30),
       120,
       CASE WHEN mod(d, 10) < 7 THEN ARRAY['org']
            ELSE ARRAY['team:' || md5('bench-team-' || (mod(d - 1, %(orgs)s) + 1)
                                       || '-' || mod(d, 3))::uuid::text]
       END,
       %(model)s,
       c.v + (SELECT array_agg((random() - 0.5) * 0.25)::real[]
              FROM generate_series(1, %(dims)s) WHERE d > 0 AND o >= 0)::vector
FROM generate_series(%(lo)s, %(hi)s) d
CROSS JOIN generate_series(0, 9) o
JOIN bench_centroids c ON c.c = mod(d * 7 + o, %(centroids)s)
"""
_SEED_PROJECTS: Final = """
INSERT INTO projects (id, org_id, name, acl_groups, indexed_at)
SELECT md5('bench-project-' || p)::uuid,
       md5('bench-org-' || (mod(p - 1, %(orgs)s) + 1))::uuid,
       'project ' || p, ARRAY['org'], now()
FROM generate_series(1, %(projects)s) p
"""
_SEED_VERTICES: Final = """
INSERT INTO graph_vertex (id, org_id, project_id, kind, name, path, props)
SELECT md5('bench-v-' || p || '-' || n)::uuid,
       md5('bench-org-' || (mod(p - 1, %(orgs)s) + 1))::uuid,
       md5('bench-project-' || p)::uuid,
       CASE WHEN n <= %(modules)s THEN 'module' ELSE 'symbol' END,
       CASE WHEN n <= %(modules)s THEN 'pkg.mod_' || n ELSE 'Cls_' || n || '.run' END,
       'pkg/mod_' || (mod(n - 1, %(modules)s) + 1) || '.py',
       jsonb_build_object('signature', 'def run(self, item: str) -> int')
FROM generate_series(1, %(projects)s) p CROSS JOIN generate_series(1, %(vertices)s) n
"""
_SEED_EDGES: Final = """
INSERT INTO graph_edge (org_id, project_id, src, dst, kind, confidence)
SELECT md5('bench-org-' || (mod(p - 1, %(orgs)s) + 1))::uuid,
       md5('bench-project-' || p)::uuid,
       md5('bench-v-' || p || '-' || n)::uuid,
       md5('bench-v-' || p || '-' || (mod(n * 7919 + j * 104729, %(vertices)s) + 1))::uuid,
       CASE WHEN n <= %(modules)s THEN 'imports' ELSE 'calls' END,
       CASE WHEN j = 3 THEN 0.6 ELSE 1.0 END
FROM generate_series(1, %(projects)s) p
CROSS JOIN generate_series(1, %(vertices)s) n
CROSS JOIN generate_series(0, %(degree)s - 1) j
"""
_SEED_PACKS: Final = """
INSERT INTO context_packs (org_id, project_id, module_id, path, pack, token_count, source_tokens)
SELECT md5('bench-org-' || (mod(p - 1, %(orgs)s) + 1))::uuid,
       md5('bench-project-' || p)::uuid,
       md5('bench-v-' || p || '-' || n)::uuid,
       'pkg/mod_' || n || '.py',
       '# pkg/mod_' || n || '.py' || repeat(chr(10) || '- def run(self, item: str) -> int', 40),
       500, 5000
FROM generate_series(1, %(projects)s) p CROSS JOIN generate_series(1, %(modules)s) n
"""
_CHUNK_INDEXES: Final = """
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'chunks' AND indexname <> 'chunks_pkey'
"""
_SIZES: Final = """
SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) AS size
FROM pg_class WHERE relname IN ('chunks', 'chunks_embedding', 'graph_edge', 'context_packs')
ORDER BY relname
"""


@dataclass(frozen=True, slots=True)
class Scale:
    orgs: int = 50
    chunks: int = 1_000_000
    edges: int = 1_000_000
    projects: int = 20
    samples: int = 1_000
    workers: int = 4  # parallel seeding connections

    @property
    def documents(self) -> int:
        return max(1, self.chunks // CHUNKS_PER_DOCUMENT)

    @property
    def vertices(self) -> int:
        return max(10, self.edges // self.projects // OUT_DEGREE)

    @property
    def modules(self) -> int:
        return max(1, self.vertices // 5)

    def params(self) -> dict[str, Any]:
        return {
            "orgs": self.orgs,
            "documents": self.documents,
            "projects": self.projects,
            "vertices": self.vertices,
            "modules": self.modules,
            "degree": OUT_DEGREE,
            "dims": DIMENSIONS,
            "centroids": CENTROIDS,
            "model": MODEL,
        }


@dataclass(frozen=True, slots=True)
class Result:
    query: str
    times_ms: tuple[float, ...]

    def percentile(self, p: int) -> float:
        return statistics.quantiles(self.times_ms, n=100, method="inclusive")[p - 1]

    @property
    def passed(self) -> bool:
        return self.percentile(95) < TARGETS[self.query]


def _uuid(seed: str) -> UUID:
    return UUID(hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest())


def _org(i: int) -> UUID:
    return _uuid(f"bench-org-{i}")


def _project(p: int) -> UUID:
    return _uuid(f"bench-project-{p}")


def _org_of_project(scale: Scale, p: int) -> UUID:
    return _org((p - 1) % scale.orgs + 1)


def _chunk_batch(url: str, params: dict[str, Any], lo: int, hi: int) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(_SEED_CHUNKS, {**params, "lo": lo, "hi": hi})


def seed(url: str, scale: Scale) -> dict[str, float]:
    """Load the seed data as the superuser (row-level security does not apply) and rebuild the
    chunk indexes afterwards. Returns the seconds each step took."""
    took: dict[str, float] = {}
    params = scale.params()
    with psycopg.connect(url, autocommit=True) as conn:
        start = time.perf_counter()
        conn.execute(_SEED_ORGS, params)
        conn.execute(_SEED_DOCUMENTS, params)
        conn.execute(_SEED_CENTROIDS, params)
        indexes = conn.execute(_CHUNK_INDEXES).fetchall()
        for name, _ in indexes:
            conn.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(name)))
        took["documents"] = time.perf_counter() - start
    start = time.perf_counter()
    batches = [
        (lo, min(lo + BATCH_DOCUMENTS - 1, scale.documents))
        for lo in range(1, scale.documents + 1, BATCH_DOCUMENTS)
    ]
    with ThreadPoolExecutor(max_workers=scale.workers) as pool:
        for done in [pool.submit(_chunk_batch, url, params, lo, hi) for lo, hi in batches]:
            done.result()
    took["chunks"] = time.perf_counter() - start
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SET max_parallel_maintenance_workers = 3")
        for name, definition in indexes:
            start = time.perf_counter()
            conn.execute(definition)
            took[f"index {name}"] = time.perf_counter() - start
        start = time.perf_counter()
        for statement in (_SEED_PROJECTS, _SEED_VERTICES, _SEED_EDGES, _SEED_PACKS):
            conn.execute(statement, params)
        took["graph and packs"] = time.perf_counter() - start
        start = time.perf_counter()
        conn.execute(
            "VACUUM (ANALYZE) orgs, documents, chunks, projects, graph_vertex, graph_edge,"
            " context_packs"
        )
        took["vacuum analyze"] = time.perf_counter() - start
    return took


def _centroids(url: str) -> list[list[float]]:
    with psycopg.connect(url) as conn:
        rows = conn.execute("SELECT v::text FROM bench_centroids ORDER BY c").fetchall()
    return [json.loads(row[0]) for row in rows]


async def _timed(run: Callable[[], Awaitable[None]], samples: int) -> tuple[float, ...]:
    for _ in range(max(10, samples // 10)):  # warm the pool, the plans and the caches
        await run()
    times = []
    for _ in range(samples):
        start = time.perf_counter()
        await run()
        times.append((time.perf_counter() - start) * 1000)
    return tuple(times)


async def _explain(
    db: Database, org: UUID, query: str, params: dict[str, Any], tune: Sequence[str] = ()
) -> str:
    async with db.transaction(TenantContext(org)) as conn:
        for statement in tune:
            await conn.execute(text(statement))
        rows = await conn.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + query), params)
        return "\n".join(str(r[0]) for r in rows)


async def measure(
    app_url: str, superuser_url: str, scale: Scale, rng: random.Random
) -> tuple[list[Result], dict[str, str]]:
    """Each query through the gateway's own code, one at a time, as the `app` role."""
    centroids = _centroids(superuser_url)
    db = Database(app_url, pool_size=2, max_overflow=0)
    store, repo, docs = PostgresGraphStore(db), GraphRepository(), DocumentRepository()

    def symbol(p: int) -> UUID:
        return _uuid(f"bench-v-{p}-{rng.randint(scale.modules + 1, scale.vertices)}")

    def question() -> tuple[int, frozenset[str], list[float]]:
        o = rng.randint(1, scale.orgs)
        team = _uuid(f"bench-team-{o}-{rng.randint(0, 2)}")
        centre = rng.choice(centroids)
        vector = [x + (rng.random() - 0.5) * 0.25 for x in centre]
        return o, frozenset({ORG, team_group(team)}), vector

    async def pack_read() -> None:
        p = rng.randint(1, scale.projects)
        paths = [f"pkg/mod_{rng.randint(1, scale.modules)}.py" for _ in range(3)]
        async with db.transaction(TenantContext(_org_of_project(scale, p))) as conn:
            if not await repo.packs(conn, _project(p), groups=GROUPS, paths=paths):
                raise RuntimeError("a seeded pack was not found")

    async def hops(depth: int) -> None:
        p = rng.randint(1, scale.projects)
        await store.neighbors(
            _org_of_project(scale, p),
            project_id=_project(p),
            groups=GROUPS,
            ids=[symbol(p)],
            kinds=KINDS,
            depth=depth,
            limit=200,
        )

    async def search() -> None:
        o, groups, vector = question()
        async with db.transaction(TenantContext(_org(o))) as conn:
            if not await docs.search(conn, vector, groups=groups, model=MODEL, k=5):
                raise RuntimeError("a seeded org returned no passage")

    try:
        results = [
            Result("pack read", await _timed(pack_read, scale.samples)),
            Result("graph 2 hops", await _timed(lambda: hops(2), scale.samples)),
            Result("graph 3 hops", await _timed(lambda: hops(3), scale.samples)),
            Result("vector search", await _timed(search, scale.samples)),
        ]
        p = rng.randint(1, scale.projects)
        org, project = _org_of_project(scale, p), _project(p)
        walk = {"project": project, "groups": sorted(GROUPS), "kinds": sorted(KINDS), "limit": 200}
        o, groups, vector = question()
        search_params = {
            "query": documents_sql._vector(vector),
            "groups": sorted(groups),
            "model": MODEL,
            "k": 5,
        }
        plans = {
            "pack read": await _explain(
                db,
                org,
                graph_sql._PACKS,
                {"project": project, "groups": sorted(GROUPS), "paths": ["pkg/mod_1.py"]},
            ),
            "graph 3 hops": await _explain(
                db, org, graph_sql._NEIGHBORS[("out", 3)], {**walk, "ids": [symbol(p)]}
            ),
            "vector search": await _explain(
                db, _org(o), documents_sql._SEARCH, search_params, documents_sql._TUNE
            ),
        }
    finally:
        await db.dispose()
    return results, plans


def _sizes(url: str) -> list[tuple[str, str]]:
    with psycopg.connect(url) as conn:
        return [(str(r[0]), str(r[1])) for r in conn.execute(_SIZES).fetchall()]


def report(
    scale: Scale,
    took: dict[str, float],
    results: Sequence[Result],
    plans: dict[str, str],
    sizes: Sequence[tuple[str, str]],
) -> str:
    lines = [
        "# Phase 4 latency gate (seeded)",
        "",
        f"Seed: {scale.orgs} orgs, {scale.chunks:,} chunks in {scale.documents:,} documents,"
        f" {scale.projects} projects with {scale.vertices * scale.projects:,} vertices,"
        f" {scale.vertices * scale.projects * OUT_DEGREE:,} edges and"
        f" {scale.modules * scale.projects:,} packs. One client, {scale.samples} samples a query.",
        "",
        "| Query | Target p95 | p50 | p95 | p99 | Result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {r.query} | {TARGETS[r.query]:.0f} ms | {r.percentile(50):.2f} ms"
        f" | {r.percentile(95):.2f} ms | {r.percentile(99):.2f} ms"
        f" | {'pass' if r.passed else 'MISS'} |"
        for r in results
    ]
    lines += ["", "| Seeding step | Seconds |", "| --- | --- |"]
    lines += [f"| {step} | {seconds:.1f} |" for step, seconds in took.items()]
    lines += ["", "| Relation | Size |", "| --- | --- |"]
    lines += [f"| {name} | {size} |" for name, size in sizes]
    for name, plan in plans.items():
        lines += ["", f"<details><summary>EXPLAIN {name}</summary>", "", "```", plan, "```"]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


async def run(scale: Scale, *, seed_value: int = 7) -> tuple[int, str]:
    """Seed a disposable database, measure, drop it. Returns the exit code and the report."""
    server = os.environ["TEST_DATABASE_URL"]
    db = create_test_database(server)
    try:
        took = await asyncio.to_thread(seed, db.superuser_url, scale)
        rng = random.Random(seed_value)
        results, plans = await measure(db.app_url, db.superuser_url, scale, rng)
        text_report = report(scale, took, results, plans, _sizes(db.superuser_url))
    finally:
        drop_test_database(server, db.name)
    return (0 if all(r.passed for r in results) else 1), text_report


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Phase 4 latency gate on seeded data")
    defaults = Scale()
    parser.add_argument("--orgs", type=int, default=defaults.orgs)
    parser.add_argument("--chunks", type=int, default=defaults.chunks)
    parser.add_argument("--edges", type=int, default=defaults.edges)
    parser.add_argument("--projects", type=int, default=defaults.projects)
    parser.add_argument("--samples", type=int, default=defaults.samples)
    parser.add_argument("--report", default=None, help="also write the report to this file")
    args = parser.parse_args(argv[1:])
    scale = Scale(args.orgs, args.chunks, args.edges, args.projects, args.samples)
    code, text_report = asyncio.run(run(scale))
    sys.stdout.write(text_report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as out:
            out.write(text_report)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
