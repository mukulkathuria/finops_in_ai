"""Org provider keys (US-06): stored encrypted, never returned, every change audited."""

from uuid import uuid7

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.credentials import (
    CredentialInfo,
    CredentialRepository,
    credential_aad,
)
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.security.envelope import Envelope
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import InvalidInput, NotFound
from gateway.domain.policy import Permission, Principal


class ProviderKeyService:
    def __init__(
        self,
        *,
        db: Database,
        repo: CredentialRepository,
        envelope: Envelope,
        audit: AuditRepository,
        authz: Authorizer,
        known_providers: frozenset[str],
    ) -> None:
        self._db = db
        self._repo = repo
        self._envelope = envelope
        self._audit = audit
        self._authz = authz
        self._known = known_providers

    async def add(
        self, principal: Principal, provider: str, api_key: str, label: str, meta: RequestMeta
    ) -> CredentialInfo:
        await self._authz.ensure(
            principal, Permission.PROVIDERS_MANAGE, target=f"provider:{provider}", meta=meta
        )
        if provider not in self._known:
            raise NotFound(
                f"provider '{provider}' is not configured on this gateway",
                code="provider_not_found",
            )
        api_key = api_key.strip()
        if not 8 <= len(api_key) <= 512 or any(c.isspace() for c in api_key):
            raise InvalidInput(
                "api_key must be 8-512 characters without whitespace", param="api_key"
            )
        credential_id = uuid7()
        sealed = self._envelope.seal(
            api_key, credential_aad(principal.org_id, provider, credential_id)
        )
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            rotated = await self._repo.revoke_active(conn, provider)
            info = await self._repo.insert(
                conn,
                credential_id=credential_id,
                org_id=principal.org_id,
                provider=provider,
                label=label.strip()[:100],
                sealed=sealed,
                key_hint=api_key[-4:],
                created_by=principal.user_id,
            )
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.PROVIDER_KEY_ROTATED
                    if rotated
                    else AuditAction.PROVIDER_KEY_ADDED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="provider_credential",
                    target_id=str(info.id),
                    details={"provider": provider, "key_hint": info.key_hint},
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
        return info

    async def list(self, principal: Principal, meta: RequestMeta) -> list[CredentialInfo]:
        await self._authz.ensure(
            principal, Permission.PROVIDERS_MANAGE, target="providers", meta=meta
        )
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            return await self._repo.list_active(conn)

    async def delete(self, principal: Principal, provider: str, meta: RequestMeta) -> None:
        await self._authz.ensure(
            principal, Permission.PROVIDERS_MANAGE, target=f"provider:{provider}", meta=meta
        )
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            if not await self._repo.revoke_active(conn, provider):
                raise NotFound("no active key for this provider", code="credential_not_found")
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.PROVIDER_KEY_DELETED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="provider",
                    target_id=provider,
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
