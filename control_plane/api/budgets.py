"""Budget endpoints (US-07, US-08): limits, alerts, settings and requests for more budget."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import AuthorizedPrincipal, request_meta
from gateway.domain.budgets import (
    MAX_LIMIT,
    BudgetChange,
    BudgetRequest,
    BudgetSettings,
    BudgetStatus,
)

router = APIRouter(prefix="/v1/admin", tags=["budgets"])
requests_router = APIRouter(prefix="/v1/budget-requests", tags=["budgets"])


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_type: Literal["org", "team", "member"]
    team_id: UUID | None = None
    user_id: UUID | None = None
    period: Literal["day", "week", "month"]
    unit: Literal["usd", "tokens", "images"]
    limit_amount: int = Field(ge=0, le=MAX_LIMIT)
    soft_pct: int = Field(default=80, ge=1, le=100)
    reason: str | None = Field(default=None, max_length=500)


class BudgetOut(BaseModel):
    id: UUID
    scope_type: str
    team_id: UUID | None
    user_id: UUID | None
    period: str
    unit: str
    limit_amount: int
    soft_pct: int
    period_start: date
    committed: int
    reserved: int
    granted: int
    remaining: int


class BudgetList(BaseModel):
    data: list[BudgetOut]


class AlertOut(BaseModel):
    id: UUID
    budget_id: UUID
    scope_type: str
    team_id: UUID | None
    user_id: UUID | None
    period: str
    unit: str
    period_start: date
    kind: str
    committed: int
    limit_amount: int
    created_at: datetime


class AlertList(BaseModel):
    data: list[AlertOut]


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    allocation: Literal["overcommit", "strict"] | None = None


class SettingsOut(BaseModel):
    timezone: str
    allocation: str


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    budget_id: UUID
    amount: int = Field(gt=0, le=MAX_LIMIT)
    reason: str = Field(min_length=1, max_length=1000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approve", "deny"]
    grant_type: Literal["one_time", "permanent"] | None = None
    note: str | None = Field(default=None, max_length=1000)


class RequestOut(BaseModel):
    id: UUID
    budget_id: UUID
    team_id: UUID
    user_id: UUID
    amount: int
    unit: str
    reason: str
    status: str
    grant_type: str | None
    decided_by: UUID | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime


class RequestList(BaseModel):
    data: list[RequestOut]


def budget_out(s: BudgetStatus) -> BudgetOut:
    b = s.budget
    return BudgetOut(
        id=b.id,
        scope_type=b.scope_type,
        team_id=b.team_id,
        user_id=b.user_id,
        period=b.period,
        unit=b.unit,
        limit_amount=b.limit_amount,
        soft_pct=b.soft_pct,
        period_start=s.period_start,
        committed=s.committed,
        reserved=s.reserved,
        granted=s.granted,
        remaining=s.remaining,
    )


def _settings_out(s: BudgetSettings) -> SettingsOut:
    return SettingsOut(timezone=s.timezone, allocation=s.allocation)


def _request_out(r: BudgetRequest) -> RequestOut:
    return RequestOut.model_validate(r, from_attributes=True)


@router.put("/budgets")
async def set_budget(
    body: BudgetIn,
    request: Request,
    response: Response,
    principal: AuthorizedPrincipal,
    services: Services,
) -> BudgetOut:
    change = BudgetChange(
        scope_type=body.scope_type,
        team_id=body.team_id,
        user_id=body.user_id,
        period=body.period,
        unit=body.unit,
        limit_amount=body.limit_amount,
        soft_pct=body.soft_pct,
        reason=body.reason,
    )
    status, created = await services.budget_service.set_budget(
        principal, change, request_meta(request)
    )
    response.status_code = 201 if created else 200
    return budget_out(status)


@router.get("/budgets")
async def list_budgets(
    request: Request,
    principal: AuthorizedPrincipal,
    services: Services,
    team_id: UUID | None = None,
) -> BudgetList:
    budgets = await services.budget_service.list_budgets(principal, team_id, request_meta(request))
    return BudgetList(data=[budget_out(s) for s in budgets])


@router.delete("/budgets/{budget_id}", status_code=204)
async def delete_budget(
    budget_id: UUID, request: Request, principal: AuthorizedPrincipal, services: Services
) -> None:
    await services.budget_service.delete_budget(principal, budget_id, request_meta(request))


@router.get("/budget-alerts")
async def list_alerts(
    request: Request,
    principal: AuthorizedPrincipal,
    services: Services,
    team_id: UUID | None = None,
) -> AlertList:
    alerts = await services.budget_service.list_alerts(principal, team_id, request_meta(request))
    return AlertList(
        data=[
            AlertOut(
                id=a.id,
                budget_id=a.budget.id,
                scope_type=a.budget.scope_type,
                team_id=a.budget.team_id,
                user_id=a.budget.user_id,
                period=a.budget.period,
                unit=a.budget.unit,
                period_start=a.period_start,
                kind=a.kind,
                committed=a.committed,
                limit_amount=a.limit_amount,
                created_at=a.created_at,
            )
            for a in alerts
        ]
    )


@router.get("/budget-settings")
async def get_settings(principal: AuthorizedPrincipal, services: Services) -> SettingsOut:
    return _settings_out(await services.budget_service.settings(principal))


@router.put("/budget-settings")
async def update_settings(
    body: SettingsIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> SettingsOut:
    new = await services.budget_service.update_settings(
        principal, timezone=body.timezone, allocation=body.allocation, meta=request_meta(request)
    )
    return _settings_out(new)


@requests_router.post("", status_code=201)
async def request_more(
    body: RequestIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> RequestOut:
    created = await services.budget_service.request_more(
        principal,
        budget_id=body.budget_id,
        amount=body.amount,
        reason=body.reason,
        meta=request_meta(request),
    )
    return _request_out(created)


@requests_router.get("")
async def list_requests(
    principal: AuthorizedPrincipal,
    services: Services,
    status: Literal["pending", "approved", "denied"] | None = None,
) -> RequestList:
    found = await services.budget_service.list_requests(principal, status=status)
    return RequestList(data=[_request_out(r) for r in found])


@requests_router.post("/{request_id}/decision")
async def decide(
    request_id: UUID,
    body: DecisionIn,
    request: Request,
    principal: AuthorizedPrincipal,
    services: Services,
) -> RequestOut:
    decided = await services.budget_service.decide(
        principal,
        request_id,
        approve=body.decision == "approve",
        grant=body.grant_type,
        note=body.note,
        meta=request_meta(request),
    )
    return _request_out(decided)
