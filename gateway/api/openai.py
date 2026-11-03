"""OpenAI-compatible data plane (US-05): /v1/chat/completions, /v1/embeddings, /v1/models.

The official OpenAI SDK works by changing its base URL. Responses carry cost and usage headers;
streams end with an SSE comment carrying the cost because headers leave before it is known.
"""

import base64
import struct
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, Protocol

import orjson
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.api.caller import Caller, ModelCaller
from gateway.domain.errors import InvalidInput, MalformedRequest
from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    EmbedRequest,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolSpec,
)
from gateway.domain.pricing import usd
from gateway.pipeline.chat import Attribution, ChatService, StreamOutcome

router = APIRouter(tags=["data-plane"])


class ChatServices(Protocol):
    @property
    def chat_service(self) -> ChatService: ...


def get_chat(request: Request) -> ChatService:
    services: ChatServices = request.app.state.container
    return services.chat_service


Chat = Annotated[ChatService, Depends(get_chat)]


class _Function(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(default="", max_length=4096)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class _Tool(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["function"] = "function"
    function: _Function


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = Field(min_length=1, max_length=200)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=2000)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    tools: list[_Tool] = Field(default_factory=list, max_length=128)
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    n: int = Field(default=1, ge=1, le=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None


class EmbeddingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = Field(min_length=1, max_length=200)
    input: str | list[str] = Field(min_length=1)
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: int | None = Field(default=None, ge=1, le=8192)


async def _json(request: Request) -> Any:
    try:
        return orjson.loads(await request.body())
    except orjson.JSONDecodeError as exc:  # also rejects invalid UTF-8 and excessive nesting
        raise MalformedRequest("request body is not valid JSON") from exc


def _validate[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise InvalidInput(
            str(first.get("msg")),
            code="invalid_request",
            param=".".join(str(p) for p in first.get("loc", ())),
        ) from exc


def _content(raw: Any) -> tuple[TextPart | ImagePart, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (TextPart(raw),)
    if not isinstance(raw, list):
        raise InvalidInput("message content must be a string or a list of parts", param="messages")
    parts: list[TextPart | ImagePart] = []
    for part in raw:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append(TextPart(part["text"]))
        elif (
            kind == "image_url"
            and isinstance(part.get("image_url"), dict)
            and part["image_url"].get("url")
        ):
            parts.append(ImagePart(str(part["image_url"]["url"]), part["image_url"].get("detail")))
        else:
            raise InvalidInput(f"unsupported content part type: {kind!r}", param="messages")
    return tuple(parts)


def _messages(raw: list[dict[str, Any]]) -> tuple[Message, ...]:
    out: list[Message] = []
    for m in raw:
        role = m.get("role") if isinstance(m, dict) else None
        if role == "developer":
            role = "system"
        if role not in ("system", "user", "assistant", "tool"):
            raise InvalidInput(f"unsupported message role: {role!r}", param="messages")
        calls = tuple(
            ToolCall(
                str(c["id"]), str(c["function"]["name"]), str(c["function"].get("arguments") or "")
            )
            for c in m.get("tool_calls") or ()
            if isinstance(c, dict) and isinstance(c.get("function"), dict)
        )
        out.append(
            Message(
                role=role,
                content=_content(m.get("content")),
                tool_calls=calls,
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
            )
        )
    return tuple(out)


def _canonical(body: ChatRequest, provider_model: str, max_tokens: int) -> CanonicalRequest:
    forced: str | None = None
    choice: Literal["auto", "none", "required"] | None = None
    if isinstance(body.tool_choice, dict):
        forced = str((body.tool_choice.get("function") or {}).get("name") or "") or None
    elif body.tool_choice in ("auto", "none", "required"):
        choice = body.tool_choice  # type: ignore[assignment]
    stop = (body.stop,) if isinstance(body.stop, str) else tuple(body.stop or ())
    return CanonicalRequest(
        model=provider_model,
        messages=_messages(body.messages),
        max_tokens=max_tokens,
        temperature=body.temperature,
        top_p=body.top_p,
        stop=stop[:4],
        tools=tuple(
            ToolSpec(t.function.name, t.function.description, t.function.parameters)
            for t in body.tools
        ),
        tool_choice=choice,
        forced_tool=forced,
        response_format=body.response_format,
        seed=body.seed,
    )


def _who(caller: Caller, request: Request) -> Attribution:
    return Attribution(
        org_id=caller.org_id,
        team_id=caller.team_id,
        user_id=caller.user_id,
        api_key_id=caller.api_key_id,
        request_id=str(getattr(request.state, "request_id", "")),
    )


def _usage_json(
    input_tokens: int, output_tokens: int, cached: int, reasoning: int
) -> dict[str, Any]:
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request, caller: ModelCaller, chat: Chat) -> Response:
    body = _validate(ChatRequest, await _json(request))
    resolved = await chat.resolve(body.model, "chat", caller.org_id)
    requested = body.max_completion_tokens or body.max_tokens or chat.default_max_tokens
    limit = resolved.entry.max_output_tokens or requested
    req = _canonical(body, resolved.entry.model, min(requested, limit))
    who = _who(caller, request)
    headers = {
        "x-finops-provider": resolved.entry.provider,
        "x-finops-model": resolved.entry.public_name,
        "x-finops-catalog-version": str(resolved.entry.catalog_version),
    }
    completion_id = f"chatcmpl-{who.request_id.replace('-', '')}"
    created = int(time.time())
    if body.stream:
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        outcome: list[StreamOutcome] = []
        frames = _sse(
            chat.stream(who, resolved, req, outcome),
            outcome,
            completion_id,
            created,
            resolved.entry.public_name,
            include_usage,
        )
        return StreamingResponse(
            frames, media_type="text/event-stream", headers={**headers, "cache-control": "no-store"}
        )
    response, cost = await chat.complete(who, resolved, req)
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in response.tool_calls
        ]
    u = response.usage
    payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": resolved.entry.public_name,
        "choices": [{"index": 0, "message": message, "finish_reason": response.finish_reason}],
        "usage": _usage_json(u.input_tokens, u.output_tokens, u.cached_tokens, u.reasoning_tokens),
    }
    headers |= {
        "x-finops-cost-micro-usd": str(cost),
        "x-finops-cost-usd": usd(cost),
        "x-finops-input-tokens": str(u.input_tokens),
        "x-finops-output-tokens": str(u.output_tokens),
    }
    return Response(orjson.dumps(payload), media_type="application/json", headers=headers)


async def _sse(
    chunks: AsyncIterator[CanonicalChunk],
    outcome: list[StreamOutcome],
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
) -> AsyncIterator[bytes]:
    def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
        event = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return b"data: " + orjson.dumps(event) + b"\n\n"

    yield frame({"role": "assistant", "content": ""})
    async for chunk in chunks:
        delta: dict[str, Any] = {}
        if chunk.content:
            delta["content"] = chunk.content
        if chunk.tool_calls:
            delta["tool_calls"] = [
                {
                    "index": t.index,
                    **({"id": t.id, "type": "function"} if t.id else {}),
                    "function": {
                        k: v
                        for k, v in (("name", t.name), ("arguments", t.arguments))
                        if v is not None
                    },
                }
                for t in chunk.tool_calls
            ]
        if delta or chunk.finish_reason:
            yield frame(delta, chunk.finish_reason)
    final = outcome[-1] if outcome else None
    if final is not None and include_usage:
        u = final.usage
        event = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": _usage_json(
                u.input_tokens, u.output_tokens, u.cached_tokens, u.reasoning_tokens
            ),
        }
        yield b"data: " + orjson.dumps(event) + b"\n\n"
    if final is not None:
        yield f": finops cost_micro_usd={final.cost_micro_usd}\n\n".encode()
    yield b"data: [DONE]\n\n"


@router.post("/v1/embeddings", response_model=None)
async def embeddings(request: Request, caller: ModelCaller, chat: Chat) -> Response:
    body = _validate(EmbeddingsRequest, await _json(request))
    inputs = (body.input,) if isinstance(body.input, str) else tuple(body.input)
    if not inputs or len(inputs) > 2048 or any(not isinstance(t, str) or not t for t in inputs):
        raise InvalidInput("input must be 1-2048 non-empty strings", param="input")
    resolved = await chat.resolve(body.model, "embedding", caller.org_id)
    response, cost = await chat.embed(
        _who(caller, request), resolved, EmbedRequest(resolved.entry.model, inputs, body.dimensions)
    )
    data = []
    for i, vector in enumerate(response.vectors):
        embedding: Any = (
            base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()
            if body.encoding_format == "base64"
            else list(vector)
        )
        data.append({"object": "embedding", "index": i, "embedding": embedding})
    tokens = response.usage.input_tokens
    payload = {
        "object": "list",
        "model": resolved.entry.public_name,
        "data": data,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
    headers = {
        "x-finops-cost-micro-usd": str(cost),
        "x-finops-cost-usd": usd(cost),
        "x-finops-provider": resolved.entry.provider,
        "x-finops-model": resolved.entry.public_name,
    }
    return Response(orjson.dumps(payload), media_type="application/json", headers=headers)


@router.get("/v1/models", response_model=None)
async def list_models(caller: ModelCaller, chat: Chat) -> Response:
    models = await chat.models()
    data = [
        {"id": m.public_name, "object": "model", "created": 0, "owned_by": m.provider}
        for m in models
    ]
    return Response(orjson.dumps({"object": "list", "data": data}), media_type="application/json")
