"""`GET /metrics` (US-29): the Prometheus scrape endpoint.

With METRICS_TOKEN set, a scrape needs `Authorization: Bearer <token>`. Without one, metrics are
served in dev and test only; staging and production answer 404 so nothing leaks by default.
"""

import hmac
from typing import Annotated, Protocol

from fastapi import APIRouter, Header, Request, Response

from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.api.deps import bearer_token
from gateway.api.markers import public
from gateway.app.settings import Settings
from gateway.domain.errors import NotFound, Unauthenticated

router = APIRouter(tags=["ops"])


class MetricsServices(Protocol):
    @property
    def settings(self) -> Settings: ...
    @property
    def metrics(self) -> GatewayMetrics: ...


def ensure_scrape_allowed(settings: Settings, authorization: str | None) -> None:
    token = settings.metrics_token.get_secret_value() if settings.metrics_token else ""
    if token:
        presented = bearer_token(authorization)
        if not hmac.compare_digest(presented.encode(), token.encode()):
            raise Unauthenticated("invalid metrics token", code="invalid_metrics_token")
    elif settings.is_production_like:
        raise NotFound("not found")


@router.get("/metrics", include_in_schema=False)
@public
async def metrics(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Response:
    services: MetricsServices = request.app.state.container
    ensure_scrape_allowed(services.settings, authorization)
    body, content_type = services.metrics.render()
    return Response(body, media_type=content_type)
