"""`GET /v1/me/usage` (US-09): the caller's own usage and remaining budget, nobody else's.

Self-inspection like `/v1/whoami`: a user (JWT, with `usage:read_self`) or an API key bound to a
user sees that user's ledger totals for the current day, week and month (org timezone) and their
own member budgets. Team and org totals are not shown: they would reveal colleagues' spending.
"""

from datetime import date
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from gateway.api.caller import AnyCaller
from gateway.domain.errors import Forbidden
from gateway.domain.policy import Permission
from gateway.domain.pricing import usd
from gateway.pipeline.usage import UsageReporter

router = APIRouter(tags=["data-plane"])


class UsageServices(Protocol):
    @property
    def usage_reporter(self) -> UsageReporter: ...


def get_reporter(request: Request) -> UsageReporter:
    services: UsageServices = request.app.state.container
    return services.usage_reporter


class PeriodOut(BaseModel):
    period: str
    period_start: date
    period_end: date
    requests: int
    cost_micro_usd: int
    cost_usd: str
    input_tokens: int
    output_tokens: int
    images: int


class MyBudgetOut(BaseModel):
    id: UUID
    team_id: UUID | None
    period: str
    unit: str
    limit_amount: int
    granted: int
    soft_pct: int
    period_start: date
    committed: int
    reserved: int
    remaining: int


class MyUsage(BaseModel):
    user_id: UUID
    timezone: str
    usage: list[PeriodOut]
    budgets: list[MyBudgetOut]


@router.get("/v1/me/usage")
async def my_usage(
    caller: AnyCaller, reporter: Annotated[UsageReporter, Depends(get_reporter)]
) -> MyUsage:
    if caller.user_id is None:
        raise Forbidden(
            "this API key is not bound to a user; usage is reported per user",
            code="not_a_user_credential",
        )
    if caller.auth_method == "jwt" and not caller.can(Permission.USAGE_READ_SELF):
        raise Forbidden("you do not have permission for this action")
    report = await reporter.for_user(caller.org_id, caller.user_id)
    return MyUsage(
        user_id=caller.user_id,
        timezone=report.timezone,
        usage=[
            PeriodOut(
                period=p.period,
                period_start=p.period_start,
                period_end=p.period_end,
                requests=p.requests,
                cost_micro_usd=p.cost_micro_usd,
                cost_usd=usd(p.cost_micro_usd),
                input_tokens=p.input_tokens,
                output_tokens=p.output_tokens,
                images=p.images,
            )
            for p in report.periods
        ],
        budgets=[
            MyBudgetOut(
                id=s.budget.id,
                team_id=s.budget.team_id,
                period=s.budget.period,
                unit=s.budget.unit,
                limit_amount=s.budget.limit_amount,
                granted=s.granted,
                soft_pct=s.budget.soft_pct,
                period_start=s.period_start,
                committed=s.committed,
                reserved=s.reserved,
                remaining=s.remaining,
            )
            for s in report.budgets
        ],
    )
