"""Optimization stages (spec section 4, "Stage contract"; US-26).

Every optimization is a stage with the same shape, so a new technique plugs in without touching
the pipeline: it reads the request, may produce a cheaper equivalent, and reports what that
saved. A stage runs `off`, in `shadow` (on a copy: it reports what it would have saved and its
result is dropped) or `on`. It runs `on` only for the version whose eval run passed; any other
version of it runs in shadow. Reports are generic rows, so the savings report shows a new
technique without a dashboard change.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal
from uuid import UUID

from gateway.domain.llm import CanonicalRequest, Usage
from gateway.domain.pricing import ModelPrice

Phase = Literal["pre_auth", "pre_llm", "post_llm", "async"]
Mode = Literal["off", "shadow", "on"]
MODES: Final[tuple[Mode, ...]] = ("off", "shadow", "on")
STAGE_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True, slots=True)
class StageReport:
    """What one stage did to one request, or would have done in shadow mode."""

    stage: str
    version: str
    phase: Phase
    tokens_before: int
    tokens_after: int
    cost_delta_micro_usd: int  # negative when the stage saves money
    latency_ms: int = 0
    quality_risk: float = 0.0  # from 0 (no risk to the answer) to 1
    applied: bool = False  # the stage changed the request (or would have, in shadow mode)
    shadow: bool = False  # it ran on a copy and nothing it produced was used
    decisions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if min(self.tokens_before, self.tokens_after, self.latency_ms) < 0:
            raise ValueError("token counts and latency cannot be negative")
        if not 0.0 <= self.quality_risk <= 1.0:
            raise ValueError("quality_risk is between 0 and 1")


@dataclass(frozen=True, slots=True)
class StageSetting:
    """An org's choice for one stage; `on` holds the version whose eval run passed."""

    mode: Mode
    version: str | None = None


def stage_settings(raw: Mapping[str, Any] | None) -> dict[str, StageSetting]:
    """Stored settings, read leniently: an unknown shape falls back to the stage's default."""
    settings: dict[str, StageSetting] = {}
    for name, value in (raw or {}).items():
        if isinstance(value, Mapping) and value.get("mode") in MODES:
            version = value.get("version")
            settings[str(name)] = StageSetting(value["mode"], str(version) if version else None)
    return settings


def effective_mode(setting: StageSetting | None, *, version: str, default: Mode) -> Mode:
    """`on` counts only for the version whose eval passed: a new version runs in shadow."""
    if setting is None:
        return default
    if setting.mode == "on" and setting.version != version:
        return "shadow"
    return setting.mode


@dataclass(slots=True)
class RequestContext:
    """What a stage reads and may change: the request about to be sent, and how it is priced."""

    org_id: UUID
    request: CanonicalRequest
    price: ModelPrice  # the router may pick a cheaper model of the same provider
    team_id: UUID | None = None

    def fork(self) -> RequestContext:
        """A copy to work on: requests are immutable, so the original cannot change through it."""
        return replace(self)

    def input_cost(self, tokens: int) -> int:
        """What `tokens` input tokens cost at this request's price (micro-USD)."""
        return self.price.cost(Usage(input_tokens=tokens))
