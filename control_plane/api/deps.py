"""Services the control-plane routers need (structural protocol, satisfied by the container)."""

from typing import Annotated, Protocol

from fastapi import Depends, Request

from control_plane.services.api_keys import ApiKeyService
from control_plane.services.budgets import BudgetService
from control_plane.services.cache import CacheSettingsService
from control_plane.services.guardrails import GuardrailService
from control_plane.services.identity import IdentityService
from control_plane.services.invitations import InvitationService
from control_plane.services.providers import ProviderKeyService
from control_plane.services.relevance import RelevanceService
from control_plane.services.routing import RoutingService
from control_plane.services.savings import SavingsService
from control_plane.services.sessions import SessionService
from control_plane.services.stages import StageAdminService
from control_plane.services.teams import TeamService
from control_plane.services.webhooks import WebhookService
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
    @property
    def budget_service(self) -> BudgetService: ...
    @property
    def webhook_service(self) -> WebhookService: ...
    @property
    def guardrail_service(self) -> GuardrailService: ...
    @property
    def stage_admin_service(self) -> StageAdminService: ...
    @property
    def savings_service(self) -> SavingsService: ...
    @property
    def cache_settings_service(self) -> CacheSettingsService: ...
    @property
    def routing_service(self) -> RoutingService: ...
    @property
    def relevance_service(self) -> RelevanceService: ...


def get_control_plane(request: Request) -> ControlPlaneServices:
    services: ControlPlaneServices = request.app.state.container
    return services


Services = Annotated[ControlPlaneServices, Depends(get_control_plane)]
