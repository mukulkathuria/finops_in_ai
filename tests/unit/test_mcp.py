"""The MCP endpoint's protocol rules (2026-07-28, and the 2025 handshake) without a server."""

import base64
from types import SimpleNamespace
from typing import Any
from uuid import uuid7

import pytest

from gateway.api.mcp import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LEGACY,
    MODERN,
    PARSE_ERROR,
    UNSUPPORTED_VERSION,
    Rpc,
    RpcError,
    _Session,
    dispatch,
    header_value,
    parse,
    tool_list,
    version_of,
)
from gateway.domain.errors import NotFound

META = {"_meta": {"io.modelcontextprotocol/protocolVersion": MODERN}}


class _Unreadable:
    async def context(self, *args: Any, **kwargs: Any) -> None:
        raise NotFound("project not found", code="project_not_found")


class _Down:
    async def search(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("embedder down")


async def test_tool_failures_reach_the_model_and_unknown_tools_do_not() -> None:
    caller = SimpleNamespace(
        org_id=uuid7(), team_id=uuid7(), user_id=None, api_key_id=uuid7(), groups=frozenset()
    )
    session = _Session(caller, _Unreadable(), _Down())  # type: ignore[arg-type]

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        params = {"name": name, "arguments": arguments}
        return await dispatch(Rpc(1, "tools/call", params), MODERN, session)

    missing = await call("get_code_context", {"project_id": str(uuid7()), "question": "Cart"})
    assert missing["isError"] is True
    assert missing["content"][0]["text"] == "project not found"
    down = await call("search_documents", {"query": "returns"})  # an outage, not a crash
    assert down["isError"] is True
    assert "embedder" not in down["content"][0]["text"]  # internals stay in the log
    with pytest.raises(RpcError) as caught:
        await call("drop_tables", {})
    assert caught.value.code == INVALID_PARAMS


def _sentinel(value: str) -> str:
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def test_one_request_or_notification_is_parsed() -> None:
    assert parse(b'{"jsonrpc": "2.0", "id": 7, "method": "tools/list"}') == Rpc(7, "tools/list", {})
    assert parse(b'{"jsonrpc": "2.0", "method": "notifications/cancelled"}').id is None
    for body, code in (
        (b"{", PARSE_ERROR),
        (b"[]", INVALID_REQUEST),  # batches ended with 2025-06-18
        (b'{"jsonrpc": "1.0", "id": 1, "method": "x"}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": null, "method": "x"}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": true, "method": "x"}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "x", "params": []}', INVALID_REQUEST),
    ):
        with pytest.raises(RpcError) as caught:
            parse(body)
        assert (caught.value.status, caught.value.code) == (400, code)


def test_modern_requests_repeat_the_body_in_their_headers() -> None:
    call = Rpc(1, "tools/call", {**META, "name": "get_code_context"})
    headers = {
        "mcp-protocol-version": MODERN,
        "mcp-method": "tools/call",
        "mcp-name": "get_code_context",
    }
    assert version_of(headers, call) == MODERN
    assert version_of({**headers, "mcp-name": _sentinel("get_code_context")}, call) == MODERN
    for bad in (
        {**headers, "mcp-name": "search_documents"},
        {**headers, "mcp-method": "tools/list"},
        {k: v for k, v in headers.items() if k != "mcp-method"},
    ):
        with pytest.raises(RpcError) as caught:
            version_of(bad, call)
        assert (caught.value.status, caught.value.code) == (400, HEADER_MISMATCH)
    with pytest.raises(RpcError) as caught:  # the version is in the header but not in _meta
        version_of(headers, Rpc(1, "tools/call", {"name": "get_code_context"}))
    assert caught.value.code == HEADER_MISMATCH


def test_versions_are_negotiated() -> None:
    listing = Rpc(1, "tools/list", {})
    with pytest.raises(RpcError) as caught:
        version_of({"mcp-protocol-version": "1900-01-01"}, listing)
    assert (caught.value.code, caught.value.data["requested"]) == (
        UNSUPPORTED_VERSION,
        "1900-01-01",
    )
    assert MODERN in caught.value.data["supported"]
    assert version_of({"mcp-protocol-version": LEGACY[1]}, listing) == LEGACY[1]  # no _meta then
    assert version_of({}, Rpc(1, "initialize", {"protocolVersion": LEGACY[1]})) in LEGACY
    with pytest.raises(RpcError) as caught:
        version_of({}, listing)  # past the handshake the version header is required
    assert caught.value.code == HEADER_MISMATCH
    stale = {"mcp-protocol-version": LEGACY[0]}
    with pytest.raises(RpcError):  # a 2025 header with a 2026 body
        version_of(stale, Rpc(1, "tools/list", META))


def test_header_values_decode_from_the_base64_sentinel() -> None:
    assert header_value("plain") == "plain"
    assert header_value(_sentinel("Grüße")) == "Grüße"
    with pytest.raises(RpcError):
        header_value("=?base64?***?=")


def test_tool_schemas_come_from_the_validating_models() -> None:
    tools = {t["name"]: t for t in tool_list()}
    schema = tools["get_code_context"]["inputSchema"]
    assert schema["required"] == ["project_id", "question"]
    assert schema["additionalProperties"] is False
    assert tools["search_documents"]["inputSchema"]["properties"]["k"]["maximum"] == 50
