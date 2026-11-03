"""Budget rules: periods in the org timezone, applicability, lock order, thresholds."""

from datetime import UTC, date, datetime
from uuid import uuid7

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.domain.budgets import (
    Budget,
    Demand,
    applicable,
    org_zone,
    period_start,
    soft_threshold_crossed,
    validate_limit,
)
from gateway.domain.errors import InvalidInput

TEAM, OTHER_TEAM, USER = uuid7(), uuid7(), uuid7()


def _b(scope: str, team: object = None, user: object = None) -> Budget:
    return Budget(uuid7(), scope, team, user, "month", "usd", 1_000_000, 80)  # type: ignore[arg-type]


def test_periods_use_the_org_timezone() -> None:
    late_utc = datetime(2026, 11, 30, 20, 0, tzinfo=UTC)  # already 1 Dec in Kolkata (UTC+5:30)
    kolkata = org_zone("Asia/Kolkata")
    assert period_start("month", late_utc, kolkata) == date(2026, 12, 1)
    assert period_start("month", late_utc, org_zone("UTC")) == date(2026, 11, 1)
    assert period_start("day", late_utc, kolkata) == date(2026, 12, 1)
    assert period_start("week", datetime(2026, 11, 5, tzinfo=UTC), org_zone("UTC")) == date(
        2026, 11, 2
    )
    assert org_zone("Not/AZone").key == "UTC"


def test_only_budgets_of_this_caller_apply_in_lock_order() -> None:
    org, team, mine = _b("org"), _b("team", TEAM), _b("member", TEAM, USER)
    foreign = [_b("team", OTHER_TEAM), _b("member", TEAM, uuid7()), _b("member", OTHER_TEAM, USER)]
    chosen = applicable([org, *foreign, team, mine], team_id=TEAM, user_id=USER)
    assert chosen == [mine, team, org]
    assert applicable([org, team, mine], team_id=None, user_id=None) == [org]


def test_demand_by_unit() -> None:
    demand = Demand(usd_micro=1234, tokens=99, images=2)
    assert (demand.amount("usd"), demand.amount("tokens"), demand.amount("images")) == (1234, 99, 2)


@pytest.mark.parametrize("bad", [-1, 10**15 + 1])
def test_limits_are_bounded(bad: int) -> None:
    with pytest.raises(InvalidInput):
        validate_limit(bad)


@given(st.integers(0, 10**12), st.integers(1, 10**12), st.integers(1, 100))
def test_soft_threshold_is_exact_integer_math(committed: int, limit: int, pct: int) -> None:
    assert soft_threshold_crossed(committed, limit, pct) == (committed * 100 >= limit * pct)
