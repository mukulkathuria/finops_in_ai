"""US-25 in the graph (migration 0032, ADR 0010): the agents' communication is a graph of tasks,
agents, questions and facts, and get_task_context reads an agent's context from its relations.
Both engines pass the same test: SQL views walked by a recursive CTE, and Cypher on Apache AGE,
where each workspace is its own graph."""

from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql

from gateway.adapters.postgres.database import TenantContext, fetch_all
from tests.integration.test_us14_files import _team_key
from tests.integration.test_us25_workspace import Api, _task, _tool
from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase

_WHO_ASKED_WHOM = sql.SQL(
    "SELECT CAST(r.x AS text), CAST(r.y AS text) FROM ag_catalog.cypher({}, $$"
    " MATCH (a:node)-[x:link]->(q:node)-[y:link]->(b:node)"
    " WHERE x.kind = 'asked' AND y.kind = 'asked_of' RETURN a.name, b.name"
    " $$) AS r(x ag_catalog.agtype, y ag_catalog.agtype)"
)


async def _who_asked_whom(db: DisposableDatabase, org: UUID, graph: str) -> list[tuple[str, ...]]:
    """Read with Cypher as `app` in `org`: (asker, asked) of every question in the graph."""
    async with await psycopg.AsyncConnection.connect(db.app_url) as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.org_id', %s, true)", [str(org)])
        await conn.execute("SELECT set_config('search_path', 'ag_catalog, public', true)")
        rows = await (await conn.execute(_WHO_ASKED_WHOM.format(sql.Literal(graph)))).fetchall()
    return sorted(tuple(str(value).strip('"') for value in row) for row in rows)


async def _graphs_named(db: DisposableDatabase, graph: str) -> int:
    async with await psycopg.AsyncConnection.connect(db.app_url) as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", [graph]
        )
        row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


async def _communication(api: Api, age: DisposableDatabase | None) -> None:
    app, client = api
    admin = await signup(client)
    org = UUID(admin["org"]["id"])
    key = bearer(await _team_key(client, admin))
    made = await client.post("/v1/workspaces", json={"name": "label launch"}, headers=key)
    assert made.status_code == 200, made.text
    workspace = made.json()["id"]
    task = await _task(client, key, workspace, title="ship the returns label")
    scope = {"workspace_id": workspace, "task_id": task}

    async def call(name: str, **arguments: Any) -> dict[str, Any]:
        result = await _tool(client, key, name, {**scope, **arguments})
        assert result["isError"] is False, result
        return result

    async def asked(agent: str, to_agent: str, question: str, **more: Any) -> str:
        result = await call("ask_agent", agent=agent, to_agent=to_agent, question=question, **more)
        return str(result["structuredContent"]["message_id"])

    for agent in ("coder", "tester"):  # an agent is asked only once it has joined the task
        await call("read_inbox", agent=agent)
    q1 = await asked("planner", "coder", "Which label API do we use?")
    answer = {"workspace_id": workspace, "agent": "coder", "message_id": q1, "answer": "Parcel v2"}
    assert (await _tool(client, key, "answer_question", answer))["isError"] is False
    q2 = await asked("coder", "tester", "Is Parcel v2 in staging?", reply_to=q1)  # a follow-up
    q3 = await asked("ops", "tester", "What is the disk quota?")  # another conversation
    facts = {}
    for agent, content in (
        ("tester", "Parcel v2 runs in staging since Monday."),
        ("planner", "The returns label ships on Friday."),
        ("ops", "The disk quota is 50 GiB."),
    ):
        put = await call("put_fact", agent=agent, content=content)
        facts[agent] = put["structuredContent"]["fact_id"]

    planner = await call("get_task_context", agent="planner")
    assert planner["structuredContent"]["messages"] == [q1, q2]  # its question and the follow-up
    # the facts of the agents in its thread: ops talks with tester, but not in this thread
    assert set(planner["structuredContent"]["facts"]) == {facts["tester"], facts["planner"]}
    assert "Parcel v2" in planner["content"][0]["text"]
    assert planner["structuredContent"]["tokens"] > 0  # counted as discovery
    ops = await call("get_task_context", agent="ops")
    assert ops["structuredContent"]["messages"] == [q3]
    assert set(ops["structuredContent"]["facts"]) == {facts["ops"], facts["tester"]}

    async with app.state.container.db.transaction(TenantContext(org)) as conn:
        edges = await fetch_all(
            conn,
            "SELECT kind, count(*) AS n FROM workspace_graph_edge WHERE task_id = :task"
            " GROUP BY kind",
            {"task": UUID(task)},
        )
    assert {r["kind"]: r["n"] for r in edges} == {
        "member_of": 4,  # planner, coder, tester, ops
        "asked": 3,
        "asked_of": 3,
        "answered": 1,
        "replies_to": 1,
        "wrote": 3,
    }

    quiet = await call("get_task_context", agent="reviewer")
    assert quiet["structuredContent"]["messages"] == quiet["structuredContent"]["facts"] == []
    assert "Nothing in this task connects to you yet" in quiet["content"][0]["text"]
    other_team = bearer(await _team_key(client, admin))  # the workspace is not shared with it
    hidden = await _tool(client, other_team, "get_task_context", {**scope, "agent": "planner"})
    assert hidden["isError"] is True

    graph = "w" + UUID(workspace).hex
    if age is not None:  # the same graph in Apache AGE, read with Cypher
        assert await _who_asked_whom(age, org, graph) == [
            ("coder", "tester"),
            ("ops", "tester"),
            ("planner", "coder"),
        ]
        other_org = UUID((await signup(client))["org"]["id"])
        assert await _who_asked_whom(age, other_org, graph) == []  # row security
    gone = await client.delete(f"/v1/workspaces/{workspace}", headers=key)
    assert gone.status_code in (200, 204), gone.text
    if age is not None:
        assert await _graphs_named(age, graph) == 0  # dropped with its workspace


async def test_us25_an_agents_context_comes_from_the_communication_graph(api: Api) -> None:
    await _communication(api, None)


async def test_us25_on_age_the_communication_graph_answers_cypher(
    age_api: Api, age_db: DisposableDatabase
) -> None:
    await _communication(age_api, age_db)
