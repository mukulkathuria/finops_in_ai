"""Signup (US-01): create an org and its first admin, then open a session."""

from typing import Literal
from uuid import uuid7

from sqlalchemy.exc import IntegrityError

from control_plane.services.sessions import Session, SessionService
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.identity import IdentityRepository
from gateway.adapters.security.passwords import Argon2PasswordHasher
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Conflict, Forbidden, InvalidInput
from gateway.domain.identity import normalize_email, password_problems, slugify, validate_slug
from gateway.domain.policy import Role, RoleGrant

SignupMode = Literal["bootstrap", "open", "disabled"]
_CONSTRAINT_CONFLICTS = {
    "users_email_key": ("an account with this email already exists", "email_taken", "email"),
    "orgs_slug_key": ("this organization slug is taken", "slug_taken", "org_slug"),
}


def raise_for_constraint(exc: IntegrityError) -> None:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint in _CONSTRAINT_CONFLICTS:
        message, code, param = _CONSTRAINT_CONFLICTS[constraint]
        raise Conflict(message, code=code, param=param) from exc


def require_strong_password(password: str, *, email: str, context: tuple[str, ...] = ()) -> None:
    problems = password_problems(password, email=email, context=context)
    if problems:
        raise InvalidInput(
            "password does not meet the policy: " + "; ".join(problems),
            code="weak_password",
            param="password",
            details={"problems": problems},
        )


class IdentityService:
    def __init__(
        self,
        *,
        db: Database,
        repo: IdentityRepository,
        audit: AuditRepository,
        hasher: Argon2PasswordHasher,
        sessions: SessionService,
        signup_mode: SignupMode,
    ) -> None:
        self._db = db
        self._repo = repo
        self._audit = audit
        self._hasher = hasher
        self._sessions = sessions
        self._signup_mode = signup_mode

    async def signup(
        self,
        *,
        org_name: str,
        org_slug: str | None,
        email: str,
        password: str,
        display_name: str,
        meta: RequestMeta | None = None,
    ) -> Session:
        meta = meta or RequestMeta()
        if self._signup_mode == "disabled":
            raise Forbidden("self-service signup is disabled", code="signup_disabled")
        org_name = org_name.strip()
        if not org_name:
            raise InvalidInput(
                "organization name is required", code="invalid_org_name", param="org_name"
            )
        email = normalize_email(email)
        slug = validate_slug(org_slug.strip().lower() if org_slug else slugify(org_name))
        require_strong_password(password, email=email, context=(org_name, slug))
        password_hash = await self._hasher.hash(password)
        org_id, user_id = uuid7(), uuid7()
        grants = (RoleGrant(Role.ORG_ADMIN), RoleGrant(Role.MEMBER))
        try:
            async with self._db.transaction(TenantContext(org_id, user_id)) as conn:
                if self._signup_mode == "bootstrap" and not await self._repo.claim_bootstrap(conn):
                    raise Forbidden(
                        "bootstrap signup is closed; ask an admin for an invitation",
                        code="signup_closed",
                    )
                await self._repo.insert_org(conn, org_id=org_id, name=org_name, slug=slug)
                await self._repo.insert_user(
                    conn,
                    user_id=user_id,
                    org_id=org_id,
                    email=email,
                    display_name=display_name.strip(),
                    password_hash=password_hash,
                )
                await self._repo.grant_roles(conn, org_id=org_id, user_id=user_id, grants=grants)
                await self._audit.append(
                    conn,
                    AuditEvent(
                        action=AuditAction.SIGNUP,
                        actor_type="user",
                        actor_id=user_id,
                        target_type="org",
                        target_id=str(org_id),
                        details={"org_slug": slug, "roles": [g.role for g in grants]},
                        ip=meta.ip,
                        request_id=meta.request_id,
                    ),
                )
                session = await self._sessions.issue(conn, user_id=user_id)
        except IntegrityError as exc:
            raise_for_constraint(exc)
            raise
        return session
