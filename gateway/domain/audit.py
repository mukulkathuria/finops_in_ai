"""Audit events (US-28). The audit log never stores prompt text or secrets (spec section 5)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

ActorType = Literal["user", "api_key", "system", "anonymous"]
_FORBIDDEN_DETAIL_KEYS: Final = (
    "password",
    "secret",
    "token",
    "prompt",
    "api_key",
    "authorization",
)


class AuditAction:
    SIGNUP = "auth.signup"
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGIN_LOCKED = "auth.login_locked"
    # An action name, not a credential.
    TOKEN_FAMILY_REVOKED = "auth.token_family_revoked"  # noqa: S105  # nosec B105
    LOGOUT = "auth.logout"
    ACCESS_DENIED = "authz.access_denied"
    USER_INVITED = "users.invited"
    INVITATION_ACCEPTED = "users.invitation_accepted"
    INVITATION_REVOKED = "users.invitation_revoked"
    TEAM_CREATED = "teams.created"
    TEAM_MEMBER_ADDED = "teams.member_added"
    TEAM_MEMBER_REMOVED = "teams.member_removed"
    API_KEY_CREATED = "api_keys.created"
    API_KEY_REVOKED = "api_keys.revoked"
    PROVIDER_KEY_ADDED = "provider_keys.added"
    PROVIDER_KEY_ROTATED = "provider_keys.rotated"
    PROVIDER_KEY_DELETED = "provider_keys.deleted"
    BUDGET_SET = "budgets.limit_set"
    BUDGET_DELETED = "budgets.deleted"
    BUDGET_REQUEST_CREATED = "budget_requests.created"
    BUDGET_REQUEST_DECIDED = "budget_requests.decided"
    POLICY_CHANGED = "policies.changed"
    SETTINGS_CHANGED = "settings.changed"
    STAGE_MODE_SET = "stages.mode_set"
    EVAL_RECORDED = "evals.recorded"
    SSO_CONFIGURED = "settings.sso_configured"
    SSO_REMOVED = "settings.sso_removed"
    SSO_LOGIN_SUCCEEDED = "auth.sso_login_succeeded"
    SSO_LOGIN_FAILED = "auth.sso_login_failed"
    SSO_IDENTITY_LINKED = "auth.sso_identity_linked"


def _check_details(details: Mapping[str, Any]) -> None:
    for key, value in details.items():
        lowered = key.lower()
        if any(word in lowered for word in _FORBIDDEN_DETAIL_KEYS):
            raise ValueError(f"audit details must not carry secret or prompt fields: {key!r}")
        _check_value(value)


def _check_value(value: Any) -> None:
    if isinstance(value, Mapping):
        _check_details(value)
    elif isinstance(value, list | tuple):  # a list of mappings must not slip past the check
        for item in value:
            _check_value(item)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    action: str
    actor_type: ActorType
    actor_id: UUID | None = None
    target_type: str | None = None
    target_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    ip: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        _check_details(self.details)


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """Where an action came from: recorded on audit entries (never request bodies)."""

    ip: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChainVerification:
    ok: bool
    checked: int
    first_bad_seq: int | None
