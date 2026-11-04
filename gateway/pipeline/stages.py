"""Running the optimization stages (spec section 4, step 7; US-26), and the first technique:
lossless whitespace compaction of what users and tools wrote."""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Final

from gateway.domain.compression import compact
from gateway.domain.llm import ContentPart, Message, TextPart
from gateway.domain.parsing import text_tokens
from gateway.domain.stages import (
    STAGE_NAME,
    Mode,
    Phase,
    RequestContext,
    StageReport,
    effective_mode,
)
from gateway.ports.stages import Stage, StageModes

log = logging.getLogger(__name__)

_INLINE_CHARS: Final = 64 * 1024  # larger texts are compacted off the event loop


class StageRunner:
    """Runs a phase's stages in their registered order. Each works on a copy of the request: a
    stage `on` hands its result on, one in `shadow` reports what it would have saved and its
    result is dropped, and one that fails is reported and skipped, never failing the request."""

    def __init__(self, stages: Sequence[Stage], modes: StageModes) -> None:
        names = [s.name for s in stages]
        if len(names) != len(set(names)) or not all(STAGE_NAME.fullmatch(n) for n in names):
            raise ValueError("stage names must be unique lowercase identifiers")
        if any(s.default_mode == "on" for s in stages):
            raise ValueError("a stage starts off or in shadow: `on` needs a passed eval run")
        self._stages = tuple(stages)
        self._modes = modes

    @property
    def stages(self) -> tuple[Stage, ...]:
        return self._stages

    def get(self, name: str) -> Stage | None:
        return next((s for s in self._stages if s.name == name), None)

    async def run(self, ctx: RequestContext, phase: Phase) -> list[StageReport]:
        selected = [s for s in self._stages if s.phase == phase]
        if not selected:
            return []
        settings = await self._modes.get(ctx.org_id)
        reports: list[StageReport] = []
        for stage in selected:
            setting = settings.get(stage.name)
            mode = effective_mode(setting, version=stage.version, default=stage.default_mode)
            if mode == "off":
                continue
            work = ctx.fork()
            started = time.perf_counter()
            try:
                report = await stage.process(work)
            except Exception:
                log.exception("optimization stage failed; skipped", extra={"stage": stage.name})
                report = StageReport(
                    stage.name, stage.version, phase, 0, 0, 0, decisions={"error": "failed"}
                )
            shadow = mode == "shadow"
            if report.applied and not shadow:
                ctx.request = work.request
            elapsed = int((time.perf_counter() - started) * 1000)
            reports.append(replace(report, latency_ms=elapsed, shadow=shadow))
        return reports


_COMPACTED: Final = frozenset({"user", "tool"})  # system prompts stay as the developer wrote them


def _compacted(messages: Sequence[Message]) -> tuple[tuple[Message, ...], int, int]:
    """The messages with user and tool text compacted, and the text tokens before and after."""
    before = after = 0
    out: list[Message] = []
    for m in messages:
        if m.role not in _COMPACTED:
            out.append(m)
            continue
        parts: list[ContentPart] = []
        for p in m.content:
            if isinstance(p, TextPart):
                tidy = compact(p.text)
                before, after = before + text_tokens(p.text), after + text_tokens(tidy)
                parts.append(replace(p, text=tidy))
            else:
                parts.append(p)
        out.append(replace(m, content=tuple(parts)))
    return tuple(out), before, after


class CompressionStage:
    """Lossless whitespace compaction of user and tool text; ships in shadow mode."""

    name = "compression"
    version = "1.0"
    phase: Phase = "pre_llm"
    default_mode: Mode = "shadow"

    async def process(self, ctx: RequestContext) -> StageReport:
        messages = ctx.request.messages
        size = sum(len(p.text) for m in messages for p in m.content if isinstance(p, TextPart))
        if size > _INLINE_CHARS:
            compacted, before, after = await asyncio.to_thread(_compacted, messages)
        else:
            compacted, before, after = _compacted(messages)
        applied = after < before
        if applied:
            ctx.request = replace(ctx.request, messages=compacted)
        return StageReport(
            self.name,
            self.version,
            self.phase,
            before,
            after,
            ctx.input_cost(after) - ctx.input_cost(before),
            quality_risk=0.05,
            applied=applied,
            decisions={"method": "whitespace"},
        )
