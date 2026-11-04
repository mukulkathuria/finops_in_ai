"""Savings endpoints (US-17): `GET`/`PUT /v1/admin/savings-settings` (the baseline model) and
`GET /v1/reports/savings` (baseline versus actual cost, by team, model, day and technique). Money
is in micro-USD (1 USD = 1,000,000)."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.adapters.postgres.savings import LedgerRow, TechniqueRow
from gateway.api.deps import AuthorizedPrincipal, request_meta

router = APIRouter(tags=["savings"])


class SavingsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_model: str | None = Field(default=None, min_length=1, max_length=200)


class LedgerOut(BaseModel):
    key: str
    label: str | None
    requests: int
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    baseline_micro_usd: int
    overhead_micro_usd: int
    savings_micro_usd: int

    @classmethod
    def of(cls, row: LedgerRow) -> LedgerOut:
        return cls(
            key=row.key,
            label=row.label,
            requests=row.requests,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_micro_usd=row.cost,
            baseline_micro_usd=row.baseline,
            overhead_micro_usd=row.overhead,
            savings_micro_usd=row.savings,
        )


class TechniqueOut(BaseModel):
    technique: str
    applied: int
    shadow_runs: int
    tokens_saved: int
    saved_micro_usd: int
    shadow_saved_micro_usd: int

    @classmethod
    def of(cls, row: TechniqueRow) -> TechniqueOut:
        return cls(
            technique=row.stage,
            applied=row.applied,
            shadow_runs=row.shadow_runs,
            tokens_saved=row.tokens_saved,
            saved_micro_usd=row.saved,
            shadow_saved_micro_usd=row.shadow_saved,
        )


class SavingsReportOut(BaseModel):
    object: Literal["savings_report"] = "savings_report"
    start: date
    end: date
    team_id: UUID | None
    totals: LedgerOut
    rows: list[LedgerOut]
    techniques: list[TechniqueOut]


@router.get("/v1/admin/savings-settings")
async def get_savings_settings(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> SavingsSettings:
    model = await services.savings_service.baseline_model(principal, request_meta(request))
    return SavingsSettings(baseline_model=model)


@router.put("/v1/admin/savings-settings")
async def set_savings_settings(
    body: SavingsSettings, request: Request, principal: AuthorizedPrincipal, services: Services
) -> SavingsSettings:
    model = await services.savings_service.set_baseline_model(
        principal, body.baseline_model, request_meta(request)
    )
    return SavingsSettings(baseline_model=model)


@router.get("/v1/reports/savings")
async def savings_report(
    request: Request,
    principal: AuthorizedPrincipal,
    services: Services,
    start: date,
    end: date,
    group_by: Annotated[Literal["team", "model", "day"] | None, Query()] = None,
    team_id: UUID | None = None,
) -> SavingsReportOut:
    report = await services.savings_service.report(
        principal,
        start=start,
        end=end,
        group_by=group_by,
        team_id=team_id,
        meta=request_meta(request),
    )
    return SavingsReportOut(
        start=report.start,
        end=report.end,
        team_id=report.team_id,
        totals=LedgerOut.of(report.totals),
        rows=[LedgerOut.of(r) for r in report.rows],
        techniques=[TechniqueOut.of(t) for t in report.techniques],
    )
