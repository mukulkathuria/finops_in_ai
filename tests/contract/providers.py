"""Recorded provider wire fixtures and a harness per adapter (spec section 3: contract tests).

Each scenario is selected by the model name in the request, so one MockTransport serves every
scenario. Fixtures follow each provider's documented response shapes.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx2
import orjson

from gateway.adapters.llm.anthropic import AnthropicAdapter
from gateway.adapters.llm.gemini import GeminiAdapter
from gateway.adapters.llm.openai_compat import OpenAICompatAdapter
from gateway.domain.llm import ProviderErrorKind, Usage
from gateway.ports.llm import LLMAdapter


@dataclass
class Reply:
    status: int = 200
    body: dict[str, Any] | None = None
    sse: list[str] | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> httpx2.Response:
        if self.sse is not None:
            text = "".join(line + "\n" for line in self.sse) + "\n"
            headers = {"content-type": "text/event-stream", **self.headers}
            return httpx2.Response(self.status, content=text.encode(), headers=headers)
        return httpx2.Response(self.status, json=self.body or {}, headers=self.headers)


Routes = dict[tuple[str, bool], Reply]  # (scenario model, streaming) -> reply


@dataclass
class Harness:
    name: str
    adapter: LLMAdapter
    requests: list[dict[str, Any]]
    expected_usage: Usage
    content_filter_raises: bool
    embeddings: bool


def _openai_sse(events: list[dict[str, Any]]) -> list[str]:
    return [f"data: {orjson.dumps(e).decode()}\n" for e in events] + ["data: [DONE]\n"]


def _anthropic_sse(events: list[dict[str, Any]]) -> list[str]:
    return [f"event: {e['type']}\ndata: {orjson.dumps(e).decode()}\n" for e in events]


def _gemini_sse(events: list[dict[str, Any]]) -> list[str]:
    return [f"data: {orjson.dumps(e).decode()}\n" for e in events]


# ---------------------------------------------------------------- OpenAI-compatible
def _oa_chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


OA_TOOL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
}
OPENAI: Routes = {
    ("text", False): Reply(
        body={
            "id": "c1",
            "object": "chat.completion",
            "model": "text",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello there!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        }
    ),
    ("text", True): Reply(
        sse=_openai_sse(
            [
                _oa_chunk({"role": "assistant", "content": ""}),
                _oa_chunk({"content": "Hello"}),
                _oa_chunk({"content": " there!"}),
                _oa_chunk({}, "stop"),
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
                },
            ]
        )
    ),
    ("tools", False): Reply(
        body={
            "id": "c2",
            "object": "chat.completion",
            "model": "tools",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None, "tool_calls": [OA_TOOL]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
        }
    ),
    ("tools", True): Reply(
        sse=_openai_sse(
            [
                _oa_chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ],
                    }
                ),
                _oa_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city": '}}]}),
                _oa_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
                _oa_chunk({}, "tool_calls"),
                {
                    "id": "c2",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
                },
            ]
        )
    ),
    ("usage", False): Reply(
        body={
            "id": "c3",
            "object": "chat.completion",
            "model": "usage",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 60},
                "completion_tokens_details": {"reasoning_tokens": 20},
            },
        }
    ),
    ("ratelimit", False): Reply(
        429,
        {
            "error": {
                "message": "Rate limit reached",
                "type": "requests",
                "code": "rate_limit_exceeded",
            }
        },
        headers={"retry-after": "7"},
    ),
    ("auth", False): Reply(
        401, {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}
    ),
    ("overloaded", False): Reply(503, {"error": {"message": "The server is overloaded"}}),
    ("ctx", False): Reply(
        400,
        {
            "error": {
                "message": "This model's maximum context length is 8192 tokens",
                "code": "context_length_exceeded",
            }
        },
    ),
    ("filter", False): Reply(
        400, {"error": {"message": "Rejected by safety system", "code": "content_policy_violation"}}
    ),
    ("malformed", True): Reply(
        sse=['data: {"choices": [{"delta": {"content": "hi"}}]}\n', "data: {not json\n"]
    ),
    ("embed", False): Reply(
        body={
            "object": "list",
            "model": "embed",
            "usage": {"prompt_tokens": 6, "total_tokens": 6},
            "data": [
                {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
            ],
        }
    ),
}


# ---------------------------------------------------------------- Anthropic Messages API
def _an_message(content: list[dict[str, Any]], stop: str, usage: dict[str, int]) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage,
    }


def _an_start(usage: dict[str, int]) -> dict[str, Any]:
    return {"type": "message_start", "message": _an_message([], None, usage)}  # type: ignore[arg-type]


def _an_error(
    status: int, etype: str, message: str, headers: dict[str, str] | None = None
) -> Reply:
    return Reply(
        status,
        {"type": "error", "error": {"type": etype, "message": message}},
        headers=headers or {},
    )


AN_USAGE = {
    "input_tokens": 12,
    "output_tokens": 4,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
ANTHROPIC: Routes = {
    ("text", False): Reply(
        body=_an_message([{"type": "text", "text": "Hello there!"}], "end_turn", AN_USAGE)
    ),
    ("text", True): Reply(
        sse=_anthropic_sse(
            [
                _an_start({**AN_USAGE, "output_tokens": 1}),
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {"type": "ping"},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " there!"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            ]
        )
    ),
    ("tools", False): Reply(
        body=_an_message(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                }
            ],
            "tool_use",
            {**AN_USAGE, "input_tokens": 20, "output_tokens": 9},
        )
    ),
    ("tools", True): Reply(
        sse=_anthropic_sse(
            [
                _an_start({**AN_USAGE, "input_tokens": 20, "output_tokens": 1}),
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city": '},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 9},
                },
                {"type": "message_stop"},
            ]
        )
    ),
    ("usage", False): Reply(
        body=_an_message(
            [{"type": "text", "text": "ok"}],
            "end_turn",
            {
                "input_tokens": 40,
                "output_tokens": 50,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 10,
            },
        )
    ),
    ("ratelimit", False): _an_error(429, "rate_limit_error", "Rate limited", {"retry-after": "7"}),
    ("auth", False): _an_error(401, "authentication_error", "invalid x-api-key"),
    ("overloaded", False): _an_error(529, "overloaded_error", "Overloaded"),
    ("ctx", False): _an_error(
        400, "invalid_request_error", "prompt is too long: 300000 tokens > 200000 maximum"
    ),
    ("filter", False): Reply(body=_an_message([], "refusal", AN_USAGE)),
    ("malformed", True): Reply(sse=["event: message_start\ndata: {not json\n"]),
}


# ---------------------------------------------------------------- Gemini REST
def _gm(
    parts: list[dict[str, Any]], finish: str | None, usage: dict[str, int] | None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                **({"finishReason": finish} if finish else {}),
            }
        ],
        "modelVersion": "gemini-test",
        "responseId": "r1",
    }
    if usage:
        out["usageMetadata"] = usage
    return out


GM_USAGE = {"promptTokenCount": 12, "candidatesTokenCount": 4, "totalTokenCount": 16}
GM_CALL = {
    "functionCall": {"name": "get_weather", "args": {"city": "Paris"}},
    "thoughtSignature": "c2lnbmF0dXJlLTE=",
}


def _gm_error(status: int, name: str, message: str, headers: dict[str, str] | None = None) -> Reply:
    return Reply(
        status,
        {"error": {"code": status, "message": message, "status": name}},
        headers=headers or {},
    )


GEMINI: Routes = {
    ("text", False): Reply(body=_gm([{"text": "Hello there!"}], "STOP", GM_USAGE)),
    ("text", True): Reply(
        sse=_gemini_sse(
            [_gm([{"text": "Hello"}], None, None), _gm([{"text": " there!"}], "STOP", GM_USAGE)]
        )
    ),
    ("tools", False): Reply(
        body=_gm([GM_CALL], "STOP", {**GM_USAGE, "promptTokenCount": 20, "candidatesTokenCount": 9})
    ),
    ("tools", True): Reply(
        sse=_gemini_sse(
            [
                _gm(
                    [GM_CALL],
                    "STOP",
                    {**GM_USAGE, "promptTokenCount": 20, "candidatesTokenCount": 9},
                )
            ]
        )
    ),
    ("usage", False): Reply(
        body=_gm(
            [{"text": "ok"}],
            "STOP",
            {
                "promptTokenCount": 100,
                "cachedContentTokenCount": 60,
                "candidatesTokenCount": 30,
                "thoughtsTokenCount": 20,
            },
        )
    ),
    ("ratelimit", False): _gm_error(
        429, "RESOURCE_EXHAUSTED", "Resource exhausted", {"retry-after": "7"}
    ),
    ("auth", False): _gm_error(401, "UNAUTHENTICATED", "API key not valid"),
    ("overloaded", False): _gm_error(503, "UNAVAILABLE", "The model is overloaded"),
    ("ctx", False): _gm_error(
        400,
        "INVALID_ARGUMENT",
        "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",
    ),
    ("filter", False): Reply(body={"promptFeedback": {"blockReason": "SAFETY"}}),
    ("malformed", True): Reply(sse=["data: {not json\n"]),
    ("embed", False): Reply(body={"embeddings": [{"values": [1.0, 0.0]}, {"values": [0.0, 1.0]}]}),
}


def _transport(
    routes: Routes,
    requests: list[dict[str, Any]],
    model_of: Callable[[httpx2.Request], tuple[str, bool]],
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = orjson.loads(request.content) if request.content else {}
        requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        scenario, streaming = model_of(request)
        if scenario == "timeout":
            raise httpx2.ReadTimeout("simulated timeout", request=request)
        reply = routes.get((scenario, streaming)) or routes.get((scenario, False))
        if reply is None:
            return httpx2.Response(404, json={"error": {"message": f"no fixture for {scenario}"}})
        return reply.response()

    return httpx2.MockTransport(handler)


def _body_model(request: httpx2.Request) -> tuple[str, bool]:
    body = orjson.loads(request.content) if request.content else {}
    return str(body.get("model")), bool(body.get("stream"))


def _gemini_model(request: httpx2.Request) -> tuple[str, bool]:
    path = unquote(request.url.path)
    model = path.split("/models/", 1)[1].split(":", 1)[0]
    return model, "streamGenerateContent" in path


def build(name: str) -> Harness:
    requests: list[dict[str, Any]] = []
    if name == "openai_compat":
        http = httpx2.AsyncClient(transport=_transport(OPENAI, requests, _body_model))
        adapter: LLMAdapter = OpenAICompatAdapter(
            base_url="https://api.example.test/v1", api_key="k", http=http
        )
        usage = Usage(input_tokens=100, output_tokens=50, cached_tokens=60, reasoning_tokens=20)
        return Harness(name, adapter, requests, usage, content_filter_raises=True, embeddings=True)
    if name == "anthropic":
        http = httpx2.AsyncClient(transport=_transport(ANTHROPIC, requests, _body_model))
        adapter = AnthropicAdapter(api_key="k", http=http, base_url="https://api.anthropic.test")
        usage = Usage(input_tokens=110, output_tokens=50, cached_tokens=60, cache_write_tokens=10)
        return Harness(
            name, adapter, requests, usage, content_filter_raises=False, embeddings=False
        )
    http = httpx2.AsyncClient(transport=_transport(GEMINI, requests, _gemini_model))
    adapter = GeminiAdapter(api_key="k", http=http, base_url="https://gemini.example.test")
    usage = Usage(input_tokens=100, output_tokens=50, cached_tokens=60, reasoning_tokens=20)
    return Harness(name, adapter, requests, usage, content_filter_raises=True, embeddings=True)


ERROR_SCENARIOS = [
    ("ratelimit", ProviderErrorKind.RATE_LIMIT),
    ("auth", ProviderErrorKind.AUTH),
    ("overloaded", ProviderErrorKind.UNAVAILABLE),
    ("ctx", ProviderErrorKind.CTX_OVERFLOW),
    ("timeout", ProviderErrorKind.TIMEOUT),
]
