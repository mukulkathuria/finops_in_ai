"""Optimization stage administration (US-26): the registered stages with each org's mode and
latest eval run, recording eval runs, and switching a stage off, to shadow or on. `on` needs a
passed eval run for the stage's current version. Every change is audited."""

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.stages import EvalRun, NewEvalRun, StageModeCache, StageRepository
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Conflict, NotFound
from gateway.domain.policy import Permission, Principal
from gateway.domain.stages import Mode, StageSetting, effective_mode
from gateway.pipeline.stages import StageRunner
from gateway.ports.stages import StageInfo


@dataclass(frozen=True, slots=True)
class StageView:
    name: str
    version: str
    phase: str
    mode: Mode  # what runs now: `on` recorded for another version runs in shadow
    default_mode: Mode
    latest_eval: EvalRun | None  # the latest run for this version


@dataclass(frozen=True, slots=True)
class EvalIn:
    dataset: str
    metric: str
    score: float
    threshold: float
    samples: int


def _view(
    stage: StageInfo,
    settings: Mapping[str, StageSetting],
    evals: Mapping[tuple[str, str], EvalRun],
) -> StageView:
    mode = effective_mode(
        settings.get(stage.name), version=stage.version, default=stage.default_mode
    )
    return StageView(
        stage.name,
        stage.version,
        stage.phase,
        mode,
        stage.default_mode,
        evals.get((stage.name, stage.version)),
    )


class StageAdminService:
    def __init__(
        self,
        *,
        db: Database,
        repo: StageRepository,
        modes: StageModeCache,
        runner: StageRunner,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._repo = repo
        self._modes = modes
        self._runner = runner
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    def _stage(self, name: str) -> StageInfo:
        stage = self._runner.get(name)
        if stage is None:
            raise NotFound(f"there is no optimization stage named {name!r}", code="stage_not_found")
        return stage

    async def list(self, principal: Principal, meta: RequestMeta) -> list[StageView]:
        if not (principal.can(Permission.ORG_MANAGE) or principal.can(Permission.CONFIG_READ)):
            await self._authz.deny(principal, [Permission.CONFIG_READ], target="stages", meta=meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            settings = await self._repo.settings(conn)
            evals = {(e.stage, e.version): e for e in await self._repo.latest_evals(conn)}
        return [_view(s, settings, evals) for s in self._runner.stages]

    async def set_mode(
        self, principal: Principal, name: str, mode: Mode, meta: RequestMeta
    ) -> StageView:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="stages", meta=meta)
        stage = self._stage(name)
        async with self._db.transaction(self._ctx(principal)) as conn:
            settings = await self._repo.settings(conn, lock=True)
            if mode == "on" and not await self._repo.passed(conn, stage.name, stage.version):
                raise Conflict(
                    f"{stage.name} {stage.version} has no passed eval run; record one first",
                    code="eval_gate_not_passed",
                )
            old = _view(stage, settings, {}).mode
            await self._repo.set_mode(conn, stage.name, mode, stage.version)
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.STAGE_MODE_SET,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="stage",
                    target_id=stage.name,
                    details={"version": stage.version, "old": old, "new": mode},
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
            evals = {(e.stage, e.version): e for e in await self._repo.latest_evals(conn)}
        self._modes.invalidate(principal.org_id)
        return _view(stage, {**settings, stage.name: StageSetting(mode, stage.version)}, evals)

    async def record_eval(
        self, principal: Principal, name: str, run: EvalIn, meta: RequestMeta
    ) -> EvalRun:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="stages", meta=meta)
        stage = self._stage(name)
        async with self._db.transaction(self._ctx(principal)) as conn:
            stored = await self._repo.record_eval(
                conn,
                NewEvalRun(
                    stage.name,
                    stage.version,
                    run.dataset,
                    run.metric,
                    run.score,
                    run.threshold,
                    run.samples,
                    principal.user_id,
                ),
            )
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.EVAL_RECORDED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="stage",
                    target_id=stage.name,
                    details={
                        "version": stage.version,
                        "dataset": run.dataset,
                        "metric": run.metric,
                        "score": run.score,
                        "threshold": run.threshold,
                        "passed": stored.passed,
                    },
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
            demoted = not stored.passed and await self._demote(conn, principal, stage, meta)
        if demoted:
            self._modes.invalidate(principal.org_id)
        return stored

    async def _demote(
        self, conn: AsyncConnection, principal: Principal, stage: StageInfo, meta: RequestMeta
    ) -> bool:
        """A failing run takes a stage that is on for this version back to shadow (audited)."""
        setting = (await self._repo.settings(conn, lock=True)).get(stage.name)
        if setting is None or setting.mode != "on" or setting.version != stage.version:
            return False
        await self._repo.set_mode(conn, stage.name, "shadow", stage.version)
        await self._audit.append(
            conn,
            AuditEvent(
                action=AuditAction.STAGE_MODE_SET,
                actor_type="user",
                actor_id=principal.user_id,
                target_type="stage",
                target_id=stage.name,
                details={
                    "version": stage.version,
                    "old": "on",
                    "new": "shadow",
                    "reason": "eval_failed",
                },
                ip=meta.ip,
                request_id=meta.request_id,
            ),
        )
        return True
