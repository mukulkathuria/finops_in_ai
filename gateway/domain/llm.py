"""Canonical LLM types: the gateway's own vocabulary (spec section 3).

Vendor request and response types never leave their adapter; everything inside the gateway speaks
these types. Token accounting convention (all providers are normalised to it):
`input_tokens` counts every prompt token, *including* the cached and cache-write subsets;
`output_tokens` counts every generated token, *including* reasoning tokens.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]
ToolChoiceMode = Literal["auto", "none", "required"]


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str


@dataclass(frozen=True, slots=True)
class ImagePart:
    url: str  # https URL or a data: URL
    detail: str | None = None


ContentPart = TextPart | ImagePart


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as OpenAI-compatible clients send it


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: tuple[ContentPart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    def text(self) -> str:
        return "".join(p.text for p in self.content if isinstance(p, TextPart))


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    model: str  # the provider-side model name
    messages: tuple[Message, ...]
    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoiceMode | None = None
    forced_tool: str | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    # Mark the stable prefix (tools, system prompt, the conversation so far) for the
    # provider's prompt cache; providers that cache on their own ignore it.
    prompt_cache: bool = False


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # subset of input read from the provider prompt cache
    cache_write_tokens: int = 0  # subset of input written to the provider prompt cache
    reasoning_tokens: int = 0  # subset of output spent on reasoning
    estimated: bool = False  # the provider reported nothing; the gateway estimated

    def __post_init__(self) -> None:
        counts = (
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )
        if min(counts) < 0:
            raise ValueError("token counts are never negative")
        if self.cached_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("cached and cache-write tokens are subsets of input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens are a subset of output tokens")


@dataclass(frozen=True, slots=True)
class CanonicalResponse:
    id: str
    model: str
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: FinishReason
    usage: Usage


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class CanonicalChunk:
    content: str | None = None
    tool_calls: tuple[ToolCallDelta, ...] = ()
    finish_reason: FinishReason | None = None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class EmbedRequest:
    model: str
    inputs: tuple[str, ...]
    dimensions: int | None = None


@dataclass(frozen=True, slots=True)
class EmbedResponse:
    model: str
    vectors: tuple[tuple[float, ...], ...]
    usage: Usage


@dataclass(frozen=True, slots=True)
class ImageRequest:
    model: str
    prompt: str
    n: int = 1
    size: str | None = None  # "WxH"
    quality: str | None = None


@dataclass(frozen=True, slots=True)
class ImageResponse:
    model: str
    images: tuple[str, ...]  # base64-encoded image data
    revised_prompts: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    chat: bool = True
    embeddings: bool = False
    vision: bool = False
    tools: bool = True
    prompt_cache: bool = False
    sampling_params: bool = True  # False when the provider rejects temperature/top_p


class ProviderErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONTENT_FILTER = "content_filter"
    CTX_OVERFLOW = "ctx_overflow"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNAVAILABLE = "unavailable"
    OTHER = "other"

    @property
    def retryable(self) -> bool:
        return self in (
            ProviderErrorKind.RATE_LIMIT,
            ProviderErrorKind.TIMEOUT,
            ProviderErrorKind.UNAVAILABLE,
        )


class ProviderError(Exception):
    """Typed provider failure. Adapters raise only this; the pipeline decides retry and fallback."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        message: str,
        *,
        provider: str,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message[:500]
        self.provider = provider
        self.status = status
        self.retry_after_s = retry_after_s


PER_MESSAGE_OVERHEAD = 16  # role markers and chat-template tokens around each message
PER_TOOL_OVERHEAD = 600  # the tool-use preamble a provider adds when tools are present
PER_IMAGE_TOKENS = 6_000  # above any supported provider's per-image token charge


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def upper_bound_input_tokens(req: CanonicalRequest) -> int:
    """A ceiling on billed prompt tokens (spec section 6, step 1): a tokenizer emits at most one
    token per UTF-8 byte, plus fixed allowances for chat-template markers, the tool preamble and
    images. Over-estimating only holds more budget for the duration of the call."""
    total = PER_MESSAGE_OVERHEAD * len(req.messages)
    for m in req.messages:
        for part in m.content:
            total += len(part.text.encode()) if isinstance(part, TextPart) else PER_IMAGE_TOKENS
        for call in m.tool_calls:
            total += len(call.id.encode()) + len(call.name.encode()) + len(call.arguments.encode())
        total += len((m.tool_call_id or "").encode()) + len((m.name or "").encode())
    if req.tools:
        total += PER_TOOL_OVERHEAD + sum(
            len(t.name.encode()) + len(t.description.encode()) + _json_bytes(t.parameters)
            for t in req.tools
        )
    if req.response_format:
        total += _json_bytes(req.response_format)
    return total
