"""Mock OpenAI-compatible LLM provider with configurable latency and fault injection.

Used by the dev stack, by end-to-end tests (in-process through an ASGI transport) and by load
tests, so they measure the gateway and the database, not a real provider (spec section 11).

Faults are requested in the prompt itself, so they pass through the gateway untouched:
`[[mock:rate_limit]]`, `[[mock:server_error]]`, `[[mock:timeout]]`, `[[mock:content_filter]]`,
`[[mock:ctx_overflow]]`, `[[mock:auth]]`, `[[mock:malformed_stream]]`, `[[mock:tool_call]]`,
`[[mock:no_usage]]` (stream without a usage chunk), `[[mock:slow:<ms>]]`,
`[[mock:leak]]` (the answer carries a fake cloud key) and `[[mock:classifier_down]]` (the
SystemOne stand-in at /v1/systemone answers 503).
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
    if body.get("logprobs") and any(  # an OCR call (docparse): read the page like a model
        isinstance(part, dict) and part.get("type") == "image_url"
        for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
    ):
        reply = "# Recognised page\n\nText read by the mock vision model."
    if "leak" in faults:  # output guardrail tests: a key-shaped canary, assembled at runtime
        reply += " canary " + "AK" + "IA" + "MOCKLEAKEDKEY123"
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
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish, **_logprobs(body, reply)}
            ],
            "usage": usage,
        }
    )


def _logprobs(body: dict[str, Any], reply: str) -> dict[str, Any]:
    """Per-token log-probabilities when asked for: a steady, confident -0.05."""
    if not body.get("logprobs"):
        return {}
    tokens = [{"token": t, "logprob": -0.05, "top_logprobs": []} for t in reply.split()]
    return {"logprobs": {"content": tokens}}


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
    """A deterministic embedding: lower-cased words hashed into signed buckets, so texts that
    share words point in similar directions (enough for retrieval tests). Text without a word
    falls back to a hash of the whole text."""
    vec = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        vec[int.from_bytes(digest[:4], "big") % dims] += 1.0 if digest[4] & 1 else -1.0
    if not any(vec):
        counter = 0
        out: list[float] = []
        while len(out) < dims:
            block = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            out.extend(v / 2**31 for v in struct.unpack(">8i", block))
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


# Deterministic stand-in for Laya and Jev (the SystemOne protocol): keyword heuristics, not a model.
_INJECTION_HINTS = re.compile(
    r"no restrictions|without restrictions|no limits|without limits|new persona|system override"
    r"|secret instructions|hidden prompt|jailbreak|ignore (?:all |the )?previous instructions",
    re.IGNORECASE,
)


@app.post("/v1/systemone", response_model=None)
async def systemone(request: Request) -> Response:
    body = orjson.loads(await request.body())
    state = body.get("state")
    text = state if isinstance(state, str) else orjson.dumps(state).decode()
    faults = {m.group(1): m.group(2) for m in _FAULT.finditer(text)}
    if "classifier_down" in faults:
        return _error(503, "server_error", "Service temporarily unavailable", "server_error")
    await _latency(int(faults.get("slow") or 0))
    questions = body.get("questions")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 10:
        return _error(
            400, "invalid_request_error", "1 to 10 questions are required", "invalid_request_error"
        )
    answers: dict[str, Any] = {}
    for name, question in questions.items():
        kind = question.get("type") if isinstance(question, dict) else None
        if kind == "noul":
            about_injection = "inject" in str(question.get("instructions", "")).lower()
            hit = about_injection and _INJECTION_HINTS.search(text) is not None
            answers[name] = {"type": "noul", "noul": 0.96 if hit else 0.03}
        elif kind == "choice":
            options = list(question.get("criteria") or [])
            first = options[0] if options else None
            answers[name] = {
                "type": "choice",
                "choice": first,
                "probabilities": {o: 1.0 if o == first else 0.0 for o in options},
                "confidence": 0.9,
            }
        elif kind == "score":
            levels = list(question.get("criteria") or [])
            answers[name] = {
                "type": "score",
                "score": 0.0,
                "legend": {str(i): level for i, level in enumerate(levels)},
                "probabilities": {"0": 1.0},
                "confidence": 0.9,
            }
        else:
            return _error(
                400, "invalid_request_error", f"unknown type for {name}", "invalid_request_error"
            )
    return _json(
        {
            "id": f"systemone-{uuid.uuid4().hex}",
            "object": "systemone.classification",
            "created": int(time.time()),
            "model": "laya-mock-0.3",
            "answers": answers,
            "usage": {"input_tokens": tokens(text), "output_tokens": len(answers)},
        }
    )
