"""The optional LiteLLM adapter (spec section 3; ADR 0008), installed with the `litellm` extra.

It serves OpenAI-compatible providers with the native adapter's request and response mapping, and
LiteLLM makes each call through an OpenAI client on the gateway's shared HTTP client (the same
pool, timeouts and transports). Switching a provider between `native` and `litellm` is one line
of configuration, and both pass the same contract suite. Retries stay in the gateway's pipeline;
LiteLLM reads its model price map from the installed package instead of fetching it, and keeps
no message text for logging.
"""

import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

import httpx2
import openai

from gateway.adapters.llm.common import malformed, transport_error
from gateway.adapters.llm.openai_compat import OpenAICompatAdapter
from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    CanonicalResponse,
    EmbedRequest,
    EmbedResponse,
    ImageRequest,
    ImageResponse,
    ProviderError,
    ProviderErrorKind,
)


def _retry_after(exc: BaseException) -> float | None:
    """The provider's Retry-After: LiteLLM keeps the reply's headers beside a placeholder."""
    for headers in (
        getattr(exc, "litellm_response_headers", None),
        getattr(getattr(exc, "response", None), "headers", None),
        getattr(exc, "headers", None),
    ):
        raw = headers.get("retry-after") if isinstance(headers, Mapping) else None
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except TypeError, ValueError:
                return None
    return None


def _undecodable(exc: BaseException) -> bool:
    """Whether the provider's reply could not be decoded, however deep LiteLLM wrapped it."""
    seen: set[int] = set()
    todo: list[object] = [exc]
    while todo:
        current = todo.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, json.JSONDecodeError):
            return True
        todo += [
            current.__cause__,
            current.__context__,
            getattr(current, "original_exception", None),
        ]
    return False


def load_litellm() -> Any:
    """Import LiteLLM once, set up for a server: no `.env` file read into the process, no price
    map download, no debug banners and no message text kept for its logging."""
    os.environ.setdefault("LITELLM_MODE", "PRODUCTION")  # its default, DEV, loads a .env file
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # never fetched at import
    import litellm  # the optional extra (ADR 0008)

    litellm.suppress_debug_info = True
    litellm.turn_off_message_logging = True
    return litellm


class LiteLLMAdapter(OpenAICompatAdapter):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http: httpx2.AsyncClient,
        name: str = "litellm",
        timeout_s: float = 120.0,
        max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            http=http,
            name=name,
            timeout_s=timeout_s,
            max_tokens_field=max_tokens_field,
        )
        self._litellm: Any = load_litellm()  # already imported at startup by the registry
        self._client = openai.AsyncOpenAI(
            api_key=api_key, base_url=self._base, http_client=http, max_retries=0, timeout=timeout_s
        )

    def _arguments(self, body: dict[str, Any]) -> dict[str, Any]:
        """The native payload as LiteLLM arguments: an OpenAI-compatible model on our client."""
        model = body.pop("model")
        return {
            **body,
            "model": f"openai/{model}",
            "client": self._client,
            "api_base": self._base,
            "timeout": self._timeout,
            "num_retries": 0,
            "_skip_mcp_handler": True,  # plain function tools only: never LiteLLM's MCP gateway
        }

    async def complete(self, req: CanonicalRequest) -> CanonicalResponse:
        try:
            resp = await self._litellm.acompletion(
                **self._arguments(self.payload(req, stream=False))
            )
        except Exception as exc:
            raise self.classify_error(exc) from exc
        return self._completion(resp.model_dump(), req)

    async def stream(self, req: CanonicalRequest) -> AsyncIterator[CanonicalChunk]:
        chunks: Any = None
        try:
            chunks = await self._litellm.acompletion(
                **self._arguments(self.payload(req, stream=True))
            )
            async for chunk in chunks:
                for out in self._events(chunk.model_dump()):
                    yield out
        except Exception as exc:
            raise self.classify_error(exc) from exc
        finally:
            if chunks is not None:  # a client gone away stops the provider too
                await chunks.aclose()

    async def embed(self, req: EmbedRequest) -> EmbedResponse:
        arguments: dict[str, Any] = {
            "model": f"openai/{req.model}",
            "input": list(req.inputs),
            "encoding_format": "float",
            "client": self._client,
            "api_base": self._base,
            "timeout": self._timeout,
        }
        if req.dimensions:  # sent as the native adapter sends it: LiteLLM's model rules not applied
            arguments["extra_body"] = {"dimensions": req.dimensions}
        try:
            resp = await self._litellm.aembedding(**arguments)
        except Exception as exc:
            raise self.classify_error(exc) from exc
        return self._embedding(resp.model_dump(), req)

    async def generate_images(self, req: ImageRequest) -> ImageResponse:
        arguments: dict[str, Any] = {
            "model": f"openai/{req.model}",
            "prompt": req.prompt,
            "n": req.n,
            "size": req.size,
            "quality": req.quality,
            "client": self._client,
            "api_base": self._base,
            "timeout": self._timeout,
        }
        if not req.model.startswith("gpt-image"):  # raw, like dimensions: no model rules
            arguments["extra_body"] = {"response_format": "b64_json"}
        try:
            resp = await self._litellm.aimage_generation(**arguments)
        except Exception as exc:
            raise self.classify_error(exc) from exc
        return self._image_response(resp.model_dump(), req)

    def classify_error(self, exc: BaseException) -> ProviderError:
        """LiteLLM raises the OpenAI SDK's exception types: a status error is mapped from its
        status and body the way the native adapter maps a reply; the rest like transport errors."""
        if isinstance(exc, ProviderError):
            return exc
        if _undecodable(exc):  # a broken stream reaches us as a "service unavailable"
            return malformed(self._name)
        status = getattr(exc, "status_code", None)
        if isinstance(exc, self._litellm.ContextWindowExceededError):
            kind = ProviderErrorKind.CTX_OVERFLOW
            return ProviderError(
                kind, "context window exceeded", provider=self._name, status=status
            )
        if isinstance(exc, self._litellm.ContentPolicyViolationError):
            kind = ProviderErrorKind.CONTENT_FILTER
            return ProviderError(
                kind, "rejected by content policy", provider=self._name, status=status
            )
        if isinstance(exc, openai.APITimeoutError):
            return ProviderError(
                ProviderErrorKind.TIMEOUT, "provider timed out", provider=self._name
            )
        if isinstance(exc, openai.APIStatusError):
            # LiteLLM's own errors may skip the SDK's constructor: every attribute is optional.
            raw = getattr(exc, "body", None)
            body = raw if isinstance(raw, Mapping) else {}
            nested = body.get("error")
            error = nested if isinstance(nested, Mapping) else body
            details = {
                "message": error.get("message") or getattr(exc, "message", None) or str(exc),
                "code": error.get("code") or getattr(exc, "code", None),
            }
            return self._error_from_body(details, status=status, retry=_retry_after(exc))
        if isinstance(exc, openai.APIConnectionError):
            kind = ProviderErrorKind.UNAVAILABLE
            return ProviderError(kind, "provider unreachable", provider=self._name)
        if isinstance(exc, ValueError | TypeError | KeyError):  # an undecodable reply
            return malformed(self._name)
        return transport_error(exc, self._name)
