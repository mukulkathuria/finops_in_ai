"""US-22 groundwork: the graph store answers fixed-depth patterns with joins and reachability with
a recursive CTE, ends cycles, puts deterministic edges first, replaces a graph whole, and returns
nothing of a project the caller cannot read, or of another org."""

import asyncio
from uuid import UUID, uuid7

import httpx2
import pytest
from fastapi import FastAPI

from gateway.adapters.postgres.database import TenantContext, fetch_one
from gateway.adapters.postgres.graph import GraphRepository
from gateway.domain.code import question_terms, question_words
from gateway.domain.graph import Edge, Vertex
from gateway.domain.retrieval import ORG, team_group
from tests.support.app import signup

Api = tuple[FastAPI, httpx2.AsyncClient]


async def test_the_graph_store_reads_within_the_project_acl(api: Api) -> None:
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
    repo = GraphRepository()
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


async def test_review_best_paths_find_order_indexes_and_concurrent_reindex(api: Api) -> None:
    """Review C-L10: a vertex reached by two paths keeps the stronger one under LIMIT. C-M6: the
    names a question gives whole come before part matches under the find limit. C-L7: the edge
    indexes cover project_id. Two concurrent re-indexes leave one whole graph, never a mix."""
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
    repo = GraphRepository()
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
