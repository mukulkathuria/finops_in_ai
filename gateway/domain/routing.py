"""Complexity routing (spec section 4, step 8; US-19): the rules that pick a model tier.

An easy request for the org's large tier can go to its small tier, a cheaper model of the same
provider, when that model can take the whole request. The router is an optimization stage: it
ships off, runs in shadow to show what it would save, and runs on only for the version whose
eval passed. A sampled share of the requests it routes (or, in shadow, would route) is answered
by the other tier too, and the agreement of the two answers is recorded; the routing report
compares it with the org's tolerance. A team can opt out.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from gateway.domain.llm import CanonicalRequest, TextPart, upper_bound_input_tokens
from gateway.domain.pricing import ModelPrice

ROUTER: Final = "router"
_REASONING: Final = re.compile(
    r"\b(?:prove|derive|step by step|analy[sz]e|design|architect|optimi[sz]e|debug|refactor"
    r"|explain why|compare|trade-?offs?|algorithm|complexity)\b",
    re.IGNORECASE,
)
_SCAN_CHARS: Final = 20_000  # reasoning words are looked for at the start of a long prompt


def _name(value: object) -> str | None:
    return str(value) if value else None


@dataclass(frozen=True, slots=True)
class RoutingSettings:
    large_model: str | None = None  # public names: the tier requests ask for, and the cheaper one
    small_model: str | None = None
    threshold: float = 0.35  # a request scoring below this goes to the small tier
    judge_rate: float = 0.05  # the share of routed requests the other tier answers too
    tolerance: float = 0.15  # how far the mean agreement may fall below 1
    opt_out_teams: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not (0.0 <= self.threshold <= 1.0 and 0.0 <= self.judge_rate <= 1.0):
            raise ValueError("threshold and judge_rate are between 0 and 1")
        if not 0.0 <= self.tolerance <= 0.5:
            raise ValueError("tolerance is between 0 and 0.5")
        if (self.large_model is None) != (self.small_model is None) or (
            self.large_model is not None and self.large_model == self.small_model
        ):
            raise ValueError("routing needs two different models: large_model and small_model")

    @property
    def configured(self) -> bool:
        return self.large_model is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "large_model": self.large_model,
            "small_model": self.small_model,
            "threshold": self.threshold,
            "judge_rate": self.judge_rate,
            "tolerance": self.tolerance,
            "opt_out_teams": list(self.opt_out_teams),
        }

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None) -> RoutingSettings:
        """Stored settings, read leniently: anything unusable leaves routing unconfigured."""
        if not raw:
            return cls()
        try:
            return cls(
                large_model=_name(raw.get("large_model")),
                small_model=_name(raw.get("small_model")),
                threshold=float(raw.get("threshold", 0.35)),
                judge_rate=float(raw.get("judge_rate", 0.05)),
                tolerance=float(raw.get("tolerance", 0.15)),
                opt_out_teams=tuple(str(t) for t in raw.get("opt_out_teams") or ()),
            )
        except TypeError, ValueError:
            return cls()


def complexity(req: CanonicalRequest) -> float:
    """How demanding a request looks, from 0 (a short lookup) to 1 (long, technical and deep in
    a conversation): its length, code or tools, reasoning words and turns. Deterministic."""
    text = "\n".join(
        p.text
        for m in req.messages
        if m.role != "system"
        for p in m.content
        if isinstance(p, TextPart)
    )
    score = min(len(text) / 8_000, 1.0) * 0.4
    if "```" in text or req.tools:
        score += 0.3
    score += min(len(_REASONING.findall(text[:_SCAN_CHARS])), 2) * 0.15
    turns = sum(1 for m in req.messages if m.role == "user")
    score += min(max(turns - 1, 0) / 10, 1.0) * 0.2
    return min(score, 1.0)


def fits(model: ModelPrice, req: CanonicalRequest) -> bool:
    """`model` can take the whole request: its prompt, counted at its ceiling, and the answer."""
    if model.max_output_tokens and req.max_tokens > model.max_output_tokens:
        return False
    window = model.context_window
    return not window or upper_bound_input_tokens(req) + req.max_tokens <= window
