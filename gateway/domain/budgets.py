"""Budget rules (spec section 6): hierarchy, periods in the org timezone, reservation amounts.

Pure logic. The atomic check-and-reserve itself is a conditional UPDATE in the database; this
module decides which budgets apply, in which order to lock them, and how much each must hold.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gateway.domain.errors import DomainError, InvalidInput

Scope = Literal["org", "team", "member"]
Period = Literal["day", "week", "month"]
Unit = Literal["usd", "tokens", "images"]
_LOCK_ORDER: dict[str, int] = {"member": 0, "team": 1, "org": 2}
MAX_LIMIT = 10**15


class BudgetExceeded(DomainError):
    kind = "budget_exceeded"
    default_code = "budget_exceeded"


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
class Demand:
    """The worst case one request can consume, per unit."""

    usd_micro: int
    tokens: int
    images: int = 0

    def amount(self, unit: Unit) -> int:
        return {"usd": self.usd_micro, "tokens": self.tokens, "images": self.images}[unit]


def org_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def period_start(period: Period, now: datetime, tz: ZoneInfo) -> date:
    local = now.astimezone(tz).date()
    if period == "day":
        return local
    if period == "week":
        return local - timedelta(days=local.weekday())  # ISO week: Monday
    return local.replace(day=1)


def applicable(
    budgets: list[Budget], *, team_id: UUID | None, user_id: UUID | None
) -> list[Budget]:
    """Every budget that constrains this caller, in the global lock order (member, team, org;
    then by id) so concurrent reservations never deadlock."""
    chosen = [
        b
        for b in budgets
        if b.scope_type == "org"
        or (b.scope_type == "team" and team_id is not None and b.team_id == team_id)
        or (
            b.scope_type == "member"
            and user_id is not None
            and b.team_id == team_id
            and b.user_id == user_id
        )
    ]
    return sorted(chosen, key=lambda b: (_LOCK_ORDER[b.scope_type], b.id))


def validate_limit(limit_amount: int) -> int:
    if not 0 <= limit_amount <= MAX_LIMIT:
        raise InvalidInput(
            f"limit must be between 0 and {MAX_LIMIT}", code="invalid_limit", param="limit_amount"
        )
    return limit_amount


def soft_threshold_crossed(committed: int, limit_amount: int, soft_pct: int) -> bool:
    return limit_amount > 0 and committed * 100 >= limit_amount * soft_pct
