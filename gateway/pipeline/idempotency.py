"""Stored results for Idempotency-Key replays (spec section 6). The first attempt's canonical
result is kept with the request that produced it, so a retry renders the very same response."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import orjson

from gateway.domain.llm import CanonicalResponse, EmbedResponse, ToolCall, Usage


@dataclass(frozen=True, slots=True)
class Origin:
    """The request that produced a result: this one, or the first one with the same key."""

    request_id: str
    at: datetime
    replayed: bool = False
    model: str | None = None  # the model that answered, when the router picked another


def _envelope(origin: Origin, cost: int, body: dict[str, Any]) -> bytes:
    served = {"served": origin.model} if origin.model else {}
    return orjson.dumps(
        {
            "request_id": origin.request_id,
            "at": origin.at.isoformat(),
            "cost": cost,
            **served,
            **body,
        }
    )


def _origin(stored: dict[str, Any]) -> Origin:
    at, served = datetime.fromisoformat(stored["at"]), stored.get("served")
    model = str(served) if served else None
    return Origin(str(stored["request_id"]), at, replayed=True, model=model)


def dump_chat(origin: Origin, cost: int, response: CanonicalResponse) -> bytes:
    return _envelope(
        origin,
        cost,
        {
            "id": response.id,
            "model": response.model,
            "content": response.content,
            "tool_calls": [[c.id, c.name, c.arguments] for c in response.tool_calls],
            "finish_reason": response.finish_reason,
            "usage": asdict(response.usage),
        },
    )


def load_chat(raw: bytes) -> tuple[CanonicalResponse, int, Origin]:
    stored = orjson.loads(raw)
    response = CanonicalResponse(
        id=stored["id"],
        model=stored["model"],
        content=stored["content"],
        tool_calls=tuple(ToolCall(*c) for c in stored["tool_calls"]),
        finish_reason=stored["finish_reason"],
        usage=Usage(**stored["usage"]),
    )
    return response, int(stored["cost"]), _origin(stored)


def dump_embed(origin: Origin, cost: int, response: EmbedResponse) -> bytes:
    return _envelope(
        origin,
        cost,
        {"model": response.model, "vectors": response.vectors, "usage": asdict(response.usage)},
    )


def load_embed(raw: bytes) -> tuple[EmbedResponse, int, Origin]:
    stored = orjson.loads(raw)
    response = EmbedResponse(
        model=stored["model"],
        vectors=tuple(tuple(float(x) for x in v) for v in stored["vectors"]),
        usage=Usage(**stored["usage"]),
    )
    return response, int(stored["cost"]), _origin(stored)
