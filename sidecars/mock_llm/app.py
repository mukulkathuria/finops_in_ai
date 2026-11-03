"""Mock OpenAI-compatible LLM provider with configurable latency and fault injection.

Used by the dev stack, by end-to-end tests (in-process through an ASGI transport) and by load
tests, so they measure the gateway and the database, not a real provider (spec section 11).

Faults are requested in the prompt itself, so they pass through the gateway untouched:
`[[mock:rate_limit]]`, `[[mock:server_error]]`, `[[mock:timeout]]`, `[[mock:content_filter]]`,
`[[mock:ctx_overflow]]`, `[[mock:auth]]`, `[[mock:malformed_stream]]`, `[[mock:tool_call]]`,
`[[mock:no_usage]]` (stream without a usage chunk), `[[mock:slow:<ms>]]`.
"""

import asyncio
import base64
import hashlib
import math
import os
import random
import re
import struct
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

CHARS_PER_TOKEN = 4
LATENCY_MIN_MS = int(os.environ.get("MOCK_LATENCY_MS_MIN", "0"))
LATENCY_MAX_MS = int(os.environ.get("MOCK_LATENCY_MS_MAX", "0"))
CHUNK_DELAY_MS = int(os.environ.get("MOCK_STREAM_CHUNK_DELAY_MS", "0"))
MODELS = ("mock-small", "mock-large", "mock-embed", "mock-image")
_FAULT = re.compile(r"\[\[mock:([a-z_]+)(?::(\d+))?\]\]")
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_FAULT_RESPONSES: dict[str, tuple[int, str, str, str]] = {
    "rate_limit": (429, "rate_limit_exceeded", "Rate limit reached for requests", "requests"),
    "server_error": (500, "server_error", "The server had an error", "server_error"),
    "content_filter": (
        400,
        "content_policy_violation",
        "Request rejected by the content filter",
        "invalid_request_error",
    ),
    "ctx_overflow": (
        400,
        "context_length_exceeded",
        "This model's maximum context length is 8192 tokens",
        "invalid_request_error",
    ),
    "auth": (401, "invalid_api_key", "Incorrect API key provided", "invalid_request_error"),
}

app = FastAPI(title="mock-llm", docs_url=None, redoc_url=None, openapi_url=None)


def tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def _json(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(orjson.dumps(payload), status, headers, media_type="application/json")


def _error(status: int, code: str, message: str, etype: str) -> Response:
    headers = {"retry-after": "1"} if status == 429 else None
    error = {"error": {"message": message, "type": etype, "param": None, "code": code}}
    return _json(error, status, headers)


async def _latency(extra_ms: int = 0) -> None:
    upper = max(LATENCY_MIN_MS, LATENCY_MAX_MS)
    ms = random.randint(LATENCY_MIN_MS, upper) + extra_ms  # noqa: S311 - jitter, not crypto
    if ms > 0:
        await asyncio.sleep(ms / 1000)


def _authorized(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    return auth.startswith("Bearer ") and len(auth) > len("Bearer ")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    data = [{"id": m, "object": "model", "owned_by": "mock"} for m in MODELS]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> Response:
    if not _authorized(request):
        return _error(401, "invalid_api_key", "Missing bearer token", "invalid_request_error")
    body = orjson.loads(await request.body())
    messages = body.get("messages") or []
    prompt = "\n".join(_text(m.get("content")) for m in messages)
    faults = {m.group(1): m.group(2) for m in _FAULT.finditer(prompt)}
    for kind, spec in _FAULT_RESPONSES.items():
        if kind in faults:
            return _error(*spec)
    if "timeout" in faults:
        await asyncio.sleep(3600)
    await _latency(int(faults.get("slow") or 0))

    model = body.get("model", "mock-small")
    last_user = next(
        (_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), ""
    )
    reply = f"Mock answer from {model}: {last_user[:160]}"
    finish = "stop"
    limit = body.get("max_completion_tokens") or body.get("max_tokens")
    if isinstance(limit, int) and tokens(reply) > limit:
        reply, finish = reply[: limit * CHARS_PER_TOKEN], "length"
    tool_call = None
    if "tool_call" in faults and body.get("tools"):
        name = body["tools"][0]["function"]["name"]
        tool_call = {
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "name": name,
            "arguments": '{"q": "mock"}',
        }
        reply, finish = "", "tool_calls"
    usage: dict[str, Any] = {
        "prompt_tokens": tokens(prompt) + 3 * len(messages),
        "completion_tokens": tokens(reply) + (8 if tool_call else 0),
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        stream = _stream(
            completion_id,
            created,
            model,
            reply,
            finish,
            usage if include_usage and "no_usage" not in faults else None,
            tool_call,
            malformed="malformed_stream" in faults,
        )
        return StreamingResponse(stream, media_type="text/event-stream")
    message: dict[str, Any] = {"role": "assistant", "content": reply or None}
    if tool_call:
        message["tool_calls"] = [
            {
                "id": tool_call["id"],
                "type": "function",
                "function": {"name": tool_call["name"], "arguments": tool_call["arguments"]},
            }
        ]
    return _json(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        }
    )


async def _stream(
    cid: str,
    created: int,
    model: str,
    reply: str,
    finish: str,
    usage: dict[str, Any] | None,
    tool_call: dict[str, str] | None,
    *,
    malformed: bool,
) -> AsyncIterator[bytes]:
    def frame(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return b"data: " + orjson.dumps(chunk) + b"\n\n"

    yield frame({"role": "assistant", "content": ""})
    for i in range(0, len(reply), 8):
        if CHUNK_DELAY_MS:
            await asyncio.sleep(CHUNK_DELAY_MS / 1000)
        yield frame({"content": reply[i : i + 8]})
        if malformed:
            yield b"data: {not json\n\n"
            return
    if tool_call:
        head = {"index": 0, "id": tool_call["id"], "type": "function"}
        yield frame(
            {"tool_calls": [{**head, "function": {"name": tool_call["name"], "arguments": ""}}]}
        )
        args = tool_call["arguments"]
        for i in range(0, len(args), 6):
            yield frame({"tool_calls": [{"index": 0, "function": {"arguments": args[i : i + 6]}}]})
    yield frame({}, finish)
    if usage is not None:
        tail = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": usage,
        }
        yield b"data: " + orjson.dumps(tail) + b"\n\n"
    yield b"data: [DONE]\n\n"


def embedding_for(text: str, dims: int) -> list[float]:
    """Deterministic unit vector: equal inputs give equal vectors."""
    out: list[float] = []
    counter = 0
    while len(out) < dims:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        out.extend(v / 2**31 for v in struct.unpack(">8i", digest))
        counter += 1
    vec = out[:dims]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@app.post("/v1/embeddings", response_model=None)
async def embeddings(request: Request) -> Response:
    if not _authorized(request):
        return _error(401, "invalid_api_key", "Missing bearer token", "invalid_request_error")
    body = orjson.loads(await request.body())
    raw = body.get("input", "")
    inputs = [raw] if isinstance(raw, str) else list(raw)
    dims = int(body.get("dimensions") or 1024)
    await _latency()
    used = sum(tokens(str(t)) for t in inputs)
    data = [
        {"object": "embedding", "index": i, "embedding": embedding_for(str(t), dims)}
        for i, t in enumerate(inputs)
    ]
    return _json(
        {
            "object": "list",
            "model": body.get("model", "mock-embed"),
            "data": data,
            "usage": {"prompt_tokens": used, "total_tokens": used},
        }
    )


@app.post("/v1/images/generations", response_model=None)
async def images(request: Request) -> Response:
    if not _authorized(request):
        return _error(401, "invalid_api_key", "Missing bearer token", "invalid_request_error")
    body = orjson.loads(await request.body())
    if "[[mock:content_filter]]" in str(body.get("prompt", "")):
        return _error(*_FAULT_RESPONSES["content_filter"])
    await _latency()
    n = int(body.get("n") or 1)
    data = [{"b64_json": base64.b64encode(_PNG_1X1).decode()} for _ in range(n)]
    return _json({"created": int(time.time()), "data": data})
