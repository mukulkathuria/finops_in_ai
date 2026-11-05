"""Single sign-on (US-27): an org's OpenID Connect provider signs its members in.

1. `start`: the org is found by its slug. The sign-in's state, nonce and PKCE verifier are kept
   for ten minutes, hashed, and the browser goes to the provider.
2. `callback`: the state is taken (once) and the provider's code exchanged with the verifier.
   The ID token's signature and claims are checked. `(issuer, subject)` maps to a user through
   `identities`. At a first sign-in, an email the provider verified links the org's user with
   that email, in an allowed domain. No account is ever created here. The browser returns to
   the web app with a one-time code; failures return with a reason code, never the provider's
   own text.
3. `exchange`: the web app trades the code (once, within a minute) for the usual session, so no
   token travels in a URL.

Admins configure the provider. Its client secret is sealed with the org envelope and never
shown. With local login off, members sign in through the provider only, and admins keep local
login, so a broken provider cannot lock the org out (SessionService)."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlencode
from uuid import UUID

from control_plane.services.authz import Authorizer
from control_plane.services.sessions import Session, SessionService
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext, set_lookup, set_tenant
from gateway.adapters.postgres.identity import IdentityRepository
from gateway.adapters.postgres.sso import PendingLogin, SsoConfig, SsoRepository, sso_aad
from gateway.adapters.security.envelope import DecryptionError, Envelope
from gateway.adapters.sso.oidc import OidcClient, OidcError
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import InvalidInput, NotFound, Unauthenticated, Unavailable
from gateway.domain.policy import Permission, Principal
from gateway.domain.secrets import digest, new_secret
from gateway.domain.sso import (
    CODE_TTL_S,
    STATE_TTL_S,
    SignIn,
    authorization_url,
    clean_domains,
    domain_allowed,
    issuer_url,
    new_pkce,
)

log = logging.getLogger(__name__)
_NOT_SET_UP: Final = "single sign-on is not set up for this organisation"


@dataclass(frozen=True, slots=True)
class SsoChange:
    issuer: str
    client_id: str
    client_secret: str | None  # None keeps the stored one; "" makes the client public
    allowed_domains: tuple[str, ...]
    local_login_disabled: bool


@dataclass(frozen=True, slots=True)
class SsoSettings:
    issuer: str
    client_id: str
    confidential_client: bool
    allowed_domains: tuple[str, ...]
    local_login_disabled: bool
    callback_url: str
    updated_at: datetime


def _bound(secret: str, browser: str) -> str:
    """How a single-use sign-in secret is stored: hashed with the browser's cookie secret."""
    return digest(f"{secret}.{browser}")


class _Refused(Exception):
    """A sign-in that ends at the web app with a reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class SsoService:
    def __init__(
        self,
        *,
        db: Database,
        repo: SsoRepository,
        identity: IdentityRepository,
        sessions: SessionService,
        oidc: OidcClient,
        envelope: Envelope,
        audit: AuditRepository,
        authz: Authorizer,
        callback_url: str | None,  # None: PUBLIC_BASE_URL is unset and single sign-on is off
        web_base_url: str,
        allow_insecure: bool,
    ) -> None:
        self._db = db
        self._repo = repo
        self._identity = identity
        self._sessions = sessions
        self._oidc = oidc
        self._envelope = envelope
        self._audit = audit
        self._authz = authz
        self._callback_url = callback_url
        self._web = web_base_url.rstrip("/") + "/login/sso"
        self._insecure = allow_insecure

    def _callback(self) -> str:
        if self._callback_url is None:
            raise Unavailable(
                "single sign-on is off on this gateway: its operator has not set PUBLIC_BASE_URL",
                code="sso_off",
            )
        return self._callback_url

    # ---------------------------------------------------------------- configuration (admins)

    def _settings(self, config: SsoConfig) -> SsoSettings:
        return SsoSettings(
            config.issuer,
            config.client_id,
            config.secret is not None,
            config.allowed_domains,
            config.local_login_disabled,
            self._callback_url or "",
            config.updated_at,
        )

    async def settings(self, principal: Principal, meta: RequestMeta) -> SsoSettings:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="sso", meta=meta)
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            config = await self._repo.config(conn)
        if config is None:
            raise NotFound(_NOT_SET_UP, code="sso_not_configured")
        return self._settings(config)

    async def configure(
        self, principal: Principal, change: SsoChange, meta: RequestMeta
    ) -> SsoSettings:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="sso", meta=meta)
        self._callback()
        issuer = issuer_url(change.issuer, allow_insecure=self._insecure)
        domains = clean_domains(change.allowed_domains)
        client_id = change.client_id.strip()
        if not client_id:
            raise InvalidInput("client_id is required", param="client_id")
        try:  # a provider that cannot be read is refused now, not at the first sign-in
            await self._oidc.provider(issuer)
        except OidcError as exc:
            raise InvalidInput(
                f"the provider's discovery document cannot be read: {exc}",
                code="sso_discovery_failed",
                param="issuer",
            ) from exc
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            current = await self._repo.config(conn)
            if change.client_secret is None:
                sealed = current.secret if current is not None else None
            elif change.client_secret:
                sealed = self._envelope.seal(change.client_secret, sso_aad(principal.org_id))
            else:
                sealed = None
            saved = await self._repo.save_config(
                conn,
                issuer=issuer,
                client_id=client_id,
                secret=sealed,
                domains=domains,
                local_login_disabled=change.local_login_disabled,
                by=principal.user_id,
            )
            details = {
                "issuer": issuer,
                "client_id": client_id,
                "confidential_client": sealed is not None,
                "allowed_domains": domains,
                "local_login_disabled": change.local_login_disabled,
            }
            await self._audit.append(
                conn, _event(AuditAction.SSO_CONFIGURED, principal.user_id, meta, details)
            )
        return self._settings(saved)

    async def remove(self, principal: Principal, meta: RequestMeta) -> None:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="sso", meta=meta)
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            removed = await self._repo.delete_config(conn)
            if removed:
                await self._audit.append(
                    conn, _event(AuditAction.SSO_REMOVED, principal.user_id, meta, {})
                )
        if not removed:
            raise NotFound(_NOT_SET_UP, code="sso_not_configured")

    # ---------------------------------------------------------------- the browser flow

    @property
    def secure_cookies(self) -> bool:
        """The sign-in cookie must be https-only when the gateway is reached over https."""
        return (self._callback_url or "").startswith("https://")

    async def start(self, org_slug: str) -> tuple[str, str]:
        """The provider's authorization URL for a new sign-in to the org, and the secret the
        browser keeps (a cookie) for the callback and the code exchange: a sign-in someone else
        started, or a one-time code they obtained, then does not work in a victim's browser."""
        callback = self._callback()
        async with self._db.transaction() as conn:
            await set_lookup(conn, "org_slug", org_slug.strip().lower())
            org_id = await self._repo.org_by_slug(conn)
            config = None
            if org_id is not None:
                await set_tenant(conn, TenantContext(org_id))
                config = await self._repo.config(conn)
                await self._repo.purge(conn)  # finished and abandoned sign-ins
        if org_id is None or config is None:
            raise NotFound(_NOT_SET_UP, code="sso_not_configured")
        try:
            provider = await self._oidc.provider(config.issuer)
        except OidcError as exc:
            log.warning("single sign-on provider unreachable", extra={"org_id": str(org_id)})
            raise Unavailable(
                "the organisation's sign-in provider cannot be reached", code="sso_unavailable"
            ) from exc
        state, nonce, pkce, browser = new_secret(), new_secret(), new_pkce(), new_secret()
        async with self._db.transaction(TenantContext(org_id)) as conn:
            await self._repo.save_login(
                conn,
                secret_hash=_bound(state, browser),
                kind="state",
                ttl_s=STATE_TTL_S,
                nonce=nonce,
                verifier=pkce.verifier,
            )
        url = authorization_url(
            provider.authorization_endpoint,
            client_id=config.client_id,
            redirect_uri=callback,
            state=state,
            nonce=nonce,
            challenge=pkce.challenge,
        )
        return url, browser

    async def callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
        browser: str,
        meta: RequestMeta,
    ) -> str:
        """Where the browser goes back to in the web app: with a one-time code, or a reason."""
        pending = await self._take(state, "state", browser) if state else None
        if pending is None or pending.expired:
            return self._back(error="sso_expired")
        org_id = pending.org_id
        try:
            if error or not code:
                raise _Refused("sso_cancelled", f"the provider returned {error or 'no code'}")
            user_id, issuer = await self._signed_in(org_id, code, pending)
        except (_Refused, OidcError, Unauthenticated, DecryptionError) as exc:
            reason = exc.code if isinstance(exc, _Refused) else "sso_failed"
            await self._failed(org_id, reason, str(exc), meta)
            return self._back(error=reason)
        one_time = new_secret()
        async with self._db.transaction(TenantContext(org_id, user_id)) as conn:
            await self._repo.save_login(
                conn,
                secret_hash=_bound(one_time, browser),
                kind="code",
                ttl_s=CODE_TTL_S,
                user_id=user_id,
            )
            await self._audit.append(
                conn, _event(AuditAction.SSO_LOGIN_SUCCEEDED, user_id, meta, {"issuer": issuer})
            )
        return self._back(code=one_time)

    async def exchange(self, code: str, browser: str, meta: RequestMeta) -> Session:
        """The session for a one-time code from `callback`, in the browser that signed in."""
        issued = await self._take(code, "code", browser)
        if issued is None or issued.expired or issued.user_id is None:
            raise Unauthenticated(
                "the sign-in code is invalid, used or expired", code="invalid_sso_code"
            )
        async with self._db.transaction(TenantContext(issued.org_id, issued.user_id)) as conn:
            session = await self._sessions.issue(conn, user_id=issued.user_id)
            await self._audit.append(
                conn,
                _event(AuditAction.LOGIN_SUCCEEDED, issued.user_id, meta, {"method": "sso"}),
            )
        return session

    async def _signed_in(self, org_id: UUID, code: str, pending: PendingLogin) -> tuple[UUID, str]:
        async with self._db.transaction(TenantContext(org_id)) as conn:
            config = await self._repo.config(conn)
        if config is None or pending.nonce is None or pending.code_verifier is None:
            raise _Refused("sso_failed", "single sign-on was turned off during the sign-in")
        secret = self._envelope.open(config.secret, sso_aad(org_id)) if config.secret else None
        provider = await self._oidc.provider(config.issuer)
        id_token = await self._oidc.exchange(
            provider,
            code=code,
            redirect_uri=self._callback(),
            client_id=config.client_id,
            client_secret=secret,
            verifier=pending.code_verifier,
        )
        sign_in = await self._oidc.verify(
            provider, id_token, client_id=config.client_id, nonce=pending.nonce
        )
        return await self._member(org_id, config, sign_in), config.issuer

    async def _member(self, org_id: UUID, config: SsoConfig, sign_in: SignIn) -> UUID:
        """The org's user this identity is: mapped already, or linked now by a verified email."""
        async with self._db.transaction(TenantContext(org_id)) as conn:
            user_id = await self._repo.identity(
                conn, provider=config.issuer, subject=sign_in.subject
            )
            if user_id is not None:
                await self._repo.touch_identity(
                    conn, provider=config.issuer, subject=sign_in.subject, email=sign_in.email
                )
            elif (
                sign_in.email is not None
                and sign_in.email_verified
                and domain_allowed(sign_in.email, config.allowed_domains)
            ):
                found = await self._repo.user_by_email(conn, sign_in.email)
                if found is None:
                    raise _Refused("sso_no_account", "no member of the org has this email")
                linked = await self._repo.link(
                    conn,
                    user_id=found,
                    provider=config.issuer,
                    subject=sign_in.subject,
                    email=sign_in.email,
                )
                user_id = linked or await self._repo.identity(
                    conn, provider=config.issuer, subject=sign_in.subject
                )
                if user_id is not None:
                    await self._audit.append(
                        conn,
                        _event(
                            AuditAction.SSO_IDENTITY_LINKED,
                            user_id,
                            None,
                            {"issuer": config.issuer},
                        ),
                    )
            if user_id is None:
                raise _Refused("sso_no_account", "the identity maps to no member")
            user = await self._identity.load_user(conn, user_id)
        if user is None or user.status != "active":
            raise _Refused("sso_account_inactive", "the member's account is not active")
        return user_id

    async def _take(self, secret: str, kind: str, browser: str) -> PendingLogin | None:
        """Find a single-use secret by its hash and delete it, in one transaction. Only the
        browser it was issued to finds it."""
        secret_hash = _bound(secret, browser)
        async with self._db.transaction() as conn:
            await set_lookup(conn, "sso_secret", secret_hash)
            found = await self._repo.find_login(conn)
            if found is None or found.kind != kind:
                return None
            await set_tenant(conn, TenantContext(found.org_id))
            if not await self._repo.take_login(conn, secret_hash):
                return None  # another request took it first
        return found

    async def _failed(self, org_id: UUID, reason: str, detail: str, meta: RequestMeta) -> None:
        details: dict[str, object] = {"reason": reason, "detail": detail[:200]}
        async with self._db.transaction(TenantContext(org_id)) as conn:
            await self._audit.append(
                conn, _event(AuditAction.SSO_LOGIN_FAILED, None, meta, details)
            )

    def _back(self, **params: str) -> str:
        return f"{self._web}?{urlencode(params)}"


def _event(
    action: str, user_id: UUID | None, meta: RequestMeta | None, details: dict[str, object]
) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_type="user" if user_id is not None else "system",
        actor_id=user_id,
        target_type="sso",
        target_id=None,
        details=details,
        ip=meta.ip if meta is not None else None,
        request_id=meta.request_id if meta is not None else None,
    )
