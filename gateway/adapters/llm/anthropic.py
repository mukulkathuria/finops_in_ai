"""Anthropic Messages API adapter, built on the official SDK (vendor types stay in this module).

The SDK pins API version 2026-10-21, which rejects temperature, top_p and top_k, so this adapter
never sends them (`capabilities().sampling_params` is False). System messages from the OpenAI
shape become the top-level `system` field; tool results are grouped into one user turn.
"""

import base64
import binascii
from collections.abc import AsyncIterator, Mapping
from typing import Any

import anthropic
import httpx2
import orjson

from gateway.adapters.llm.common import malformed
from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    CanonicalResponse,
    EmbedRequest,
    EmbedResponse,
    FinishReason,
    ImagePart,
    ImageRequest,
    ImageResponse,
    Message,
    ModelCapabilities,
    ProviderError,
    ProviderErrorKind,
    TextPart,
    ToolCall,
    ToolCallDelta,
    Usage,
)

_FINISH: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


def _image_block(part: ImagePart) -> dict[str, Any]:
    if part.url.startswith("data:"):
        header, _, data = part.url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        try:
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.BAD_REQUEST,
                "image data URL is not valid base64",
                provider="anthropic",
            ) from exc
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": part.url}}


def _blocks(m: Message) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for p in m.content:
        if isinstance(p, TextPart):
            if p.text:
                blocks.append({"type": "text", "text": p.text})
        else:
            blocks.append(_image_block(p))
    return blocks


def _json_object(arguments: str) -> dict[str, Any]:
    try:
        value = orjson.loads(arguments or "{}")
    except orjson.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _mark_cacheable(params: dict[str, Any], *, multi_turn: bool) -> None:
    """Prompt caching is a prefix match over tools, then system, then messages. A breakpoint on
    the system prompt (or, without one, the last tool) caches what every request of an app
    shares; in a conversation one on the newest turn lets the next turn read all before it. A
    single question is left unmarked: writing it costs more and nothing would read it."""
    if "system" in params:
        params["system"] = [
            {"type": "text", "text": params["system"], "cache_control": {"type": "ephemeral"}}
        ]
    elif params.get("tools"):
        params["tools"][-1]["cache_control"] = {"type": "ephemeral"}
    if multi_turn:
        last = params["messages"][-1]["content"][-1]
        if last.get("type") != "text" or last.get("text"):  # an empty text block cannot be marked
            last["cache_control"] = {"type": "ephemeral"}


class AnthropicAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        http: httpx2.AsyncClient,
        base_url: str | None = None,
        name: str = "anthropic",
        timeout_s: float = 120.0,
    ) -> None:
        # Retries and fallback belong to the gateway pipeline, never to the SDK.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, http_client=http, max_retries=0, timeout=timeout_s
        )
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            chat=True,
            embeddings=False,
            vision=True,
            tools=True,
            prompt_cache=True,
            sampling_params=False,
        )

    def params(self, req: CanonicalRequest) -> dict[str, Any]:
        system = "\n\n".join(m.text() for m in req.messages if m.role == "system" and m.text())
        messages: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                result = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.text(),
                }
                previous = messages[-1] if messages else None
                if (
                    previous
                    and previous["role"] == "user"
                    and all(b.get("type") == "tool_result" for b in previous["content"])
                ):
                    previous["content"].append(result)
                else:
                    messages.append({"role": "user", "content": [result]})
                continue
            blocks = _blocks(m)
            if m.role == "assistant":
                blocks += [
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.name,
                        "input": _json_object(c.arguments),
                    }
                    for c in m.tool_calls
                ]
            messages.append({"role": m.role, "content": blocks or [{"type": "text", "text": ""}]})
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": messages,
        }
        if system:
            params["system"] = system
        if req.stop:
            params["stop_sequences"] = list(req.stop)
        if req.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in req.tools
            ]
        if req.forced_tool:
            params["tool_choice"] = {"type": "tool", "name": req.forced_tool}
        elif req.tool_choice:
            params["tool_choice"] = {
                "type": {"auto": "auto", "none": "none", "required": "any"}[req.tool_choice]
            }
        if req.prompt_cache:
            multi_turn = any(m.role == "assistant" for m in req.messages)
            _mark_cacheable(params, multi_turn=multi_turn)
        return params

    async def complete(self, req: CanonicalRequest) -> CanonicalResponse:
        try:
            message = await self._client.messages.create(**self.params(req))
        except anthropic.AnthropicError as exc:
            raise self.classify_error(exc) from exc
        except (ValueError, TypeError) as exc:  # undecodable body: keep the adapter contract typed
            raise malformed(self._name) from exc
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = tuple(
            ToolCall(b.id, b.name, orjson.dumps(b.input).decode())
            for b in message.content
            if b.type == "tool_use"
        )
        return CanonicalResponse(
            id=message.id,
            model=message.model,
            content=text or None,
            tool_calls=calls,
            finish_reason=_FINISH.get(str(message.stop_reason), "stop"),
            usage=self.normalize_usage(message.usage.model_dump()),
        )

    async def stream(self, req: CanonicalRequest) -> AsyncIterator[CanonicalChunk]:
        usage: dict[str, Any] = {}
        finish: FinishReason = "stop"
        tool_index: dict[int, int] = {}
        try:
            events = await self._client.messages.create(**self.params(req), stream=True)
            async for event in events:
                if event.type == "message_start":
                    usage = event.message.usage.model_dump()
                elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                    tool_index[event.index] = len(tool_index)
                    block = event.content_block
                    delta = ToolCallDelta(
                        index=tool_index[event.index], id=block.id, name=block.name
                    )
                    yield CanonicalChunk(tool_calls=(delta,))
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield CanonicalChunk(content=event.delta.text)
                    elif event.delta.type == "input_json_delta" and event.index in tool_index:
                        delta = ToolCallDelta(
                            index=tool_index[event.index], arguments=event.delta.partial_json
                        )
                        yield CanonicalChunk(tool_calls=(delta,))
                elif event.type == "message_delta":
                    finish = _FINISH.get(str(event.delta.stop_reason), "stop")
                    if event.usage is not None:
                        usage.update(
                            {k: v for k, v in event.usage.model_dump().items() if v is not None}
                        )
                elif event.type == "message_stop":
                    yield CanonicalChunk(finish_reason=finish, usage=self.normalize_usage(usage))
        except anthropic.AnthropicError as exc:
            raise self.classify_error(exc) from exc
        except (ValueError, TypeError) as exc:  # undecodable stream event
            raise malformed(self._name) from exc

    async def embed(self, req: EmbedRequest) -> EmbedResponse:
        raise ProviderError(
            ProviderErrorKind.BAD_REQUEST,
            "Anthropic does not offer an embeddings API",
            provider=self._name,
        )

    async def generate_images(self, req: ImageRequest) -> ImageResponse:
        raise ProviderError(
            ProviderErrorKind.BAD_REQUEST, "Anthropic does not generate images", provider=self._name
        )

    def normalize_usage(self, raw: Mapping[str, Any]) -> Usage:
        # Anthropic reports cache reads and writes separately from input_tokens; fold them in.
        uncached = int(raw.get("input_tokens") or 0)
        read = int(raw.get("cache_read_input_tokens") or 0)
        write = int(raw.get("cache_creation_input_tokens") or 0)
        return Usage(
            input_tokens=uncached + read + write,
            output_tokens=int(raw.get("output_tokens") or 0),
            cached_tokens=read,
            cache_write_tokens=write,
        )

    def classify_error(self, exc: BaseException) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderError(
                ProviderErrorKind.TIMEOUT, "provider timed out", provider=self._name
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(
                ProviderErrorKind.UNAVAILABLE, "provider unreachable", provider=self._name
            )
        if isinstance(exc, anthropic.APIStatusError):
            status = exc.status_code
            etype = str(getattr(exc, "type", "") or "")
            message = str(exc.message)
            retry = None
            raw_retry = (
                exc.response.headers.get("retry-after") if exc.response is not None else None
            )
            if raw_retry:
                try:
                    retry = float(raw_retry)
                except ValueError:
                    retry = None
            if status == 429 or etype == "rate_limit_error":
                kind = ProviderErrorKind.RATE_LIMIT
            elif status in (401, 402, 403) or etype in (
                "authentication_error",
                "permission_error",
                "billing_error",
            ):
                kind = ProviderErrorKind.AUTH
            elif etype == "overloaded_error" or status >= 500:
                kind = ProviderErrorKind.UNAVAILABLE
            elif "prompt is too long" in message.lower() or "context window" in message.lower():
                kind = ProviderErrorKind.CTX_OVERFLOW
            elif 400 <= status < 500:
                kind = ProviderErrorKind.BAD_REQUEST
            else:
                kind = ProviderErrorKind.OTHER
            return ProviderError(
                kind, message, provider=self._name, status=status, retry_after_s=retry
            )
        return ProviderError(
            ProviderErrorKind.OTHER,
            f"provider call failed ({type(exc).__name__})",
            provider=self._name,
        )
