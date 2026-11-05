"""US-22: graph reads keep their limits, every graph query checks the project's ACL in its own
statement, and a graph is validated before it is written."""

from uuid import uuid7

import pytest

from gateway.adapters.postgres import graph as store
from gateway.domain.graph import Edge, Vertex, check_graph, check_read


def test_every_graph_query_checks_the_project_acl_in_the_same_statement() -> None:
    queries = [*store._NEIGHBORS.values(), *store._REACH.values(), store._FIND, store._PACKS]
    assert len(queries) == 10
    for sql in queries:
        assert "p.acl_groups && CAST(:groups AS text[])" in sql
        assert "project_id = :project" in sql
    for sql in (store._GET_PROJECT, store._LIST_PROJECTS):
        assert "p.acl_groups && CAST(:groups AS text[])" in sql


def test_reads_keep_their_limits() -> None:
    check_read(["imports", "calls"], 3, 10, max_depth=3)
    for kinds, depth, limit in (
        (["teleports"], 1, 10),
        ([], 1, 10),
        (["calls"], 4, 10),
        (["calls"], 0, 10),
        (["calls"], 1, 0),
        (["calls"], 1, 5_000),
    ):
        with pytest.raises(ValueError, match=r"edge kinds|depth|limit"):
            check_read(kinds, depth, limit, max_depth=3)


def test_a_graph_is_validated_before_it_is_written() -> None:
    a, b = Vertex(uuid7(), "module", "a"), Vertex(uuid7(), "module", "b")
    check_graph([a, b], [Edge(a.id, b.id, "imports"), Edge(b.id, a.id, "calls", 0.4)])
    for vertices, edges in (
        ([a, a], []),  # an id twice
        ([Vertex(uuid7(), "planet", "x")], []),
        ([a, b], [Edge(a.id, uuid7(), "imports")]),  # an edge out of the graph
        ([a, b], [Edge(a.id, b.id, "imports", 1.5)]),
        ([a, b], [Edge(a.id, b.id, "likes")]),
    ):
        with pytest.raises(ValueError, match=r"repeat|unknown|leaves|confidence"):
            check_graph(vertices, edges)


def test_reads_order_deterministically_and_whole_names_first() -> None:
    for sql in store._NEIGHBORS.values():
        assert "ORDER BY confidence DESC, name, id" in sql  # review C-L1
    for sql in store._REACH.values():
        assert "ORDER BY depth, v.name, v.id" in sql
    assert "ANY(CAST(:words AS text[])) DESC" in store._FIND  # review C-M6


async def test_a_walk_of_two_or_three_hops_starts_from_few_vertices() -> None:
    graph = store.PostgresGraphStore(None)  # refused before any query
    for depth in (2, 3):
        with pytest.raises(ValueError, match="at most"):
            await graph.neighbors(
                uuid7(),
                project_id=uuid7(),
                groups=frozenset({"org"}),
                ids=[uuid7() for _ in range(21)],
                kinds=["calls"],
                depth=depth,
            )
