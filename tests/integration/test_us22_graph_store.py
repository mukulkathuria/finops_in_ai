"""US-22 groundwork: the graph store answers fixed-depth patterns and reachability, ends cycles,
puts deterministic edges first, replaces a graph whole, and returns nothing of a project the
caller cannot read, or of another org. Both engines pass the same tests: joins and recursive CTEs
(`sql`, ADR 0004) and Cypher on Apache AGE (`age`, ADR 0010)."""

import asyncio
from uuid import UUID, uuid7

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from psycopg import sql

from gateway.adapters.postgres.database import TenantContext, fetch_one
from gateway.adapters.postgres.graph import GraphRepository
from gateway.domain.code import question_terms, question_words
from gateway.domain.graph import Edge, Vertex
from gateway.domain.retrieval import ORG, team_group
from tests.support.app import signup
from tests.support.database import DisposableDatabase

Api = tuple[FastAPI, httpx2.AsyncClient]


def _repo(app: FastAPI) -> GraphRepository:
    return GraphRepository(age=app.state.container.settings.graph_engine == "age")


async def _reads_within_the_project_acl(api: Api) -> None:
    app, client = api
    container = app.state.container
    org = UUID((await signup(client))["org"]["id"])
    other = UUID((await signup(client))["org"]["id"])
    team = uuid7()
    a, b, c, d = (Vertex(uuid7(), "module", name, f"{name}.py") for name in "abcd")
    edges = [
        Edge(a.id, b.id, "imports"),
        Edge(b.id, c.id, "imports"),
        Edge(c.id, a.id, "imports"),  # a cycle
        Edge(a.id, d.id, "calls", 0.5),  # inferred
    ]
    repo = _repo(app)
    async with container.db.transaction(TenantContext(org)) as conn:
        project = await repo.create_project(
            conn, name="core", groups=[team_group(team)], created_by=None
        )
        await repo.replace_graph(conn, project_id=project, vertices=[a, b, c, d], edges=edges)
    graph = container.graph_store
    readers = frozenset({team_group(team)})

    async def near(**read: object) -> list[tuple[str, float | None]]:
        found = await graph.neighbors(org, project_id=project, groups=readers, **read)
        return [(r.vertex.name, r.confidence) for r in found]

    assert await near(ids=[a.id], kinds=["imports"]) == [("b", 1.0)]
    assert await near(ids=[a.id], kinds=["imports"], depth=2) == [("c", 1.0)]
    assert await near(ids=[a.id], kinds=["imports"], depth=3) == [("a", 1.0)]  # the cycle
    assert await near(ids=[b.id], kinds=["imports"], direction="in") == [("a", 1.0)]
    both = await near(ids=[a.id], kinds=["imports", "calls"])
    assert both == [("b", 1.0), ("d", 0.5)]  # deterministic edges first

    reach = await graph.reachable(
        org, project_id=project, groups=readers, ids=[a.id], kinds=["imports", "calls"]
    )
    assert {r.vertex.name: r.depth for r in reach} == {"b": 1, "d": 1, "c": 2, "a": 3}
    sure = await graph.reachable(
        org,
        project_id=project,
        groups=readers,
        ids=[a.id],
        kinds=["imports", "calls"],
        min_confidence=0.9,
    )
    assert {r.vertex.name for r in sure} == {"a", "b", "c"}
    back = await graph.reachable(
        org, project_id=project, groups=readers, ids=[d.id], kinds=["calls"], direction="in"
    )
    assert [(r.vertex.name, r.depth) for r in back] == [("a", 1)]
    first = await graph.reachable(
        org, project_id=project, groups=readers, ids=[a.id], kinds=["imports"], limit=1
    )
    assert [(r.vertex.name, r.depth) for r in first] == [("b", 1)]

    for org_id, groups in ((org, frozenset({ORG})), (other, readers)):
        read = {"project_id": project, "groups": groups, "ids": [a.id], "kinds": ["imports"]}
        assert await graph.neighbors(org_id, **read) == []
        assert await graph.reachable(org_id, **read) == []

    async with container.db.transaction(TenantContext(org)) as conn:
        await repo.replace_graph(
            conn, project_id=project, vertices=[a, b], edges=[Edge(b.id, a.id, "imports")]
        )
    assert await near(ids=[a.id], kinds=["imports"]) == []  # replaced whole
    assert await near(ids=[b.id], kinds=["imports"]) == [("a", 1.0)]
    assert await graph.sql_pgq() is False  # PostgreSQL 19 ships without SQL/PGQ (ADR 0004)


async def _best_paths_find_order_indexes_and_concurrent_reindex(api: Api) -> None:
    app, client = api
    container = app.state.container
    org = UUID((await signup(client))["org"]["id"])
    team = uuid7()
    readers = frozenset({team_group(team)})
    a, b, c, d = (Vertex(uuid7(), "module", name, f"{name}.py") for name in "abcd")
    diamond = [
        Edge(a.id, b.id, "calls", 1.0),
        Edge(b.id, c.id, "calls", 0.3),  # a, b, c: its weakest edge is 0.3
        Edge(a.id, d.id, "calls", 0.9),
        Edge(d.id, c.id, "calls", 0.9),  # a, d, c: 0.9, the best path
    ]
    repo = _repo(app)
    async with container.db.transaction(TenantContext(org)) as conn:
        project = await repo.create_project(
            conn, name="diamond", groups=[team_group(team)], created_by=None
        )
        await repo.replace_graph(conn, project_id=project, vertices=[a, b, c, d], edges=diamond)
    best = await container.graph_store.neighbors(
        org, project_id=project, groups=readers, ids=[a.id], kinds=["calls"], depth=2, limit=1
    )
    assert [r.vertex.name for r in best] == ["c"]
    assert best[0].confidence == pytest.approx(0.9, abs=1e-6)

    question = "How does ChatService work?"
    parts = [Vertex(uuid7(), "symbol", "Chat", f"m{i}.py") for i in range(3)]  # sorts first
    whole = Vertex(uuid7(), "symbol", "ChatService", "z.py")
    async with container.db.transaction(TenantContext(org)) as conn:
        await repo.replace_graph(conn, project_id=project, vertices=[*parts, whole], edges=[])
        found = await repo.find(
            conn,
            project,
            groups=readers,
            terms=question_terms(question),
            words=question_words(question),
            kinds=["symbol"],
            limit=2,
        )
        index = await fetch_one(
            conn, "SELECT indexdef FROM pg_indexes WHERE indexname = 'graph_edge_out'"
        )
    assert found[0].name == "ChatService"
    assert index is not None
    assert "project_id" in index["indexdef"]

    async def reindex(name: str) -> None:
        async with container.db.transaction(TenantContext(org)) as conn:
            vertex = Vertex(uuid7(), "module", name, f"{name}.py")
            await repo.replace_graph(conn, project_id=project, vertices=[vertex], edges=[])

    await asyncio.gather(reindex("first"), reindex("second"))  # the project row lock serialises
    async with container.db.transaction(TenantContext(org)) as conn:
        left = await repo.find(
            conn,
            project,
            groups=readers,
            terms=frozenset({"first", "second"}),
            words=frozenset(),
            kinds=["module"],
            limit=10,
        )
    assert [v.name for v in left] in (["first"], ["second"])  # one whole graph, never a mix


async def test_the_graph_store_reads_within_the_project_acl(api: Api) -> None:
    await _reads_within_the_project_acl(api)


async def test_the_age_graph_store_reads_within_the_project_acl(age_api: Api) -> None:
    await _reads_within_the_project_acl(age_api)


async def test_review_best_paths_find_order_indexes_and_concurrent_reindex(api: Api) -> None:
    """Review C-L10: a vertex reached by two paths keeps the stronger one under LIMIT. C-M6: the
    names a question gives whole come before part matches under the find limit. C-L7: the edge
    indexes cover project_id. Two concurrent re-indexes leave one whole graph, never a mix."""
    await _best_paths_find_order_indexes_and_concurrent_reindex(api)


async def test_age_best_paths_find_order_indexes_and_concurrent_reindex(age_api: Api) -> None:
    await _best_paths_find_order_indexes_and_concurrent_reindex(age_api)


async def _cypher(db: DisposableDatabase, org: UUID, project: UUID) -> list[tuple[str, ...]]:
    """`(name)-[kind]->(name)` of the project's AGE graph, read with Cypher as `app` in `org`."""
    query = sql.SQL(
        "SELECT CAST(r.x AS text), CAST(r.k AS text), CAST(r.y AS text) FROM ag_catalog.cypher({},"
        " $$ MATCH (x:node)-[e:link]->(y:node) RETURN x.name, e.kind, y.name $$)"
        " AS r(x ag_catalog.agtype, k ag_catalog.agtype, y ag_catalog.agtype)"
    ).format(sql.Literal("g" + project.hex))
    async with await psycopg.AsyncConnection.connect(db.app_url) as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.org_id', %s, true)", [str(org)])
        rows = await (await conn.execute(query)).fetchall()
    return sorted(tuple(str(value).strip('"') for value in row) for row in rows)


async def test_each_project_is_an_age_graph_that_cypher_reads(
    age_api: Api, age_db: DisposableDatabase
) -> None:
    """ADR 0010: one AGE graph per project, kept with the SQL graph in the same transaction.
    Cypher reads it directly, row security hides it from other orgs, and readiness checks
    the extension."""
    app, client = age_api
    container = app.state.container
    org = UUID((await signup(client))["org"]["id"])
    other = UUID((await signup(client))["org"]["id"])
    module, run = Vertex(uuid7(), "module", "app", "app.py"), Vertex(uuid7(), "symbol", "app.run")
    defines = [Edge(module.id, run.id, "defines")]
    repo = _repo(app)
    async with container.db.transaction(TenantContext(org)) as conn:
        project = await repo.create_project(
            conn, name="cypher", groups=[team_group(uuid7())], created_by=None
        )
    assert await _cypher(age_db, org, project) == []  # created empty
    async with container.db.transaction(TenantContext(org)) as conn:
        await repo.replace_graph(conn, project_id=project, vertices=[module, run], edges=defines)
    assert await _cypher(age_db, org, project) == [("app", "defines", "app.run")]
    assert await _cypher(age_db, other, project) == []  # row security

    async with container.db.transaction(TenantContext(org)) as conn:
        await repo.replace_graph(conn, project_id=project, vertices=[module], edges=[])
    assert await _cypher(age_db, org, project) == []  # replaced whole

    resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"]["graph"] == "ok"
