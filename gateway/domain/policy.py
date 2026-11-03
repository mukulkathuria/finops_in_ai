"""Role-based authorization, deny by default (spec section 6 and 10).

Roles and their permissions are data (tables `roles`, `role_permissions`); this module only
evaluates a principal's grants against that catalog. Team-scoped grants apply to one team only.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    ORG_ADMIN = "org_admin"
    FINANCE = "finance"
    AUDITOR = "auditor"
    MEMBER = "member"
    TEAM_LEAD = "team_lead"


class Permission(StrEnum):
    ORG_MANAGE = "org:manage"
    USERS_MANAGE = "users:manage"
    TEAMS_MANAGE = "teams:manage"
    TEAM_MEMBERS_MANAGE = "team_members:manage"
    PROVIDERS_MANAGE = "providers:manage"
    BUDGETS_MANAGE_ORG = "budgets:manage_org"
    BUDGETS_MANAGE_TEAM = "budgets:manage_team"
    BUDGETS_MANAGE_MEMBER = "budgets:manage_member"
    BUDGET_REQUESTS_DECIDE = "budget_requests:decide"
    BUDGET_REQUESTS_CREATE = "budget_requests:create"
    API_KEYS_MANAGE = "api_keys:manage"
    REPORTS_READ = "reports:read"
    REPORTS_READ_TEAM = "reports:read_team"
    PRICES_READ = "prices:read"
    USAGE_READ_SELF = "usage:read_self"
    AUDIT_READ = "audit:read"
    CONFIG_READ = "config:read"
    MODELS_USE = "models:use"


RoleCatalog = Mapping[str, frozenset[str]]  # role name -> permission names


@dataclass(frozen=True, slots=True)
class RoleGrant:
    role: str
    team_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    org_id: UUID
    email: str
    grants: tuple[RoleGrant, ...]
    catalog: RoleCatalog = field(repr=False)
    team_ids: frozenset[UUID] = frozenset()

    def can(self, permission: Permission, team_id: UUID | None = None) -> bool:
        """Org-scoped grants apply everywhere in the org; team grants only to their own team."""
        for grant in self.grants:
            if permission in self.catalog.get(grant.role, frozenset()) and (
                grant.team_id is None or grant.team_id == team_id
            ):
                return True
        return False

    def can_somewhere(self, permission: Permission) -> bool:
        return any(permission in self.catalog.get(g.role, frozenset()) for g in self.grants)

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(g.role for g in self.grants)

    def led_team_ids(self) -> frozenset[UUID]:
        return frozenset(g.team_id for g in self.grants if g.role == Role.TEAM_LEAD and g.team_id)
