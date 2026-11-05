"""US-22: the MCP context tools over Streamable HTTP. A 2026-07-28 client discovers the server,
lists the tools and calls them with headers that repeat the body; a 2025 client opens with
`initialize`. The tools read only what the credential may read."""

from typing import Any

import httpx2
from fastapi import FastAPI

from tests.integration.test_us14_files import _team_key
from tests.integration.test_us22_code_context import SOURCES, _upload
from tests.support.app import bearer, signup

Api = tuple[FastAPI, httpx2.AsyncClient]
MODERN = "2026-07-28"
ACCEPT = "application/json, text/event-stream"


def _rpc(method: str, params: dict[str, Any] | None = None, rid: int | None = 1) -> dict[str, Any]:
    meta = {"io.modelcontextprotocol/protocolVersion": MODERN}
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": {"_meta": meta}}
    body["params"].update(params or {})
    if rid is not None:
        body["id"] = rid
    return body


def _headers(
    key: dict[str, str], method: str, name: str | None = None, version: str = MODERN
) -> dict[str, str]:
    headers = {**key, "MCP-Protocol-Version": version, "Mcp-Method": method, "Accept": ACCEPT}
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


async def _tool(
    client: httpx2.AsyncClient, key: dict[str, str], name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    body = _rpc("tools/call", {"name": name, "arguments": arguments})
    resp = await client.post("/v1/mcp", json=body, headers=_headers(key, "tools/call", name))
    assert resp.status_code == 200, resp.text
    result: dict[str, Any] = resp.json()["result"]
    return result


async def test_us22_mcp_tools_answer_under_the_callers_acl(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    key = bearer(await _team_key(client, admin))
    project = (await client.post("/v1/projects", json={"name": "shop"}, headers=key)).json()["id"]
    file_id = await _upload(client, key, {"files": SOURCES})
    indexed = await client.post(
        f"/v1/projects/{project}/index", json={"file_id": file_id}, headers=key
    )
    assert indexed.status_code == 200, indexed.text
    doc = {"title": "Returns", "text": "Customers may return items within 30 days."}
    assert (await client.post("/v1/documents", json=doc, headers=key)).status_code == 200

    found = await client.post(
        "/v1/mcp", json=_rpc("server/discover"), headers=_headers(key, "server/discover")
    )
    assert found.status_code == 200, found.text
    discovered = found.json()["result"]
    assert MODERN in discovered["supportedVersions"]
    assert (discovered["capabilities"], discovered["resultType"]) == ({"tools": {}}, "complete")
    assert discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "finops-gateway"

    listed = await client.post(
        "/v1/mcp", json=_rpc("tools/list"), headers=_headers(key, "tools/list")
    )
    tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
    assert set(tools) == {"get_code_context", "search_documents"}

    question = "How does Cart compute its total?"
    context = await _tool(
        client, key, "get_code_context", {"project_id": project, "question": question}
    )
    assert context["isError"] is False
    assert "def total(self) -> int" in context["content"][0]["text"]
    assert context["structuredContent"]["modules"] == ["shop/cart.py"]
    assert context["structuredContent"]["tokens"] < context["structuredContent"]["source_tokens"]

    search = await _tool(client, key, "search_documents", {"query": "returning items", "k": 1})
    assert "30 days" in search["content"][0]["text"]

    other_team = bearer(await _team_key(client, admin))  # it cannot read the project or the doc
    hidden = await _tool(
        client, other_team, "get_code_context", {"project_id": project, "question": question}
    )
    assert hidden["isError"] is True
    unseen = await _tool(client, other_team, "search_documents", {"query": "returning items"})
    assert "30 days" not in unseen["content"][0]["text"]
    invalid = await _tool(client, key, "get_code_context", {"project_id": "nope", "question": "x"})
    assert invalid["isError"] is True  # the model can correct its arguments
    assert "project_id" in invalid["content"][0]["text"]


async def test_us22_mcp_checks_versions_headers_and_origins(api: Api) -> None:
    _, client = api
    key = bearer(await _team_key(client, await signup(client)))
    body = _rpc("tools/call", {"name": "get_code_context", "arguments": {}})
    mismatch = await client.post(
        "/v1/mcp", json=body, headers=_headers(key, "tools/call", "search_documents")
    )
    assert (mismatch.status_code, mismatch.json()["error"]["code"]) == (400, -32020)
    old = await client.post(
        "/v1/mcp",
        json=_rpc("tools/list"),
        headers=_headers(key, "tools/list", version="1900-01-01"),
    )
    assert (old.status_code, old.json()["error"]["code"]) == (400, -32022)
    assert MODERN in old.json()["error"]["data"]["supported"]
    unknown = await client.post(
        "/v1/mcp", json=_rpc("prompts/list"), headers=_headers(key, "prompts/list")
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, -32601)
    note = await client.post(
        "/v1/mcp",
        json=_rpc("notifications/cancelled", rid=None),
        headers=_headers(key, "notifications/cancelled"),
    )
    assert (note.status_code, note.content) == (202, b"")
    browser = await client.post(
        "/v1/mcp",
        json=_rpc("tools/list"),
        headers={**_headers(key, "tools/list"), "Origin": "https://pages.example"},
    )
    assert browser.status_code == 403
    nobody = await client.post(
        "/v1/mcp", json=_rpc("tools/list"), headers={"MCP-Protocol-Version": MODERN}
    )
    assert nobody.status_code == 401

    hello = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "older-client", "version": "1"},
    }
    legacy = await client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": hello},
        headers=key,
    )
    assert legacy.json()["result"]["protocolVersion"] == "2025-06-18"
    assert legacy.json()["result"]["capabilities"] == {"tools": {}}
    listed = await client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={**key, "MCP-Protocol-Version": "2025-06-18"},
    )
    assert len(listed.json()["result"]["tools"]) == 2


async def test_us22_mcp_tools_screen_passages_like_chat(api: Api) -> None:
    """Review S-L4: the tools' output reaches a model, so a passage the org's policy blocks on is
    dropped as it is for a chat request. S-I2: answers are not cached."""
    _, client = api
    key = bearer(await _team_key(client, await signup(client)))
    secret = "AK" + "IA" + "QWERTYUIOPASDFGH"
    for title, text in (
        ("Runbook", "Deploy with the blue pipeline."),
        ("Old", f"deploy: {secret}"),
    ):
        made = await client.post("/v1/documents", json={"title": title, "text": text}, headers=key)
        assert made.status_code == 200, made.text
    body = _rpc(
        "tools/call", {"name": "search_documents", "arguments": {"query": "How do we deploy?"}}
    )
    resp = await client.post(
        "/v1/mcp", json=body, headers=_headers(key, "tools/call", "search_documents")
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    result = resp.json()["result"]
    assert "blue pipeline" in result["content"][0]["text"]
    assert secret not in resp.text  # dropped before any model could see it
    assert result["structuredContent"]["dropped"] == 1
