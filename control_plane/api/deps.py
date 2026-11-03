"""Services the control-plane routers need (structural protocol, satisfied by the container)."""

from typing import Annotated, Protocol

from fastapi import Depends, Request

from control_plane.services.api_keys import ApiKeyService
from control_plane.services.identity import IdentityService
from control_plane.services.invitations import InvitationService
from control_plane.services.providers import ProviderKeyService
from control_plane.services.sessions import SessionService
from control_plane.services.teams import TeamService
from gateway.api.deps import GatewayServices


class ControlPlaneServices(GatewayServices, Protocol):
    @property
    def identity_service(self) -> IdentityService: ...
    @property
    def team_service(self) -> TeamService: ...
    @property
    def invitation_service(self) -> InvitationService: ...
    @property
    def session_service(self) -> SessionService: ...
    @property
    def api_key_service(self) -> ApiKeyService: ...
    @property
    def provider_key_service(self) -> ProviderKeyService: ...


def get_control_plane(request: Request) -> ControlPlaneServices:
    services: ControlPlaneServices = request.app.state.container
    return services


Services = Annotated[ControlPlaneServices, Depends(get_control_plane)]
