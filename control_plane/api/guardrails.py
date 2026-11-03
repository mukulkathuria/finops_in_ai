"""Guardrail policy endpoints (US-11, US-12): `GET` and `PUT /v1/admin/guardrails`."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import AuthorizedPrincipal, request_meta
from gateway.domain.guardrails import GuardrailPolicy

router = APIRouter(prefix="/v1/admin/guardrails", tags=["guardrails"])


class PolicyOut(BaseModel):
    pii: str
    secrets: str
    injection: str
    injection_threshold: float
    on_classifier_error: str
    output: str
    hosted_classifier: bool


class PolicyIn(BaseModel):
    """Any subset of the policy; omitted settings keep their current value."""

    model_config = ConfigDict(extra="forbid")
    pii: Literal["redact", "block", "off"] | None = None
    secrets: Literal["block", "redact"] | None = None
    injection: Literal["block", "flag", "off"] | None = None
    injection_threshold: float | None = Field(default=None, ge=0.5, le=0.99)
    on_classifier_error: Literal["closed", "open"] | None = None
    output: Literal["redact", "off"] | None = None
    hosted_classifier: bool | None = None


def _out(policy: GuardrailPolicy) -> PolicyOut:
    return PolicyOut(**policy.to_dict())


@router.get("")
async def get_policy(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> PolicyOut:
    return _out(await services.guardrail_service.get(principal, request_meta(request)))


@router.put("")
async def update_policy(
    body: PolicyIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> PolicyOut:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    policy = await services.guardrail_service.update(principal, changes, request_meta(request))
    return _out(policy)
