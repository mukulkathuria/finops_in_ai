"""Knowledge domains (ADR 0011): an org admin declares what graph retrieval is for, such as
`medical`. The domain's example questions are embedded into its intent, which a query must come
near to be answered from the graph. Its entity and relation types steer the extraction of the
documents filed under it. Creating and deleting a domain is audited; deleting one removes its
graph, and its documents stay, filed under no domain."""

from collections.abc import Sequence
from uuid import UUID

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.knowledge import DomainRecord, KnowledgeRepository
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Conflict, InvalidInput, NotFound
from gateway.domain.knowledge import centroid, check_types
from gateway.domain.policy import Permission, Principal
from gateway.ports.embeddings import Embedder


class KnowledgeService:
    def __init__(
        self,
        *,
        db: Database,
        repo: KnowledgeRepository,
        embedder: Embedder,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._repo = repo
        self._embedder = embedder
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    def _event(
        self, principal: Principal, domain: DomainRecord, change: str, meta: RequestMeta
    ) -> AuditEvent:
        return AuditEvent(
            action=AuditAction.SETTINGS_CHANGED,
            actor_type="user",
            actor_id=principal.user_id,
            target_type="knowledge_domain",
            target_id=str(domain.id),
            details={"setting": "knowledge_domain", change: domain.name},
            ip=meta.ip,
            request_id=meta.request_id,
        )

    async def create(
        self,
        principal: Principal,
        meta: RequestMeta,
        *,
        name: str,
        description: str,
        entity_types: Sequence[str],
        relation_types: Sequence[str],
        examples: Sequence[str],
        threshold: float,
    ) -> DomainRecord:
        await self._authz.ensure(
            principal, Permission.ORG_MANAGE, target="knowledge_domain", meta=meta
        )
        questions = [q.strip() for q in examples if q.strip()]
        try:
            entities = check_types(entity_types, "entity types")
            relations = check_types(relation_types, "relation types")
            if not questions:
                raise ValueError("a domain needs at least one example question")
        except ValueError as exc:
            raise InvalidInput(str(exc), code="invalid_knowledge_domain") from exc
        vectors = await self._embedder.embed(questions)
        try:
            intent = centroid(vectors)
        except ValueError as exc:
            raise InvalidInput(str(exc), code="invalid_knowledge_domain") from exc
        async with self._db.transaction(self._ctx(principal)) as conn:
            record = await self._repo.create(
                conn,
                name=name,
                description=description.strip(),
                entity_types=entities,
                relation_types=relations,
                examples=questions,
                threshold=threshold,
                vector=intent,
                model=self._embedder.model,
                created_by=principal.user_id,
            )
            if record is None:
                raise Conflict(
                    f"the organisation has a knowledge domain named {name}",
                    code="knowledge_domain_exists",
                )
            await self._audit.append(conn, self._event(principal, record, "created", meta))
        return record

    async def domains(self, principal: Principal, meta: RequestMeta) -> list[DomainRecord]:
        if not (principal.can(Permission.ORG_MANAGE) or principal.can(Permission.CONFIG_READ)):
            await self._authz.deny(
                principal, [Permission.CONFIG_READ], target="knowledge_domain", meta=meta
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._repo.domains(conn)

    async def delete(self, principal: Principal, domain_id: UUID, meta: RequestMeta) -> None:
        await self._authz.ensure(
            principal, Permission.ORG_MANAGE, target="knowledge_domain", meta=meta
        )
        async with self._db.transaction(self._ctx(principal)) as conn:
            found = next((d for d in await self._repo.domains(conn) if d.id == domain_id), None)
            if found is None or not await self._repo.delete(conn, domain_id):
                raise NotFound("no such knowledge domain", code="knowledge_domain_not_found")
            await self._audit.append(conn, self._event(principal, found, "deleted", meta))
