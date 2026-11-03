"""Google Gemini adapter over the REST API (v1beta generateContent, SSE streaming, embeddings).

Gemini 3 models attach a `thoughtSignature` to function calls and require it back on replay. The
OpenAI shape has no field for it, so the adapter carries it inside the tool-call id
(`call_<hex>.gts.<base64url>`) and restores it when the history comes back: stateless across
replicas.
"""

import base64
import binascii
import mimetypes
import secrets
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx2
import orjson

from gateway.adapters.llm.common import json_body, malformed, retry_after, sse_data, transport_error
from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    CanonicalResponse,
    ContentPart,
    EmbedRequest,
    EmbedResponse,
    FinishReason,
    ModelCapabilities,
    ProviderError,
    ProviderErrorKind,
    TextPart,
    ToolCall,
    ToolCallDelta,
    Usage,
)

_SIGNATURE_MARK = ".gts."
_FILTERED = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
    }
)
_STATUS_KIND = {
    "RESOURCE_EXHAUSTED": ProviderErrorKind.RATE_LIMIT,
    "UNAUTHENTICATED": ProviderErrorKind.AUTH,
    "PERMISSION_DENIED": ProviderErrorKind.AUTH,
    "DEADLINE_EXCEEDED": ProviderErrorKind.TIMEOUT,
    "UNAVAILABLE": ProviderErrorKind.UNAVAILABLE,
    "INTERNAL": ProviderErrorKind.UNAVAILABLE,
}


def encode_call_id(signature: str | None) -> str:
    base = f"call_{secrets.token_hex(6)}"
    if not signature:
        return base
    return (
        base + _SIGNATURE_MARK + base64.urlsafe_b64encode(signature.encode()).decode().rstrip("=")
    )


def decode_signature(call_id: str) -> str | None:
    _, mark, encoded = call_id.partition(_SIGNATURE_MARK)
    if not mark:
        return None
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None


def _json_object(arguments: str) -> dict[str, Any]:
    try:
        value = orjson.loads(arguments or "{}")
    except orjson.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _part(p: ContentPart) -> dict[str, Any]:
    if isinstance(p, TextPart):
        return {"text": p.text}
    if p.url.startswith("data:"):
        header, _, data = p.url.partition(",")
        return {"inlineData": {"mimeType": header[5:].split(";")[0] or "image/png", "data": data}}
    mime = mimetypes.guess_type(p.url)[0] or "image/jpeg"
    return {"fileData": {"fileUri": p.url, "mimeType": mime}}


class GeminiAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        http: httpx2.AsyncClient,
        base_url: str = "https://generativelanguage.googleapis.com",
        name: str = "gemini",
        timeout_s: float = 120.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
        self._http = http
        self._name = name
        self._timeout = timeout_s

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            chat=True, embeddings=True, vision=True, tools=True, prompt_cache=True
        )

    def payload(self, req: CanonicalRequest) -> dict[str, Any]:
        system = "\n\n".join(m.text() for m in req.messages if m.role == "system" and m.text())
        names: dict[str, str] = {}
        contents: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role == "system":
                continue
            if m.role == "user":
                contents.append(
                    {"role": "user", "parts": [_part(p) for p in m.content] or [{"text": ""}]}
                )
            elif m.role == "assistant":
                parts: list[dict[str, Any]] = [{"text": m.text()}] if m.text() else []
                for call in m.tool_calls:
                    names[call.id] = call.name
                    part: dict[str, Any] = {
                        "functionCall": {"name": call.name, "args": _json_object(call.arguments)}
                    }
                    if signature := decode_signature(call.id):
                        part["thoughtSignature"] = signature
                    parts.append(part)
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:  # tool result
                result = _json_object(m.text()) or {"result": m.text()}
                name = names.get(m.tool_call_id or "", m.name or "tool")
                response_part = {"functionResponse": {"name": name, "response": result}}
                previous = contents[-1] if contents else None
                if (
                    previous
                    and previous["role"] == "user"
                    and all("functionResponse" in x for x in previous["parts"])
                ):
                    previous["parts"].append(response_part)
                else:
                    contents.append({"role": "user", "parts": [response_part]})
        config: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
        optional = {"temperature": req.temperature, "topP": req.top_p, "seed": req.seed}
        config.update({k: v for k, v in optional.items() if v is not None})
        if req.stop:
            config["stopSequences"] = list(req.stop)[:5]
        if req.response_format and req.response_format.get("type") in (
            "json_object",
            "json_schema",
        ):
            config["responseMimeType"] = "application/json"
            schema = (req.response_format.get("json_schema") or {}).get("schema")
            if schema:
                config["responseJsonSchema"] = schema
        body: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if req.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parametersJsonSchema": t.parameters,
                        }
                        for t in req.tools
                    ]
                }
            ]
        if req.forced_tool:
            body["toolConfig"] = {
                "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [req.forced_tool]}
            }
        elif req.tool_choice:
            mode = {"auto": "AUTO", "none": "NONE", "required": "ANY"}[req.tool_choice]
            body["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
        return body

    def _url(self, model: str, method: str) -> str:
        return f"{self._base}/v1beta/models/{model}:{method}"

    async def complete(self, req: CanonicalRequest) -> CanonicalResponse:
        try:
            resp = await self._http.post(
                self._url(req.model, "generateContent"),
                content=orjson.dumps(self.payload(req)),
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            raise transport_error(exc, self._name) from exc
        if resp.status_code >= 400:
            raise self._status_error(resp)
        data = json_body(resp)
        text, calls, finish = self._candidate(data)
        usage = data.get("usageMetadata")
        return CanonicalResponse(
            id=str(data.get("responseId") or ""),
            model=str(data.get("modelVersion") or req.model),
            content=text or None,
            tool_calls=calls,
            finish_reason=finish or ("tool_calls" if calls else "stop"),
            usage=self.normalize_usage(usage) if usage else Usage(estimated=True),
        )

    def _candidate(
        self, data: Mapping[str, Any]
    ) -> tuple[str, tuple[ToolCall, ...], FinishReason | None]:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            if reason:
                raise ProviderError(
                    ProviderErrorKind.CONTENT_FILTER,
                    f"prompt blocked by the provider ({reason})",
                    provider=self._name,
                )
            return "", (), None
        candidate = candidates[0]
        try:
            parts = (candidate.get("content") or {}).get("parts") or []
            text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
            calls = tuple(
                ToolCall(
                    encode_call_id(p.get("thoughtSignature")),
                    p["functionCall"]["name"],
                    orjson.dumps(p["functionCall"].get("args") or {}).decode(),
                )
                for p in parts
                if "functionCall" in p
            )
        except (KeyError, TypeError) as exc:
            raise malformed(self._name) from exc
        reason = candidate.get("finishReason")
        finish: FinishReason | None
        if reason is None:
            finish = None
        elif reason in _FILTERED:
            finish = "content_filter"
        elif reason == "MAX_TOKENS":
            finish = "length"
        else:
            finish = "tool_calls" if calls else "stop"
        return text, calls, finish

    async def stream(self, req: CanonicalRequest) -> AsyncIterator[CanonicalChunk]:
        usage: Mapping[str, Any] | None = None
        finish: FinishReason | None = None
        index = 0
        try:
            async with self._http.stream(
                "POST",
                self._url(req.model, "streamGenerateContent") + "?alt=sse",
                content=orjson.dumps(self.payload(req)),
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._status_error(resp)
                async for data in sse_data(resp.aiter_lines()):
                    try:
                        event = orjson.loads(data)
                    except orjson.JSONDecodeError as exc:
                        raise malformed(self._name) from exc
                    if not isinstance(event, dict):
                        raise malformed(self._name)
                    text, calls, chunk_finish = self._candidate(event)
                    deltas = tuple(
                        ToolCallDelta(index=index + i, id=c.id, name=c.name, arguments=c.arguments)
                        for i, c in enumerate(calls)
                    )
                    index += len(calls)
                    if text or deltas:
                        yield CanonicalChunk(content=text or None, tool_calls=deltas)
                    finish = chunk_finish or finish
                    usage = event.get("usageMetadata") or usage
        except httpx2.HTTPError as exc:
            raise transport_error(exc, self._name) from exc
        final = finish or ("tool_calls" if index else "stop")
        if final == "stop" and index:
            final = "tool_calls"
        yield CanonicalChunk(
            finish_reason=final,
            usage=self.normalize_usage(usage) if usage else Usage(estimated=True),
        )

    async def embed(self, req: EmbedRequest) -> EmbedResponse:
        requests: list[dict[str, Any]] = []
        for text in req.inputs:
            item: dict[str, Any] = {
                "model": f"models/{req.model}",
                "content": {"parts": [{"text": text}]},
            }
            if req.dimensions:
                item["outputDimensionality"] = req.dimensions
            requests.append(item)
        try:
            resp = await self._http.post(
                self._url(req.model, "batchEmbedContents"),
                content=orjson.dumps({"requests": requests}),
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            raise transport_error(exc, self._name) from exc
        if resp.status_code >= 400:
            raise self._status_error(resp)
        try:
            vectors = tuple(
                tuple(float(x) for x in e["values"]) for e in json_body(resp)["embeddings"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise malformed(self._name) from exc
        # The embeddings API reports no token counts; estimate at ~4 characters per token.
        estimated = sum(max(1, len(t) // 4) for t in req.inputs)
        return EmbedResponse(
            model=req.model, vectors=vectors, usage=Usage(input_tokens=estimated, estimated=True)
        )

    def normalize_usage(self, raw: Mapping[str, Any]) -> Usage:
        prompt = int(raw.get("promptTokenCount") or 0) + int(
            raw.get("toolUsePromptTokenCount") or 0
        )
        thoughts = int(raw.get("thoughtsTokenCount") or 0)
        cached = int(raw.get("cachedContentTokenCount") or 0)
        return Usage(
            input_tokens=prompt,
            output_tokens=int(raw.get("candidatesTokenCount") or 0) + thoughts,
            cached_tokens=min(cached, prompt),
            reasoning_tokens=thoughts,
        )

    def classify_error(self, exc: BaseException) -> ProviderError:
        return transport_error(exc, self._name)

    def _status_error(self, resp: httpx2.Response) -> ProviderError:
        error = json_body(resp).get("error")
        error = error if isinstance(error, dict) else {}
        message = str(error.get("message") or resp.text[:300] or "provider error")
        status_name = str(error.get("status") or "")
        status = resp.status_code
        kind = _STATUS_KIND.get(status_name)
        if kind is None:
            if status == 429:
                kind = ProviderErrorKind.RATE_LIMIT
            elif status in (401, 403):
                kind = ProviderErrorKind.AUTH
            elif "token" in message.lower() and (
                "exceed" in message.lower() or "limit" in message.lower()
            ):
                kind = ProviderErrorKind.CTX_OVERFLOW
            elif 400 <= status < 500:
                kind = ProviderErrorKind.BAD_REQUEST
            else:
                kind = ProviderErrorKind.UNAVAILABLE
        return ProviderError(
            kind, message, provider=self._name, status=status, retry_after_s=retry_after(resp)
        )
