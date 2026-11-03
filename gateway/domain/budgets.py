"""Budget rules (spec section 6): limits, scopes and what one request may consume.

Pure logic. The atomic check-and-reserve is a single SQL statement (adapters/postgres/budgets.py)
and period boundaries are SQL functions (`budget_period_start`), so the request path, the usage
views and the janitor share one definition of "this period" in the org timezone.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from gateway.domain.errors import DomainError, InvalidInput
from gateway.domain.llm import CanonicalRequest, EmbedRequest, upper_bound_input_tokens
from gateway.domain.pricing import ModelPrice

Scope = Literal["org", "team", "member"]
Period = Literal["day", "week", "month"]
Unit = Literal["usd", "tokens", "images"]
Allocation = Literal["overcommit", "strict"]
GrantType = Literal["one_time", "permanent"]
RequestStatus = Literal["pending", "approved", "denied"]
MAX_LIMIT = 10**15
EMBED_INPUT_OVERHEAD = 2  # start and end markers an embedding tokenizer may add per input
_SHAPES: dict[str, tuple[bool, bool]] = {
    "org": (False, False),
    "team": (True, False),
    "member": (True, True),
}


class BudgetExceeded(DomainError):
    kind = "budget_exceeded"
    default_code = "budget_exceeded"


def validate_limit(limit_amount: int) -> int:
    if not 0 <= limit_amount <= MAX_LIMIT:
        raise InvalidInput(
            f"limit must be between 0 and {MAX_LIMIT}", code="invalid_limit", param="limit_amount"
        )
    return limit_amount


@dataclass(frozen=True, slots=True)
class Budget:
    id: UUID
    scope_type: Scope
    team_id: UUID | None
    user_id: UUID | None
    period: Period
    unit: Unit
    limit_amount: int
    soft_pct: int


@dataclass(frozen=True, slots=True)
class BudgetChange:
    """A requested limit for one scope, period and unit (an upsert by that natural key)."""

    scope_type: Scope
    team_id: UUID | None
    user_id: UUID | None
    period: Period
    unit: Unit
    limit_amount: int
    soft_pct: int = 80
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.team_id is not None, self.user_id is not None) != _SHAPES[self.scope_type]:
            raise InvalidInput(
                "an org budget takes no team or user, a team budget takes a team_id only and a"
                " member budget takes a team_id and a user_id",
                code="invalid_scope",
                param="scope_type",
            )
        validate_limit(self.limit_amount)
        if not 1 <= self.soft_pct <= 100:
            raise InvalidInput("soft_pct must be between 1 and 100", param="soft_pct")


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """A budget with its counters for the current period."""

    budget: Budget
    period_start: date
    committed: int
    reserved: int
    granted: int  # one-time approvals for this period only

    @property
    def effective_limit(self) -> int:
        return self.budget.limit_amount + self.granted

    @property
    def remaining(self) -> int:
        return max(self.effective_limit - self.committed - self.reserved, 0)


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    timezone: str
    allocation: Allocation


@dataclass(frozen=True, slots=True)
class Demand:
    """What one request may consume (the worst case before the call, the actual after), per unit."""

    usd_micro: int
    tokens: int
    images: int = 0

    def amount(self, unit: Unit) -> int:
        if unit == "usd":
            return self.usd_micro
        return self.tokens if unit == "tokens" else self.images


def chat_demand(entry: ModelPrice, req: CanonicalRequest) -> Demand:
    """Spec section 6, step 1: input at the dearest input price plus every allowed output token."""
    prompt = upper_bound_input_tokens(req)
    return Demand(
        usd_micro=entry.worst_case(input_tokens=prompt, max_output_tokens=req.max_tokens),
        tokens=prompt + req.max_tokens,
    )


def embed_demand(entry: ModelPrice, req: EmbedRequest) -> Demand:
    prompt = sum(len(text.encode()) + EMBED_INPUT_OVERHEAD for text in req.inputs)
    return Demand(
        usd_micro=entry.worst_case(input_tokens=prompt, max_output_tokens=0), tokens=prompt
    )


def check_member_limit(
    limit: int, *, team_cap: int | None, others: int, strict: bool, capped: bool
) -> None:
    """Spec section 6: a team lead (`capped`) never sets a member above the team cap, and under
    strict allocation the member limits of a team never sum above it. No team cap, no constraint.
    `others` is the sum of the team's other member limits for the same period and unit."""
    if team_cap is None:
        return
    if capped and limit > team_cap:
        raise InvalidInput(
            f"the limit exceeds the team cap of {team_cap}",
            code="above_team_cap",
            param="limit_amount",
        )
    if strict and others + limit > team_cap:
        raise InvalidInput(
            f"member limits would total {others + limit}, above the team cap of {team_cap}"
            " under strict allocation",
            code="allocation_exceeded",
            param="limit_amount",
        )


def check_team_cap(cap: int, *, members: int) -> None:
    """Under strict allocation a team cap never drops below the sum of its member limits."""
    if members > cap:
        raise InvalidInput(
            f"member limits total {members}, above a team cap of {cap} under strict allocation;"
            " lower them first",
            code="allocation_exceeded",
            param="limit_amount",
        )


@dataclass(frozen=True, slots=True)
class BudgetRequest:
    """A member's request for more budget on a budget that applies to them."""

    id: UUID
    budget_id: UUID
    team_id: UUID
    user_id: UUID
    amount: int
    unit: Unit
    reason: str
    status: RequestStatus
    grant_type: GrantType | None
    decided_by: UUID | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime
