"""Guardrail policy administration (US-11, US-12). Org admins change it; auditors may read it.
Every change is audited with the old and new policy."""

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.policies import PolicyCache, PolicyRepository
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.guardrails import GuardrailPolicy
from gateway.domain.policy import Permission, Principal


class GuardrailService:
    def __init__(
        self,
        *,
        db: Database,
        repo: PolicyRepository,
        cache: PolicyCache,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._repo = repo
        self._cache = cache
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    async def get(self, principal: Principal, meta: RequestMeta) -> GuardrailPolicy:
        if not (principal.can(Permission.ORG_MANAGE) or principal.can(Permission.CONFIG_READ)):
            await self._authz.deny(
                principal, [Permission.CONFIG_READ], target="guardrails", meta=meta
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            return GuardrailPolicy.parse(await self._repo.guardrails(conn), strict=False)

    async def update(
        self, principal: Principal, changes: dict[str, object], meta: RequestMeta
    ) -> GuardrailPolicy:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="guardrails", meta=meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            old = GuardrailPolicy.parse(await self._repo.guardrails(conn, lock=True), strict=False)
            new = GuardrailPolicy.parse({**old.to_dict(), **changes})
            before, after = old.to_dict(), new.to_dict()
            if new != old:
                await self._repo.set_guardrails(conn, new)
                await self._audit.append(
                    conn,
                    AuditEvent(
                        action=AuditAction.POLICY_CHANGED,
                        actor_type="user",
                        actor_id=principal.user_id,
                        target_type="org",
                        target_id=str(principal.org_id),
                        # Entries rather than keys: one setting is named `secrets`, a word
                        # the audit guard rightly refuses as a key.
                        details={
                            "policy": "guardrails",
                            "changes": [
                                {"setting": name, "old": value, "new": after[name]}
                                for name, value in before.items()
                                if after[name] != value
                            ],
                        },
                        ip=meta.ip,
                        request_id=meta.request_id,
                    ),
                )
        self._cache.invalidate(principal.org_id)
        return new
