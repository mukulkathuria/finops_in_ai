"""Sessions (US-03): login with lockout, rotating refresh tokens, family revocation, logout."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext, set_lookup, set_tenant
from gateway.adapters.postgres.identity import IdentityRepository
from gateway.adapters.postgres.sessions import SessionRepository
from gateway.adapters.postgres.sso import SsoRepository
from gateway.adapters.security.passwords import Argon2PasswordHasher
from gateway.adapters.security.tokens import TokenService
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Forbidden, InvalidInput, Unauthenticated
from gateway.domain.identity import normalize_email
from gateway.domain.policy import Permission, Principal
from gateway.domain.secrets import digest, new_secret

REFRESH_PREFIX = "frt_"  # recognisable for log redaction and secret scanners
_BAD_LOGIN = "email or password is incorrect"


@dataclass(frozen=True, slots=True)
class Session:
    org_id: UUID
    org_name: str
    org_slug: str
    user_id: UUID
    email: str
    roles: tuple[str, ...]
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class SessionService:
    def __init__(
        self,
        *,
        db: Database,
        sessions: SessionRepository,
        identity: IdentityRepository,
        audit: AuditRepository,
        hasher: Argon2PasswordHasher,
        tokens: TokenService,
        refresh_ttl: timedelta,
        family_ttl: timedelta,
        max_failed_logins: int,
        sso: SsoRepository,
    ) -> None:
        self._db = db
        self._sessions = sessions
        self._identity = identity
        self._audit = audit
        self._hasher = hasher
        self._tokens = tokens
        self._refresh_ttl = refresh_ttl
        self._family_ttl = family_ttl
        self._max_failed = max_failed_logins
        self._sso = sso

    async def issue(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        family_id: UUID | None = None,
        family_expires_at: datetime | None = None,
    ) -> Session:
        """Inside the caller's tenant transaction: mint a refresh token (new family unless rotating)
        and an access token for an active user."""
        user = await self._identity.load_user(conn, user_id)
        if user is None or user.status != "active":
            raise Unauthenticated("account is not active", code="account_inactive")
        org_name, org_slug = await self._identity.get_org(conn)
        now = datetime.now(UTC)
        family_expires = family_expires_at or now + self._family_ttl
        expires = min(now + self._refresh_ttl, family_expires)
        secret = REFRESH_PREFIX + new_secret()
        await self._sessions.create_refresh(
            conn,
            org_id=user.org_id,
            user_id=user.id,
            family_id=family_id or uuid7(),
            token_hash=digest(secret),
            expires_at=expires,
            family_expires_at=family_expires,
        )
        return Session(
            org_id=user.org_id,
            org_name=org_name,
            org_slug=org_slug,
            user_id=user.id,
            email=user.email,
            roles=tuple(sorted({g.role for g in user.grants})),
            access_token=self._tokens.issue_access(user.id, user.org_id),
            expires_in=self._tokens.access_ttl_seconds,
            refresh_token=secret,
            refresh_expires_in=int((expires - now).total_seconds()),
        )

    async def login(self, *, email: str, password: str, meta: RequestMeta) -> Session:
        try:
            normalized = normalize_email(email)
        except InvalidInput:
            normalized = ""  # a malformed email is just a failed login, same answer as any other
        user = None
        if normalized:
            async with self._db.transaction() as conn:
                await set_lookup(conn, "email", normalized)
                user = await self._sessions.find_login(conn, normalized)
        now = datetime.now(UTC)
        # A locked account answers exactly like a wrong password (same hashing work, same error),
        # so the lock reveals neither that the account exists nor that the password was right.
        locked = user is not None and user.locked_until is not None and user.locked_until > now
        valid = await self._hasher.verify(
            user.password_hash if user is not None and not locked else None, password
        )
        if user is None or locked:
            raise Unauthenticated(_BAD_LOGIN, code="invalid_credentials")
        ctx = TenantContext(user.org_id, user.id)
        if not valid:
            async with self._db.transaction(ctx) as conn:
                failures, _ = await self._sessions.record_failure(
                    conn, user.id, threshold=self._max_failed
                )
                details = {"failures": failures}
                await self._audit.append(
                    conn, self._event(AuditAction.LOGIN_FAILED, user.id, meta, details)
                )
                if failures >= self._max_failed:
                    await self._audit.append(
                        conn, self._event(AuditAction.LOGIN_LOCKED, user.id, meta, details)
                    )
            raise Unauthenticated(_BAD_LOGIN, code="invalid_credentials")
        if user.status != "active":
            raise Unauthenticated("account is not active", code="account_inactive")
        rehash = None
        if user.password_hash and self._hasher.needs_rehash(user.password_hash):
            rehash = await self._hasher.hash(password)
        session = None
        async with self._db.transaction(ctx) as conn:
            if await self._sso_required(conn, user.id):
                refusal = {"reason": "sso_required"}
                await self._audit.append(
                    conn, self._event(AuditAction.LOGIN_FAILED, user.id, meta, refusal)
                )
            else:
                await self._sessions.record_success(conn, user.id, rehash)
                session = await self.issue(conn, user_id=user.id)
                await self._audit.append(
                    conn, self._event(AuditAction.LOGIN_SUCCEEDED, user.id, meta, {})
                )
        if session is None:  # the password was right: saying so reveals nothing new
            raise Forbidden(
                "this organisation signs in through its single sign-on provider",
                code="sso_required",
            )
        return session

    async def _sso_required(self, conn: AsyncConnection, user_id: UUID) -> bool:
        """With local login off for the org (US-27), only its admins may still use a
        password, so a provider that breaks cannot lock everyone out."""
        if not await self._sso.local_login_disabled(conn):
            return False
        user = await self._identity.load_user(conn, user_id)
        if user is None:
            return True
        catalog = await self._identity.load_role_catalog(conn)
        member = Principal(user.id, user.org_id, user.email, user.grants, catalog)
        return not member.can(Permission.ORG_MANAGE)

    async def refresh(self, *, refresh_token: str, meta: RequestMeta) -> Session:
        token_hash = digest(refresh_token)
        async with self._db.transaction() as conn:
            await set_lookup(conn, "token_hash", token_hash)
            record = await self._sessions.find_refresh(conn, token_hash)
            if record is None:
                raise Unauthenticated("refresh token is not valid", code="invalid_refresh_token")
            await set_tenant(conn, TenantContext(record.org_id, record.user_id))
            if record.revoked_at is not None:
                raise Unauthenticated("session was revoked", code="refresh_token_revoked")
            if record.rotated_at is None and await self._sessions.mark_rotated(conn, record.id):
                now = datetime.now(UTC)
                if record.expires_at <= now or record.family_expires_at <= now:
                    raise Unauthenticated("refresh token has expired", code="refresh_token_expired")
                return await self.issue(
                    conn,
                    user_id=record.user_id,
                    family_id=record.family_id,
                    family_expires_at=record.family_expires_at,
                )
            # Reuse of a rotated token, or a lost race on rotation: revoke the whole family.
            revoked = await self._sessions.revoke_family(conn, record.family_id, "reuse")
            await self._audit.append(
                conn,
                self._event(
                    AuditAction.TOKEN_FAMILY_REVOKED,
                    record.user_id,
                    meta,
                    {"family_id": str(record.family_id), "reason": "reuse", "revoked": revoked},
                ),
            )
        # Raised after the revocation has committed.
        raise Unauthenticated(
            "refresh token was already used; every session in this family is revoked",
            code="refresh_token_reused",
        )

    async def logout(self, *, refresh_token: str, meta: RequestMeta) -> None:
        """Revoke the whole family. Unknown tokens succeed silently: logout is idempotent."""
        token_hash = digest(refresh_token)
        async with self._db.transaction() as conn:
            await set_lookup(conn, "token_hash", token_hash)
            record = await self._sessions.find_refresh(conn, token_hash)
            if record is None:
                return
            await set_tenant(conn, TenantContext(record.org_id, record.user_id))
            if await self._sessions.revoke_family(conn, record.family_id, "logout"):
                await self._audit.append(
                    conn, self._event(AuditAction.LOGOUT, record.user_id, meta, {})
                )

    @staticmethod
    def _event(
        action: str, user_id: UUID, meta: RequestMeta, details: Mapping[str, object]
    ) -> AuditEvent:
        return AuditEvent(
            action=action,
            actor_type="user",
            actor_id=user_id,
            target_type="user",
            target_id=str(user_id),
            details=details,
            ip=meta.ip,
            request_id=meta.request_id,
        )
