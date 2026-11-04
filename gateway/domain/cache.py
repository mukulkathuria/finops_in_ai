"""Exact and semantic response caching (spec section 4, step 6 and rule 2; US-18).

An answer is served from the cache only inside the org and the permission scope it was made in:
the key covers the org, the caller's scope (team and user), the model and every sampling
parameter, the system prompt, the conversation, and the org's guardrail policy, so an answer
filtered under one policy is never served under a stricter one. The semantic cache matches a
single-turn question by meaning inside that same partition, above a similarity threshold.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from gateway.domain.detectors import RULES_VERSION
from gateway.domain.llm import CanonicalRequest, CanonicalResponse, Message, TextPart, Usage

CacheMode = Literal["off", "exact", "semantic"]
CACHE_MODES: Final[tuple[CacheMode, ...]] = ("off", "exact", "semantic")
CACHE_STAGES: Final = frozenset(
    {"exact_cache", "semantic_cache"}
)  # their savings are avoided calls
MAX_TTL_S: Final = 30 * 86_400
_USAGE_FIELDS: Final = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")


@dataclass(frozen=True, slots=True)
class CacheSettings:
    mode: CacheMode = "off"
    ttl_s: int = 86_400
    threshold: float = 0.95  # the cosine similarity a semantic match needs
    provider_cache: bool = True  # mark stable prompt prefixes for the provider's cache

    def __post_init__(self) -> None:
        if self.mode not in CACHE_MODES:
            raise ValueError(f"mode is one of {', '.join(CACHE_MODES)}")
        if not 60 <= self.ttl_s <= MAX_TTL_S:
            raise ValueError("ttl_s is between 60 seconds and 30 days")
        if not 0.8 <= self.threshold <= 1.0:
            raise ValueError("threshold is between 0.8 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "ttl_s": self.ttl_s,
            "threshold": self.threshold,
            "provider_cache": self.provider_cache,
        }

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None) -> CacheSettings:
        """Stored settings, read leniently: anything unusable means the cache is off."""
        if not raw:
            return cls()
        try:
            return cls(
                mode=raw.get("mode", "off"),
                ttl_s=int(raw.get("ttl_s", 86_400)),
                threshold=float(raw.get("threshold", 0.95)),
                provider_cache=bool(raw.get("provider_cache", True)),
            )
        except TypeError, ValueError:
            return cls()


@dataclass(frozen=True, slots=True)
class CacheKeys:
    exact: bytes  # the whole request
    partition: bytes  # everything but the conversation: where a semantic match may be found


def _digest(*parts: str) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.digest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _message(m: Message) -> dict[str, Any]:
    return {
        "role": m.role,
        "content": [
            p.text.strip() if isinstance(p, TextPart) else {"image": p.url, "detail": p.detail}
            for p in m.content
        ],
        "tool_calls": [[c.id, c.name, c.arguments] for c in m.tool_calls],
        "tool_call_id": m.tool_call_id,
        "name": m.name,
    }


def cache_keys(
    *,
    org_id: UUID,
    team_id: UUID | None,
    user_id: UUID | None,
    req: CanonicalRequest,
    policy: Mapping[str, Any],
    rules: str = RULES_VERSION,
) -> CacheKeys:
    params = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "top_p": req.top_p,
        "stop": list(req.stop),
        "tools": [[t.name, t.description, t.parameters] for t in req.tools],
        "tool_choice": req.tool_choice,
        "forced_tool": req.forced_tool,
        "response_format": req.response_format,
        "seed": req.seed,
    }
    system = [_message(m) for m in req.messages if m.role == "system"]
    partition = _digest(
        "finops-cache-v1",
        rules,  # answers filtered under other detection rules never match
        str(org_id),
        str(team_id),
        str(user_id),
        _canonical(params),
        _canonical(system),
        _canonical(dict(policy)),
    )
    conversation = _canonical([_message(m) for m in req.messages])
    return CacheKeys(_digest(partition.hex(), conversation), partition)


def single_turn_question(req: CanonicalRequest) -> str | None:
    """The question of a single-turn, text-only request without tools: the only shape the
    semantic cache matches, since a history or a tool changes what an answer means."""
    turns = [m for m in req.messages if m.role != "system"]
    if req.tools or len(turns) != 1 or turns[0].role != "user":
        return None
    if not all(isinstance(p, TextPart) for p in turns[0].content):
        return None
    return turns[0].text().strip() or None


def cacheable(response: CanonicalResponse) -> bool:
    """Only complete, plain answers are kept."""
    return response.finish_reason == "stop" and not response.tool_calls and bool(response.content)


def response_record(response: CanonicalResponse) -> dict[str, Any]:
    usage = response.usage
    return {
        "model": response.model,
        "content": response.content,
        "usage": {name: getattr(usage, name) for name in _USAGE_FIELDS},
    }


def response_from(record: Mapping[str, Any], *, response_id: str) -> CanonicalResponse:
    usage = record.get("usage") or {}
    return CanonicalResponse(
        id=response_id,
        model=str(record.get("model") or ""),
        content=str(record.get("content") or ""),
        tool_calls=(),
        finish_reason="stop",
        usage=Usage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(usage.get("cached_tokens") or 0),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
        ),
    )
