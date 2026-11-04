"""Optimization stage endpoints (US-26): `GET /v1/admin/stages`, `PUT /v1/admin/stages/{name}`
and `POST /v1/admin/stages/{name}/evals`."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from control_plane.services.stages import EvalIn, StageView
from gateway.adapters.postgres.stages import EvalRun
from gateway.api.deps import AuthorizedPrincipal, request_meta

router = APIRouter(prefix="/v1/admin/stages", tags=["stages"])


class EvalOut(BaseModel):
    id: str
    dataset: str
    metric: str
    score: float
    threshold: float
    samples: int
    passed: bool
    created_at: int

    @classmethod
    def of(cls, run: EvalRun) -> EvalOut:
        return cls(
            id=str(run.id),
            dataset=run.dataset,
            metric=run.metric,
            score=run.score,
            threshold=run.threshold,
            samples=run.samples,
            passed=run.passed,
            created_at=int(run.created_at.timestamp()),
        )


class StageOut(BaseModel):
    name: str
    version: str
    phase: str
    mode: str
    default_mode: str
    latest_eval: EvalOut | None

    @classmethod
    def of(cls, view: StageView) -> StageOut:
        return cls(
            name=view.name,
            version=view.version,
            phase=view.phase,
            mode=view.mode,
            default_mode=view.default_mode,
            latest_eval=EvalOut.of(view.latest_eval) if view.latest_eval else None,
        )


class StageList(BaseModel):
    object: Literal["list"] = "list"
    data: list[StageOut]


class ModeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "shadow", "on"]


class EvalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=64)
    score: float = Field(ge=-1e12, le=1e12, allow_inf_nan=False)
    threshold: float = Field(ge=-1e12, le=1e12, allow_inf_nan=False)
    samples: int = Field(ge=1, le=10_000_000)


@router.get("")
async def list_stages(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> StageList:
    views = await services.stage_admin_service.list(principal, request_meta(request))
    return StageList(data=[StageOut.of(v) for v in views])


@router.put("/{name}")
async def set_stage_mode(
    name: str, body: ModeIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> StageOut:
    view = await services.stage_admin_service.set_mode(
        principal, name, body.mode, request_meta(request)
    )
    return StageOut.of(view)


@router.post("/{name}/evals", status_code=201)
async def record_eval(
    name: str, body: EvalBody, request: Request, principal: AuthorizedPrincipal, services: Services
) -> EvalOut:
    run = await services.stage_admin_service.record_eval(
        principal, name, EvalIn(**body.model_dump()), request_meta(request)
    )
    return EvalOut.of(run)
