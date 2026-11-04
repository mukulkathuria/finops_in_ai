"""Stage settings, stage reports and eval runs (US-26). Settings live in the org's settings;
reports are written in the settlement transaction, next to the ledger row; an eval run's
`passed` is computed by the database from its score and threshold."""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_all, fetch_one
from gateway.domain.stages import Mode, StageReport, StageSetting, stage_settings

_SETTINGS: Final = "SELECT settings -> 'stages' AS stages FROM orgs WHERE id = app_current_org()"
_SETTINGS_LOCKED: Final = _SETTINGS + " FOR UPDATE"
_SET: Final = """
UPDATE orgs SET settings = jsonb_set(
  settings, '{stages}',
  coalesce(settings -> 'stages', '{}'::jsonb)
    || jsonb_build_object(
         CAST(:name AS text),
         jsonb_build_object('mode', CAST(:mode AS text), 'version', CAST(:version AS text))))
WHERE id = app_current_org()
RETURNING id
"""
_REPORT: Final = """
INSERT INTO stage_reports (org_id, request_id, ts, stage, version, phase, tokens_before,
  tokens_after, cost_delta_micro_usd, latency_ms, quality_risk, applied, shadow, decisions)
VALUES (app_current_org(), :request_id, :ts, :stage, :version, :phase, :tokens_before,
  :tokens_after, :cost_delta, :latency_ms, :quality_risk, :applied, :shadow,
  CAST(:decisions AS jsonb))
"""
_EVAL: Final = """
INSERT INTO eval_runs (org_id, stage, version, dataset, metric, score, threshold, samples,
  created_by)
VALUES (app_current_org(), :stage, :version, :dataset, :metric, :score, :threshold, :samples,
  :created_by)
RETURNING id, stage, version, dataset, metric, score, threshold, samples, passed, created_at
"""
_LATEST_EVALS: Final = """
SELECT DISTINCT ON (stage, version) id, stage, version, dataset, metric, score, threshold,
       samples, passed, created_at
FROM eval_runs
ORDER BY stage, version, created_at DESC
"""
_PASSED: Final = """
SELECT EXISTS (SELECT 1 FROM eval_runs WHERE stage = :stage AND version = :version AND passed)
  AS passed
"""


@dataclass(frozen=True, slots=True)
class EvalRun:
    id: UUID
    stage: str
    version: str
    dataset: str
    metric: str
    score: float
    threshold: float
    samples: int
    passed: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewEvalRun:
    stage: str
    version: str
    dataset: str
    metric: str
    score: float
    threshold: float
    samples: int
    created_by: UUID | None


class StageRepository:
    async def settings(
        self, conn: AsyncConnection, *, lock: bool = False
    ) -> dict[str, StageSetting]:
        row = await fetch_one(conn, _SETTINGS_LOCKED if lock else _SETTINGS)
        raw = row["stages"] if row is not None else None
        return stage_settings(raw if isinstance(raw, dict) else None)

    async def set_mode(self, conn: AsyncConnection, name: str, mode: Mode, version: str) -> None:
        await fetch_one(conn, _SET, {"name": name, "mode": mode, "version": version})

    async def record_reports(
        self,
        conn: AsyncConnection,
        *,
        request_id: str,
        ts: datetime,
        reports: Sequence[StageReport],
    ) -> None:
        await conn.execute(
            text(_REPORT),
            [
                {
                    "request_id": request_id,
                    "ts": ts,
                    "stage": r.stage,
                    "version": r.version,
                    "phase": r.phase,
                    "tokens_before": r.tokens_before,
                    "tokens_after": r.tokens_after,
                    "cost_delta": r.cost_delta_micro_usd,
                    "latency_ms": r.latency_ms,
                    "quality_risk": r.quality_risk,
                    "applied": r.applied,
                    "shadow": r.shadow,
                    "decisions": orjson.dumps(dict(r.decisions)).decode(),
                }
                for r in reports
            ],
        )

    async def record_eval(self, conn: AsyncConnection, run: NewEvalRun) -> EvalRun:
        row = await fetch_one(
            conn,
            _EVAL,
            {
                "stage": run.stage,
                "version": run.version,
                "dataset": run.dataset,
                "metric": run.metric,
                "score": run.score,
                "threshold": run.threshold,
                "samples": run.samples,
                "created_by": run.created_by,
            },
        )
        if row is None:
            raise RuntimeError("the eval run insert returned no row")
        return EvalRun(**dict(row))

    async def latest_evals(self, conn: AsyncConnection) -> list[EvalRun]:
        return [EvalRun(**dict(row)) for row in await fetch_all(conn, _LATEST_EVALS)]

    async def passed(self, conn: AsyncConnection, stage: str, version: str) -> bool:
        row = await fetch_one(conn, _PASSED, {"stage": stage, "version": version})
        return bool(row is not None and row["passed"])


class StageModeCache:
    """Per-org stage settings as snapshots (the `PolicyCache` pattern): a change applies at once
    in this process and within the TTL in every other replica."""

    def __init__(
        self,
        db: Database,
        repo: StageRepository,
        *,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._repo = repo
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[UUID, tuple[float, dict[str, StageSetting]]] = {}

    async def get(self, org_id: UUID) -> Mapping[str, StageSetting]:
        entry = self._entries.get(org_id)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        async with self._db.transaction(TenantContext(org_id)) as conn:
            settings = await self._repo.settings(conn)
        self._entries[org_id] = (self._clock() + self._ttl, settings)
        return settings

    def invalidate(self, org_id: UUID) -> None:
        self._entries.pop(org_id, None)
