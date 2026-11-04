"""Optimization stage ports (spec section 4, "Stage contract"; US-26)."""

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from gateway.domain.stages import Mode, Phase, RequestContext, StageReport, StageSetting


class Stage(Protocol):
    """One optimization technique. `process` may replace `ctx.request` with a cheaper equivalent
    and reports what it changed. It always works on a copy, so a failure or a shadow run leaves
    the request that is sent untouched."""

    @property
    def name(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def phase(self) -> Phase: ...
    @property
    def default_mode(self) -> Mode: ...

    async def process(self, ctx: RequestContext) -> StageReport: ...


class StageModes(Protocol):
    """Each org's stage settings: a snapshot that may lag a change by its TTL."""

    async def get(self, org_id: UUID) -> Mapping[str, StageSetting]: ...
