"""Services the control-plane routers need (structural protocol, satisfied by the container)."""

from typing import Annotated, Protocol

from fastapi import Depends, Request

from control_plane.services.identity import IdentityService
from gateway.api.deps import GatewayServices


class ControlPlaneServices(GatewayServices, Protocol):
    @property
    def identity_service(self) -> IdentityService: ...


def get_control_plane(request: Request) -> ControlPlaneServices:
    services: ControlPlaneServices = request.app.state.container
    return services


Services = Annotated[ControlPlaneServices, Depends(get_control_plane)]
