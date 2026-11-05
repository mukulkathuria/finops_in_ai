"""US-25: a shared workspace for agents. A fact agent A records is retrievable by agent B without
re-search; repeated identical questions are deduplicated; loops and long clarification chains
are refused; each agent keeps within its discovery budget; discovery tokens per task are
reported, and drop against a task that works without the workspace."""

from typing import Any

import httpx2
from fastapi import FastAPI

from tests.integration.test_us14_files import _team_key
from tests.support.app import bearer, signup

Api = tuple[FastAPI, httpx2.AsyncClient]
MODERN = "2026-07-28"
POLICY = (
    "Returns policy. Customers may return items within 30 days of delivery for a full refund."
    " Items must be unused and in their original packaging. Refunds go to the original payment"
    " method within five business days. Sale items can be exchanged but not refunded. Return"
    " shipping is free for members and costs a flat fee for everyone else."
)


async def _tool(
    client: httpx2.AsyncClient, key: dict[str, str], name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    meta = {"io.modelcontextprotocol/protocolVersion": MODERN}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"_meta": meta, "name": name, "arguments": arguments},
    }
    headers = {**key, "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": name}
    resp = await client.post("/v1/mcp", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    result: dict[str, Any] = resp.json()["result"]
    return result


async def _report(
    client: httpx2.AsyncClient, key: dict[str, str], workspace: str, task: str
) -> dict[str, Any]:
    resp = await client.get(f"/v1/workspaces/{workspace}/tasks/{task}", headers=key)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _task(
    client: httpx2.AsyncClient, key: dict[str, str], workspace: str, **body: Any
) -> str:
    resp = await client.post(f"/v1/workspaces/{workspace}/tasks", json=body, headers=key)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


async def test_us25_agents_share_facts_and_discovery_drops_against_the_baseline(
    api: Api,
) -> None:
    _, client = api
    key = bearer(await _team_key(client, await signup(client)))
    for i in range(3):
        doc = {"title": f"Returns {i}", "text": f"{POLICY} Edition {i}."}
        assert (await client.post("/v1/documents", json=doc, headers=key)).status_code == 200
    made = await client.post("/v1/workspaces", json={"name": "support research"}, headers=key)
    assert made.status_code == 200, made.text
    workspace = made.json()["id"]
    baseline = await _task(client, key, workspace, title="returns, without the workspace")
    shared = await _task(client, key, workspace, title="returns, with the workspace")

    research = {"query": "returns policy refund", "k": 3, "workspace_id": workspace}
    for agent in ("agent-a", "agent-b"):  # both research the same thing from the documents
        await _tool(
            client, key, "search_documents", {**research, "task_id": baseline, "agent": agent}
        )

    await _tool(
        client, key, "search_documents", {**research, "task_id": shared, "agent": "agent-a"}
    )
    fact = {
        "workspace_id": workspace,
        "agent": "agent-a",
        "task_id": shared,
        "key": "returns-window",
        "content": "Customers may return items within 30 days of delivery.",
        "source": "documents: Returns",
    }
    recorded = await _tool(client, key, "put_fact", fact)
    assert recorded["structuredContent"]["deduplicated"] is False
    again = await _tool(client, key, "put_fact", fact)  # the same fact is kept once
    assert again["structuredContent"] == {**recorded["structuredContent"], "deduplicated": True}

    reader = {"workspace_id": workspace, "agent": "agent-b", "task_id": shared}
    got = await _tool(client, key, "get_fact", {**reader, "key": "returns-window"})
    assert "30 days" in got["content"][0]["text"]
    query = "how long can customers return items"
    found = await _tool(client, key, "search_workspace", {**reader, "query": query})
    assert "30 days" in found["content"][0]["text"]
    assert found["structuredContent"]["deduplicated"] is False
    repeat = await _tool(client, key, "search_workspace", {**reader, "query": query})
    assert repeat["structuredContent"]["deduplicated"] is True  # answered from the record
    assert repeat["structuredContent"]["tokens"] < found["structuredContent"]["tokens"]

    without = await _report(client, key, workspace, baseline)
    with_workspace = await _report(client, key, workspace, shared)
    assert with_workspace["discovery_tokens"] < without["discovery_tokens"]
    assert {a["agent"] for a in without["agents"]} == {"agent-a", "agent-b"}
    assert with_workspace["facts"] == 1

    stranger = bearer(await _team_key(client, await signup(client)))  # another org
    hidden = await _tool(client, stranger, "get_fact", {**reader, "key": "returns-window"})
    assert hidden["isError"] is True
    assert (
        await client.get(f"/v1/workspaces/{workspace}/tasks/{shared}", headers=stranger)
    ).status_code == 404


async def test_us25_questions_are_deduplicated_and_loops_hops_and_budgets_stop(api: Api) -> None:
    _, client = api
    key = bearer(await _team_key(client, await signup(client)))
    workspace = (
        await client.post("/v1/workspaces", json={"name": "coordination"}, headers=key)
    ).json()["id"]
    task = await _task(client, key, workspace, title="ship the returns label")
    ask = {"workspace_id": workspace, "task_id": task, "to_agent": "carrier-expert"}
    question = "Which carrier ships returns?"

    first = await _tool(client, key, "ask_agent", {**ask, "agent": "agent-a", "question": question})
    assert first["structuredContent"]["status"] == "asked"
    second = await _tool(
        client, key, "ask_agent", {**ask, "agent": "agent-b", "question": question}
    )
    message = first["structuredContent"]["message_id"]
    assert second["structuredContent"]["message_id"] == message  # one message, not two
    assert second["structuredContent"]["status"] == "pending"
    answer = {"workspace_id": workspace, "agent": "carrier-expert", "message_id": message}
    answered = await _tool(client, key, "answer_question", {**answer, "answer": "Parcel Co."})
    assert answered["isError"] is False
    late = await _tool(client, key, "ask_agent", {**ask, "agent": "agent-c", "question": question})
    assert late["structuredContent"]["status"] == "answered"
    assert "answer: Parcel Co." in late["content"][0]["text"]  # framed, never bare
    wrong = await _tool(
        client, key, "answer_question", {**answer, "agent": "agent-a", "answer": "x"}
    )
    assert wrong["isError"] is True  # only the agent asked may answer

    loop = {**ask, "agent": "agent-a", "question": "Is the label printed yet?"}
    assert (await _tool(client, key, "ask_agent", loop))["isError"] is False
    assert (await _tool(client, key, "ask_agent", loop))["isError"] is False
    looping = await _tool(client, key, "ask_agent", loop)
    assert looping["isError"] is True
    assert "looping" in looping["content"][0]["text"]

    chain = {**ask, "agent": "agent-a"}
    parent = None
    for hop in range(1, 5):
        step = await _tool(
            client, key, "ask_agent", {**chain, "question": f"Follow-up {hop}?", "reply_to": parent}
        )
        if hop <= 3:
            assert step["structuredContent"]["hops"] == hop
            parent = step["structuredContent"]["message_id"]
        else:
            assert step["isError"] is True  # the chain stops at three hops
            assert "hops" in step["content"][0]["text"]

    small = await _task(client, key, workspace, title="tight", agent_token_budget=30)
    note = {"workspace_id": workspace, "agent": "agent-a", "key": "long", "content": "word " * 400}
    await _tool(client, key, "put_fact", note)
    over = await _tool(
        client,
        key,
        "get_fact",
        {"workspace_id": workspace, "agent": "agent-b", "task_id": small, "key": "long"},
    )
    assert over["isError"] is True  # past agent-b's discovery budget on the task
    assert (await _report(client, key, workspace, small))["discovery_tokens"] == 0
