"""US-22 groundwork: the graph store answers fixed-depth patterns with joins and reachability with
a recursive CTE, ends cycles, puts deterministic edges first, replaces a graph whole, and returns
nothing of a project the caller cannot read, or of another org."""

from uuid import UUID, uuid7

import httpx2
from fastapi import FastAPI

from gateway.adapters.postgres.database import TenantContext
from gateway.adapters.postgres.graph import GraphRepository
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
