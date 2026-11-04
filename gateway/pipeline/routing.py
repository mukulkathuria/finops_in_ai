"""The router stage and its quality check (spec section 4, step 8; US-19)."""

import math
import random
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Final
from uuid import UUID

from gateway.adapters.postgres.catalog import CatalogCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.routing import RoutingRepository, RoutingSettingsCache
from gateway.domain.llm import CanonicalResponse
from gateway.domain.parsing import text_tokens
from gateway.domain.routing import ROUTER, complexity, fits
from gateway.domain.stages import Mode, Phase, RequestContext, StageReport
from gateway.ports.embeddings import Embedder

_COMPARED_CHARS: Final = 8_000  # of each answer: enough to tell whether two answers agree


class RouterStage:
    """An easy request for the org's large tier goes to its small tier. Ships off. Its saving is
    priced when the request settles, on the tokens it used; until then it reports zero."""

    name = ROUTER
    version = "1.0"
    phase: Phase = "pre_llm"
    default_mode: Mode = "off"

    def __init__(
        self,
        settings: RoutingSettingsCache,
        catalog: CatalogCache,
        *,
        sample: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._sample = sample

    def _report(
        self, decisions: dict[str, Any], *, risk: float = 0.0, applied: bool = False
    ) -> StageReport:
        return StageReport(
            self.name,
            self.version,
            self.phase,
            0,
            0,
            0,
            quality_risk=risk,
            applied=applied,
            decisions=decisions,
        )

    async def process(self, ctx: RequestContext) -> StageReport:
        settings = await self._settings.get(ctx.org_id)
        large, req = ctx.price, ctx.request
        if not settings.configured or large.public_name != settings.large_model:
            return self._report({"skipped": "model"})
        if ctx.team_id is not None and str(ctx.team_id) in settings.opt_out_teams:
            return self._report({"skipped": "opted_out"})
        score = round(complexity(req), 3)
        if score >= settings.threshold:
            return self._report({"tier": "large", "score": score})
        small = (await self._catalog.get()).resolve(settings.small_model or "")
        if (
            small is None
            or small.kind != "chat"
            or small.provider != large.provider
            or not fits(small, req)
        ):
            return self._report({"tier": "large", "score": score, "skipped": "small_model"})
        ctx.request, ctx.price = replace(req, model=small.model), small
        decisions: dict[str, Any] = {
            "tier": "small",
            "score": score,
            "from": large.public_name,
            "to": small.public_name,
        }
        # Which requests are checked is a sample, not a secret.
        if self._sample() < settings.judge_rate:  # nosec B311
            decisions["judge"] = True
        return self._report(decisions, risk=score, applied=True)


def said(response: CanonicalResponse) -> str:
    """What an answer says, for comparing two answers: its text, or the tools it calls."""
    if response.content:
        return response.content
    return "\n".join(f"{c.name}({c.arguments})" for c in response.tool_calls)


class Verifier:
    """The sampled quality check: how far an answer from the small tier agrees with the large
    tier's answer to the same request, as the cosine similarity of their embeddings."""

    def __init__(self, *, db: Database, repo: RoutingRepository, embedder: Embedder | None) -> None:
        self._db = db
        self._repo = repo
        self._embedder = embedder

    async def agreement(self, answer: str, reference: str) -> tuple[float, int]:
        """The agreement, from -1 to 1, and the tokens embedded to measure it."""
        answer, reference = answer[:_COMPARED_CHARS].strip(), reference[:_COMPARED_CHARS].strip()
        if self._embedder is None or not answer or not reference:
            return (1.0 if answer == reference else 0.0), 0
        a, b = await self._embedder.embed([answer, reference])
        dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
        norm = math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(y * y for y in b))
        score = max(-1.0, min(1.0, dot / norm)) if norm else 0.0
        return score, text_tokens(answer) + text_tokens(reference)

    async def record(
        self, org_id: UUID, *, request_id: str, candidate: str, reference: str, score: float
    ) -> None:
        async with self._db.transaction(TenantContext(org_id)) as conn:
            await self._repo.record_sample(
                conn, request_id=request_id, candidate=candidate, reference=reference, score=score
            )
