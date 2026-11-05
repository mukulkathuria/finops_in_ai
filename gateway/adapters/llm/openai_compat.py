"""OpenAI-compatible adapter (OpenAI, Groq, Together, DeepSeek, Mistral, vLLM, Ollama, the mock).

Speaks `/chat/completions` and `/embeddings` over a shared httpx2 client. Streams always ask for
`stream_options.include_usage`, so billing uses provider-reported usage whenever it exists.
"""

from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

import httpx2
import orjson

from gateway.adapters.llm.common import json_body, malformed, retry_after, sse_data, transport_error
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
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}
_CTX_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})
_FILTER_CODES = frozenset({"content_policy_violation", "content_filter"})


class OpenAICompatAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http: httpx2.AsyncClient,
        name: str = "openai_compat",
        timeout_s: float = 120.0,
        max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        self._http = http
        self._name = name
        self._timeout = timeout_s
        self._max_field = max_tokens_field

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(chat=True, embeddings=True, vision=True, tools=True)

    # ------------------------------------------------------------------ request mapping
    @staticmethod
    def _message(m: Message) -> dict[str, Any]:
        out: dict[str, Any] = {"role": m.role}
        if len(m.content) == 1 and isinstance(m.content[0], TextPart):
            out["content"] = m.content[0].text
        elif m.content:
            parts: list[dict[str, Any]] = []
            for p in m.content:
                if isinstance(p, TextPart):
                    parts.append({"type": "text", "text": p.text})
                elif isinstance(p, ImagePart):
                    image: dict[str, Any] = {"url": p.url}
                    if p.detail:
                        image["detail"] = p.detail
                    parts.append({"type": "image_url", "image_url": image})
            out["content"] = parts
        else:
            out["content"] = None if m.tool_calls else ""
        if m.tool_calls:
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in m.tool_calls
            ]
        if m.tool_call_id:
            out["tool_call_id"] = m.tool_call_id
        if m.name:
            out["name"] = m.name
        return out

    def payload(self, req: CanonicalRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": [self._message(m) for m in req.messages],
            self._max_field: req.max_tokens,
        }
        optional = {"temperature": req.temperature, "top_p": req.top_p, "seed": req.seed}
        body.update({k: v for k, v in optional.items() if v is not None})
        if req.stop:
            body["stop"] = list(req.stop)
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in req.tools
            ]
        if req.forced_tool:
            body["tool_choice"] = {"type": "function", "function": {"name": req.forced_tool}}
        elif req.tool_choice:
            body["tool_choice"] = req.tool_choice
        if req.response_format:
            body["response_format"] = req.response_format
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    # ------------------------------------------------------------------ calls
    async def complete(self, req: CanonicalRequest) -> CanonicalResponse:
        try:
            resp = await self._http.post(
                f"{self._base}/chat/completions",
                content=orjson.dumps(self.payload(req, stream=False)),
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            raise transport_error(exc, self._name) from exc
        if resp.status_code >= 400:
            raise self._status_error(resp)
        return self._completion(json_body(resp), req)

    def _completion(self, data: Mapping[str, Any], req: CanonicalRequest) -> CanonicalResponse:
        """A reply (OpenAI shape) as the canonical response."""
        try:
            choice = data["choices"][0]
            msg = choice["message"]
            calls = tuple(
                ToolCall(c["id"], c["function"]["name"], c["function"].get("arguments") or "")
                for c in msg.get("tool_calls") or ()
            )
            raw_usage = data.get("usage")
            return CanonicalResponse(
                id=str(data.get("id", "")),
                model=str(data.get("model") or req.model),
                content=msg.get("content"),
                tool_calls=calls,
                finish_reason=_FINISH.get(str(choice.get("finish_reason")), "stop"),
                usage=self.normalize_usage(raw_usage) if raw_usage else Usage(estimated=True),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise malformed(self._name) from exc

    async def stream(self, req: CanonicalRequest) -> AsyncIterator[CanonicalChunk]:
        try:
            async with self._http.stream(
                "POST",
                f"{self._base}/chat/completions",
                content=orjson.dumps(self.payload(req, stream=True)),
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._status_error(resp)
                async for data in sse_data(resp.aiter_lines()):
                    if data == "[DONE]":
                        return
                    for chunk in self._chunks(data):
                        yield chunk
        except httpx2.HTTPError as exc:
            raise transport_error(exc, self._name) from exc

    def _chunks(self, data: str) -> list[CanonicalChunk]:
        try:
            event = orjson.loads(data)
        except orjson.JSONDecodeError as exc:
            raise malformed(self._name) from exc
        if not isinstance(event, dict):
            raise malformed(self._name)
        return self._events(event)

    def _events(self, event: Mapping[str, Any]) -> list[CanonicalChunk]:
        """The chunks one streamed event (OpenAI shape) carries."""
        if isinstance(event.get("error"), dict):
            raise self._error_from_body(event["error"], status=None, retry=None)
        out: list[CanonicalChunk] = []
        for choice in event.get("choices") or ():
            delta = choice.get("delta") or {}
            calls = tuple(
                ToolCallDelta(
                    index=int(t.get("index", 0)),
                    id=t.get("id"),
                    name=(t.get("function") or {}).get("name"),
                    arguments=(t.get("function") or {}).get("arguments") or "",
                )
                for t in delta.get("tool_calls") or ()
            )
            finish = choice.get("finish_reason")
            if delta.get("content") or calls or finish:
                out.append(
                    CanonicalChunk(
                        content=delta.get("content") or None,
                        tool_calls=calls,
                        finish_reason=_FINISH.get(str(finish), "stop") if finish else None,
                    )
                )
        if event.get("usage"):
            out.append(CanonicalChunk(usage=self.normalize_usage(event["usage"])))
        return out

    async def embed(self, req: EmbedRequest) -> EmbedResponse:
        body: dict[str, Any] = {
            "model": req.model,
            "input": list(req.inputs),
            "encoding_format": "float",
        }
        if req.dimensions:
            body["dimensions"] = req.dimensions
        try:
            resp = await self._http.post(
                f"{self._base}/embeddings",
                content=orjson.dumps(body),
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            raise transport_error(exc, self._name) from exc
        if resp.status_code >= 400:
            raise self._status_error(resp)
        return self._embedding(json_body(resp), req)

    def _embedding(self, data: Mapping[str, Any], req: EmbedRequest) -> EmbedResponse:
        """An embeddings reply (OpenAI shape) as the canonical response."""
        try:
            items = sorted(data["data"], key=lambda d: int(d["index"]))
            vectors = tuple(tuple(float(x) for x in item["embedding"]) for item in items)
            tokens = int((data.get("usage") or {}).get("prompt_tokens") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise malformed(self._name) from exc
        return EmbedResponse(
            model=str(data.get("model") or req.model),
            vectors=vectors,
            usage=Usage(input_tokens=tokens),
        )

    async def generate_images(self, req: ImageRequest) -> ImageResponse:
        body: dict[str, Any] = {"model": req.model, "prompt": req.prompt, "n": req.n}
        if req.size:
            body["size"] = req.size
        if req.quality:
            body["quality"] = req.quality
        if not req.model.startswith("gpt-image"):  # those answer in base64 and refuse the field
            body["response_format"] = "b64_json"
        try:
            resp = await self._http.post(
                f"{self._base}/images/generations",
                content=orjson.dumps(body),
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            raise transport_error(exc, self._name) from exc
        if resp.status_code >= 400:
            raise self._status_error(resp)
        return self._image_response(json_body(resp), req)

    def _image_response(self, data: Mapping[str, Any], req: ImageRequest) -> ImageResponse:
        """An images reply (OpenAI shape) as the canonical response: base64 data only."""
        try:
            items = list(data["data"])
            images = tuple(item["b64_json"] for item in items)
            revised = tuple(item.get("revised_prompt") for item in items)
        except (KeyError, TypeError, AttributeError) as exc:
            raise malformed(self._name) from exc
        if not images or not all(isinstance(i, str) and i for i in images):
            raise malformed(self._name)
        return ImageResponse(
            model=str(data.get("model") or req.model),
            images=images[: req.n],
            revised_prompts=tuple(r if isinstance(r, str) else None for r in revised[: req.n]),
        )

    # ------------------------------------------------------------------ normalisation
    def normalize_usage(self, raw: Mapping[str, Any]) -> Usage:
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        cached = int((raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        reasoning = int((raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        return Usage(
            input_tokens=prompt,
            output_tokens=completion,
            cached_tokens=min(cached, prompt),
            reasoning_tokens=min(reasoning, completion),
        )

    def classify_error(self, exc: BaseException) -> ProviderError:
        return transport_error(exc, self._name)

    def _status_error(self, resp: httpx2.Response) -> ProviderError:
        error = json_body(resp).get("error")
        if not isinstance(error, dict):
            error = {"message": resp.text[:300]}
        return self._error_from_body(error, status=resp.status_code, retry=retry_after(resp))

    def _error_from_body(
        self, error: Mapping[str, Any], *, status: int | None, retry: float | None
    ) -> ProviderError:
        message = str(error.get("message") or "provider error")
        code = str(error.get("code") or "")
        if code == "insufficient_quota" or status in (401, 403):
            kind = ProviderErrorKind.AUTH
        elif status == 429:
            kind = ProviderErrorKind.RATE_LIMIT
        elif status in (408, 504):
            kind = ProviderErrorKind.TIMEOUT
        elif code in _CTX_CODES or "maximum context length" in message.lower():
            kind = ProviderErrorKind.CTX_OVERFLOW
        elif code in _FILTER_CODES:
            kind = ProviderErrorKind.CONTENT_FILTER
        elif status is not None and 400 <= status < 500:
            kind = ProviderErrorKind.BAD_REQUEST
        elif status is None:
            kind = ProviderErrorKind.OTHER
        else:
            kind = ProviderErrorKind.UNAVAILABLE
        return ProviderError(kind, message, provider=self._name, status=status, retry_after_s=retry)
