"""Org alert webhook (spec F9: at the soft threshold the user and lead are alerted).

The URL and the signing secret are sealed together with envelope encryption; the secret is shown
once, when the endpoint is created or its secret rotated. Only org admins manage the endpoint.
"""

import orjson

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.webhooks import (
    Delivery,
    WebhookEndpoint,
    WebhookRepository,
    webhook_aad,
)
from gateway.adapters.security.envelope import Envelope
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import NotFound
from gateway.domain.policy import Permission, Principal
from gateway.domain.webhooks import new_webhook_secret, url_hint, validate_webhook_url


class WebhookService:
    def __init__(
        self,
        *,
        db: Database,
        repo: WebhookRepository,
        envelope: Envelope,
        audit: AuditRepository,
        authz: Authorizer,
        allow_insecure: bool,
    ) -> None:
        self._db = db
        self._repo = repo
        self._envelope = envelope
        self._audit = audit
        self._authz = authz
        self._allow_insecure = allow_insecure

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    async def _authorize(self, principal: Principal, meta: RequestMeta) -> None:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="webhook", meta=meta)

    async def _store(
        self, principal: Principal, url: str, secret: str, action: str, meta: RequestMeta
    ) -> tuple[WebhookEndpoint, bool]:
        sealed = self._envelope.seal(
            orjson.dumps({"url": url, "secret": secret}).decode(), webhook_aad(principal.org_id)
        )
        async with self._db.transaction(self._ctx(principal)) as conn:
            endpoint, created = await self._repo.upsert(
                conn, hint=url_hint(url), sealed=sealed, actor=principal.user_id
            )
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.SETTINGS_CHANGED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="org",
                    target_id=str(principal.org_id),
                    details={"setting": "alert_webhook", "change": action, "host": url_hint(url)},
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
        return endpoint, created

    async def _current(self, principal: Principal) -> dict[str, str]:
        async with self._db.transaction(self._ctx(principal)) as conn:
            sealed = await self._repo.sealed(conn, principal.org_id)
        if sealed is None:
            raise NotFound("no alert webhook is configured", code="webhook_not_found")
        stored: dict[str, str] = orjson.loads(
            self._envelope.open(sealed, webhook_aad(principal.org_id))
        )
        return stored

    async def set_url(
        self, principal: Principal, url: str, meta: RequestMeta
    ) -> tuple[WebhookEndpoint, str | None]:
        """Create the endpoint (returns its new secret, shown once) or change its URL (keeps the
        secret, returns None)."""
        await self._authorize(principal, meta)
        url = validate_webhook_url(url, allow_insecure=self._allow_insecure)
        try:
            secret = (await self._current(principal))["secret"]
            fresh = None
        except NotFound:
            secret = fresh = new_webhook_secret()
        endpoint, _ = await self._store(principal, url, secret, "set", meta)
        return endpoint, fresh

    async def rotate_secret(self, principal: Principal, meta: RequestMeta) -> str:
        await self._authorize(principal, meta)
        current = await self._current(principal)
        secret = new_webhook_secret()
        await self._store(principal, current["url"], secret, "secret_rotated", meta)
        return secret

    async def get(self, principal: Principal, meta: RequestMeta) -> WebhookEndpoint:
        await self._authorize(principal, meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            endpoint = await self._repo.get(conn, principal.org_id)
        if endpoint is None:
            raise NotFound("no alert webhook is configured", code="webhook_not_found")
        return endpoint

    async def delete(self, principal: Principal, meta: RequestMeta) -> None:
        await self._authorize(principal, meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            if not await self._repo.delete(conn):
                raise NotFound("no alert webhook is configured", code="webhook_not_found")
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.SETTINGS_CHANGED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="org",
                    target_id=str(principal.org_id),
                    details={"setting": "alert_webhook", "change": "deleted"},
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )

    async def deliveries(self, principal: Principal, meta: RequestMeta) -> list[Delivery]:
        await self._authorize(principal, meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._repo.deliveries(conn)
