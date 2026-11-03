"""Team API keys (US-04): shown once, stored only as a hash, scoped to the team, revocable."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.api_keys import ApiKeyRecord, ApiKeyRepository
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.teams import TeamRepository
from gateway.domain.api_keys import new_api_key
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Gone, InvalidInput, NotFound
from gateway.domain.policy import Permission, Principal
from gateway.domain.secrets import digest


@dataclass(frozen=True, slots=True)
class CreatedKey:
    record: ApiKeyRecord
    key: str


class ApiKeyService:
    def __init__(
        self,
        *,
        db: Database,
        keys: ApiKeyRepository,
        teams: TeamRepository,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._keys = keys
        self._teams = teams
        self._audit = audit
        self._authz = authz

    async def _require_team(self, principal: Principal, team_id: UUID, meta: RequestMeta) -> None:
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            if not await self._teams.team_exists(conn, team_id):
                raise NotFound("team not found", code="team_not_found")
        await self._authz.ensure(
            principal,
            Permission.API_KEYS_MANAGE,
            team_id=team_id,
            target=f"team:{team_id}",
            meta=meta,
        )

    async def create(
        self,
        principal: Principal,
        *,
        team_id: UUID,
        name: str,
        user_id: UUID | None,
        expires_in_days: int | None,
        meta: RequestMeta,
    ) -> CreatedKey:
        name = " ".join(name.split())
        if not 1 <= len(name) <= 100:
            raise InvalidInput(
                "key name must be 1-100 characters", code="invalid_key_name", param="name"
            )
        await self._require_team(principal, team_id, meta)
        ctx = TenantContext(principal.org_id, principal.user_id)
        key, prefix = new_api_key()
        expires = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
        async with self._db.transaction(ctx) as conn:
            if user_id is not None and not await self._keys.bound_member_is_active(
                conn, team_id=team_id, user_id=user_id
            ):
                raise InvalidInput(
                    "a key can only be bound to an active member of the team",
                    code="invalid_key_owner",
                    param="user_id",
                )
            record = await self._keys.create(
                conn,
                org_id=principal.org_id,
                team_id=team_id,
                user_id=user_id,
                name=name,
                prefix=prefix,
                key_hash=digest(key),
                created_by=principal.user_id,
                expires_at=expires,
            )
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.API_KEY_CREATED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="api_key",
                    target_id=str(record.id),
                    details={"team_id": str(team_id), "prefix": prefix, "name": name},
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
        return CreatedKey(record=record, key=key)

    async def list(
        self, principal: Principal, team_id: UUID, meta: RequestMeta
    ) -> list[ApiKeyRecord]:
        await self._require_team(principal, team_id, meta)
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            return await self._keys.list_for_team(conn, team_id)

    async def revoke(
        self, principal: Principal, team_id: UUID, key_id: UUID, meta: RequestMeta
    ) -> None:
        await self._require_team(principal, team_id, meta)
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            outcome = await self._keys.revoke(
                conn, team_id=team_id, key_id=key_id, by=principal.user_id
            )
            if outcome == "not_found":
                raise NotFound("API key not found", code="api_key_not_found")
            if outcome == "already_revoked":
                raise Gone("API key was already revoked", code="api_key_revoked")
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.API_KEY_REVOKED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="api_key",
                    target_id=str(key_id),
                    details={"team_id": str(team_id)},
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
