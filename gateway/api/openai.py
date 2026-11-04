"""OpenAI-compatible data plane (US-05): /v1/chat/completions, /v1/embeddings, /v1/models.

The official OpenAI SDK works by changing its base URL. Responses carry cost and usage headers;
streams end with an SSE comment carrying the cost because headers leave before it is known.
"""

import base64
import struct
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing, suppress
from typing import Annotated, Any, Literal, Protocol

import orjson
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import Receive, Scope, Send

from gateway.api.caller import Caller, ModelCaller
from gateway.api.documents import Retrieval
from gateway.api.files import Parsing, file_owner
from gateway.domain.errors import InvalidInput, MalformedRequest
from gateway.domain.idempotency import Idempotency
from gateway.domain.llm import (
    CanonicalRequest,
    ContentPart,
    EmbedRequest,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolSpec,
)
from gateway.domain.parsing import text_tokens
from gateway.domain.pricing import usd
from gateway.pipeline.chat import Attribution, ChatService, ChunkStream, StreamOutcome
from gateway.pipeline.idempotency import Origin
from gateway.pipeline.retrieval import retrieval_report, with_retrieved

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


class _RetrievalIn(BaseModel):
    """`"retrieval": {"k": 5}`: add the k passages nearest to the question that the caller may
    see (send it with the OpenAI SDK through `extra_body`)."""

    model_config = ConfigDict(extra="forbid")
    k: int = Field(default=5, ge=1, le=50)


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
    retrieval: _RetrievalIn | None = None


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


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """The question to retrieve for: the text of the last user message (not its files)."""
    for m in reversed(messages):
        if not (isinstance(m, dict) and m.get("role") == "user"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(p["text"])
                for p in content
                if isinstance(p, dict)
                and p.get("type") == "text"
                and isinstance(p.get("text"), str)
            )
    return ""


def _file_ids(messages: list[dict[str, Any]]) -> list[str]:
    """The files referenced by `{"type": "file"}` parts, in order and without repeats. Files are
    attached by id (upload them to /v1/files first), and only to user messages."""
    seen: dict[str, None] = {}
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        for part in content if isinstance(content, list) else ():
            if not (isinstance(part, dict) and part.get("type") == "file"):
                continue
            ref = part.get("file")
            if not isinstance(ref, dict) or not isinstance(ref.get("file_id"), str):
                raise InvalidInput(
                    "a file part needs file.file_id: upload the file to /v1/files first",
                    code="file_id_required",
                    param="messages",
                )
            if m.get("role") != "user":
                raise InvalidInput("files can be attached to user messages only", param="messages")
            seen.setdefault(ref["file_id"], None)
    return list(seen)


def _content(raw: Any, files: Mapping[str, tuple[ContentPart, ...]]) -> tuple[ContentPart, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (TextPart(raw),)
    if not isinstance(raw, list):
        raise InvalidInput("message content must be a string or a list of parts", param="messages")
    parts: list[ContentPart] = []
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
        elif (
            kind == "file"
            and isinstance(part.get("file"), dict)
            and part["file"].get("file_id") in files
        ):
            parts.extend(files[part["file"]["file_id"]])  # resolved by the parse service
        else:
            raise InvalidInput(f"unsupported content part type: {kind!r}", param="messages")
    return tuple(parts)


def _messages(
    raw: list[dict[str, Any]], files: Mapping[str, tuple[ContentPart, ...]]
) -> tuple[Message, ...]:
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
                content=_content(m.get("content"), files),
                tool_calls=calls,
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
            )
        )
    return tuple(out)


def _canonical(
    body: ChatRequest,
    provider_model: str,
    max_tokens: int,
    files: Mapping[str, tuple[ContentPart, ...]],
) -> CanonicalRequest:
    forced: str | None = None
    choice: Literal["auto", "none", "required"] | None = None
    if isinstance(body.tool_choice, dict):
        forced = str((body.tool_choice.get("function") or {}).get("name") or "") or None
    elif body.tool_choice in ("auto", "none", "required"):
        choice = body.tool_choice  # type: ignore[assignment]
    stop = (body.stop,) if isinstance(body.stop, str) else tuple(body.stop or ())
    return CanonicalRequest(
        model=provider_model,
        messages=_messages(body.messages, files),
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


def _idempotency(request: Request, caller: Caller, body: bytes) -> Idempotency | None:
    """An Idempotency-Key belongs to one credential: equal keys of two callers never meet."""
    owner = f"key:{caller.api_key_id}" if caller.api_key_id else f"user:{caller.user_id}"
    return Idempotency.from_header(
        request.headers.get("idempotency-key"), caller=owner, route=request.url.path, body=body
    )


def _completion_id(origin: Origin) -> str:
    """Derived from the request that produced the result, so a replay is byte-identical."""
    return f"chatcmpl-{origin.request_id.replace('-', '')}"


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
async def chat_completions(
    request: Request, caller: ModelCaller, chat: Chat, parsing: Parsing, retrieval: Retrieval
) -> Response:
    body = _validate(ChatRequest, await _json(request))
    resolved = await chat.resolve(body.model, "chat", caller.org_id)
    requested = body.max_completion_tokens or body.max_tokens or chat.default_max_tokens
    max_tokens = min(requested, resolved.entry.max_output_tokens or requested)
    who = _who(caller, request)
    file_ids = _file_ids(body.messages)
    if file_ids or body.retrieval is not None:
        # Converting attachments and embedding the question is work no budget pays for: what
        # the budgets cannot cover even without them is refused first.
        bare = _canonical(body, resolved.entry.model, max_tokens, dict.fromkeys(file_ids, ()))
        await chat.precheck(who, resolved, bare)
    # Attachments become Markdown before the budget hold and the guardrails see the request.
    attached = await parsing.attach(file_owner(caller), file_ids)
    req = _canonical(body, resolved.entry.model, max_tokens, attached.parts)
    headers = {
        "x-finops-provider": resolved.entry.provider,
        "x-finops-model": resolved.entry.public_name,
        "x-finops-catalog-version": str(resolved.entry.catalog_version),
    }
    notes: dict[str, object] | None = None
    if attached.reports:
        headers["x-finops-file-tokens-before"] = str(attached.tokens_before)
        headers["x-finops-file-tokens-after"] = str(attached.tokens_after)
        notes = {"files": list(attached.reports)}
    if body.retrieval is not None:  # the permission filter runs inside the vector search
        question = _last_user_text(body.messages)
        k = body.retrieval.k
        plan = await retrieval.plan(caller.org_id, k)  # US-21: rerank more than k
        hits = await retrieval.search(
            file_owner(caller), caller.groups, question, plan.candidates, source="chat"
        )
        hits, dropped = await chat.passages(caller.org_id, hits)
        selection = await retrieval.select(question, hits, k=k, plan=plan)
        req = with_retrieved(req, selection.passages)
        headers["x-finops-retrieved-chunks"] = str(len(selection.passages))
        retrieved: dict[str, Any] = {
            **retrieval_report(selection.passages),
            "dropped": dropped,
            "query_tokens": text_tokens(question),
        }
        if (relevance := selection.report) is not None:
            retrieved["relevance"] = relevance
            if plan.mode == "on" and relevance["rerank"] == "ok":
                pruned = relevance["tokens_before"] - relevance["tokens_after"]
                headers["x-finops-pruned-tokens"] = str(pruned)
        notes = {**(notes or {}), "retrieval": retrieved}
    idem = _idempotency(request, caller, await request.body())
    if body.stream:
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        outcome: list[StreamOutcome] = []
        # Budget, idempotency and provider errors raise here, so the client gets JSON errors.
        stream = await chat.stream(who, resolved, req, outcome, idem, notes=notes)
        if stream.origin.replayed:
            headers["idempotent-replayed"] = "true"
        if stream.cache_hit != "none":
            headers["x-finops-cache"] = stream.cache_hit
        model = stream.origin.model or resolved.entry.public_name  # the router's pick
        headers["x-finops-model"] = model
        frames = _sse(
            stream.chunks,
            outcome,
            _completion_id(stream.origin),
            int(stream.origin.at.timestamp()),
            model,
            include_usage,
        )
        return _SettlingStream(
            frames, stream.chunks, headers={**headers, "cache-control": "no-store"}
        )
    result = await chat.complete(who, resolved, req, idem, notes=notes)
    response, cost = result.response, result.cost
    completion_id, created = _completion_id(result.origin), int(result.origin.at.timestamp())
    if result.origin.replayed:
        headers["idempotent-replayed"] = "true"
    if result.cache_hit != "none":
        headers["x-finops-cache"] = result.cache_hit
    model = result.origin.model or resolved.entry.public_name  # the router's pick
    headers["x-finops-model"] = model
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
        "model": model,
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


class _SettlingStream(StreamingResponse):
    """Closes the frames and the relay however the response ends: completed, disconnected,
    cancelled, or never started (Starlette skips the body when its first send already fails),
    so every stream settles its budget hold and ledger row exactly once."""

    def __init__(
        self, frames: AsyncGenerator[bytes], chunks: ChunkStream, *, headers: dict[str, str]
    ) -> None:
        super().__init__(frames, media_type="text/event-stream", headers=headers)
        self._frames = frames
        self._chunks = chunks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with suppress(RuntimeError):  # still running elsewhere: it closes itself
                await self._frames.aclose()
            await self._chunks.aclose()


async def _sse(
    chunks: ChunkStream,
    outcome: list[StreamOutcome],
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
) -> AsyncGenerator[bytes]:
    def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
        event = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return b"data: " + orjson.dumps(event) + b"\n\n"

    async with aclosing(chunks):  # closing the frames always settles the stream
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
    result = await chat.embed(
        _who(caller, request),
        resolved,
        EmbedRequest(resolved.entry.model, inputs, body.dimensions),
        _idempotency(request, caller, await request.body()),
    )
    response, cost = result.response, result.cost
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
    if result.origin.replayed:
        headers["idempotent-replayed"] = "true"
    return Response(orjson.dumps(payload), media_type="application/json", headers=headers)


@router.get("/v1/models", response_model=None)
async def list_models(caller: ModelCaller, chat: Chat) -> Response:
    models = await chat.models()
    data = [
        {"id": m.public_name, "object": "model", "created": 0, "owned_by": m.provider}
        for m in models
    ]
    return Response(orjson.dumps({"object": "list", "data": data}), media_type="application/json")
