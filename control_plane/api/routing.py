"""Routing endpoints (US-19): `GET`/`PUT /v1/admin/routing-settings` and
`GET /v1/reports/routing` (the judged agreement of routed answers against the tolerance)."""

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from control_plane.services.routing import QualityReport
from gateway.api.deps import AuthorizedPrincipal, request_meta
from gateway.domain.routing import RoutingSettings

router = APIRouter(tags=["routing"])


class RoutingOut(BaseModel):
    large_model: str | None
    small_model: str | None
    threshold: float
    judge_rate: float
    tolerance: float
    opt_out_teams: list[str]


class RoutingIn(BaseModel):
    """Any subset of the settings; omitted ones keep their current value."""

    model_config = ConfigDict(extra="forbid")
    large_model: str | None = Field(default=None, min_length=1, max_length=200)
    small_model: str | None = Field(default=None, min_length=1, max_length=200)
    threshold: float | None = Field(default=None, ge=0, le=1)
    judge_rate: float | None = Field(default=None, ge=0, le=1)
    tolerance: float | None = Field(default=None, ge=0, le=0.5)
    opt_out_teams: list[UUID] | None = Field(default=None, max_length=1000)


class QualityOut(BaseModel):
    object: Literal["routing_report"] = "routing_report"
    start: date
    end: date
    samples: int
    mean_agreement: float | None
    tolerance: float
    within_tolerance: bool | None
    below_tolerance: int
    routed: int
    kept_on_large: int

    @classmethod
    def of(cls, report: QualityReport) -> QualityOut:
        q = report.quality
        return cls(
            start=report.start,
            end=report.end,
            samples=q.samples,
            mean_agreement=q.mean_agreement,
            tolerance=report.tolerance,
            within_tolerance=report.within_tolerance,
            below_tolerance=q.below,
            routed=q.routed,
            kept_on_large=q.kept,
        )


def _out(settings: RoutingSettings) -> RoutingOut:
    return RoutingOut(**settings.to_dict())


@router.get("/v1/admin/routing-settings")
async def get_routing_settings(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> RoutingOut:
    return _out(await services.routing_service.settings(principal, request_meta(request)))


@router.put("/v1/admin/routing-settings")
async def set_routing_settings(
    body: RoutingIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> RoutingOut:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    settings = await services.routing_service.update(principal, changes, request_meta(request))
    return _out(settings)


@router.get("/v1/reports/routing")
async def routing_report(
    request: Request, principal: AuthorizedPrincipal, services: Services, start: date, end: date
) -> QualityOut:
    report = await services.routing_service.quality(
        principal, start=start, end=end, meta=request_meta(request)
    )
    return QualityOut.of(report)
