"""Chat and embeddings use cases (US-05, US-07, US-10, US-29).

Every request follows spec section 6: reserve the worst-case cost against each applicable budget
in one transaction, call the provider with retries, then settle the actual cost and append the
ledger row in one transaction. A failed call releases its hold. Streams settle when they end, fail
or are closed, using the provider's usage report or an estimate.

Settlement is shielded from the caller's cancellation (a disconnect or a shutdown must not lose a
charge the provider already made) and retried on transient database failures; it is idempotent
per hold, so a retry after an unacknowledged commit changes nothing.

With an Idempotency-Key, the key is claimed in the reserve transaction and completed, with the
canonical result, in the settle transaction: a retry of a completed request replays that result
and is never reserved or charged again. A failed or cancelled attempt frees its key, so the
client may run the request again (the partial output of that attempt stays billed).

Each stage (budget.reserve, the provider call, budget.settle) is a span of the request's trace
and a sample of the stage-duration histogram; no prompt or completion text is ever recorded.
"""

import asyncio
import logging
import random
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NoReturn, Protocol, TypeVar
from uuid import UUID, uuid7

from opentelemetry.trace import Span
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.llm.registry import OrgAdapters
from gateway.adapters.observability.telemetry import Telemetry
from gateway.adapters.postgres.budgets import BudgetRepository
from gateway.adapters.postgres.catalog import CatalogCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.idempotency import IdempotencyRepository
from gateway.adapters.postgres.savings import BaselineModels
from gateway.adapters.postgres.stages import StageRepository
from gateway.adapters.postgres.usage import UsageEvent, UsageRepository
from gateway.domain.budgets import BudgetExceeded, Demand, chat_demand, embed_demand
from gateway.domain.cache import CACHE_STAGES, cacheable
from gateway.domain.errors import DomainError, NotFound, Unavailable
from gateway.domain.guardrails import GuardrailPolicy
from gateway.domain.idempotency import Idempotency
from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    CanonicalResponse,
    EmbedRequest,
    EmbedResponse,
    FinishReason,
    ProviderError,
    ToolCall,
    ToolCallDelta,
    Usage,
)
from gateway.domain.pricing import Catalog, ModelKind, ModelPrice
from gateway.domain.retrieval import Hit
from gateway.domain.routing import ROUTER
from gateway.domain.savings import (
    OverheadRates,
    baseline_cost,
    cache_hit_report,
    file_conversion_report,
    overhead_cost,
    overhead_inputs,
    provider_cache_report,
    unserved,
)
from gateway.domain.stages import RequestContext, StageReport
from gateway.pipeline.cache import CacheHit, Lookup, ResponseCache
from gateway.pipeline.guardrails import Guardrails, Inspection, OutputFilter, OutputStream
from gateway.pipeline.idempotency import Origin, dump_chat, dump_embed, load_chat, load_embed
from gateway.pipeline.routing import Verifier, said
from gateway.pipeline.stages import StageRunner
from gateway.ports.llm import LLMAdapter

T = TypeVar("T")
log = logging.getLogger(__name__)
_MAX_BACKOFF_S: Final = 4.0
# A hold must outlive the longest provider call; the janitor returns holds of crashed processes.
HOLD_TTL: Final = timedelta(minutes=15)
# A dropped connection, a pool or statement timeout or a deadlock: worth another attempt.
_TRANSIENT: Final = (OperationalError, InterfaceError, PoolTimeoutError)


class _RolledBack(Exception):
    """Ends a transaction that must leave nothing behind (a budget precheck)."""


_DB_ATTEMPTS: Final = 3
_MAX_JUDGES: Final = 8  # quality checks in flight at once; past that a sample is skipped


@dataclass(frozen=True, slots=True)
class Attribution:
    org_id: UUID
    team_id: UUID | None
    user_id: UUID | None
    api_key_id: UUID | None
    request_id: str
    # The request clock: it picks the budget period and stamps the ledger row, so both agree.
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class Resolved:
    entry: ModelPrice
    adapter: LLMAdapter


@dataclass(frozen=True, slots=True)
class StreamOutcome:
    """Filled in while a stream runs; read by the API after the last chunk."""

    usage: Usage
    finish_reason: FinishReason | None
    cost_micro_usd: int


class ChunkStream(Protocol):
    """An async iterator of chunks that can be closed at any time, even before its first read."""

    def __aiter__(self) -> AsyncIterator[CanonicalChunk]: ...
    def __anext__(self) -> Awaitable[CanonicalChunk]: ...
    def aclose(self) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class ChatResult:
    response: CanonicalResponse
    cost: int
    origin: Origin
    cache_hit: str = "none"  # exact or semantic when the answer came from the cache


@dataclass(frozen=True, slots=True)
class EmbedResult:
    response: EmbedResponse
    cost: int
    origin: Origin


@dataclass(frozen=True, slots=True)
class StreamResult:
    chunks: ChunkStream
    origin: Origin
    cache_hit: str = "none"


@dataclass(frozen=True, slots=True)
class _Call:
    """One request in flight, the budget hold it carries and its idempotency key, if any."""

    who: Attribution
    r: Resolved
    kind: str
    hold: str
    started: float
    idem: Idempotency | None
    # Guardrail decisions for the ledger row; every exit path settles through `_end`.
    decisions: list[dict[str, object]] = field(default_factory=list)
    notes: dict[str, object] = field(default_factory=dict)  # more ledger flags (attachments)
    reports: list[StageReport] = field(default_factory=list)  # what the stages did
    cache: list[Lookup] = field(default_factory=list)  # the cache keys (at most one), if cached
    requested: ModelPrice | None = None  # the model asked for, when the router picked `r`

    @property
    def origin(self) -> Origin:
        served = self.r.entry.public_name if self.requested is not None else None
        return Origin(self.who.request_id, self.who.at, model=served)


@dataclass(frozen=True, slots=True)
class _StreamEnd:
    """What a finished stream settles with."""

    usage: Usage
    cost: int
    status: str
    charged: bool
    finish: FinishReason | None
    stored: bytes | None
    response: CanonicalResponse | None = None  # the whole answer, when it ended well


def estimate_tokens(text: str) -> int:
    """Rough count (about 4 characters per token) used only when a provider reports nothing."""
    return (len(text) + 3) // 4


def _spent(usage: Usage, cost: int) -> Demand:
    return Demand(usd_micro=cost, tokens=usage.input_tokens + usage.output_tokens)


def _measured(req: CanonicalRequest, response: CanonicalResponse) -> Usage:
    """The provider's usage report, or an estimate when it reported nothing."""
    if not response.usage.estimated:
        return response.usage
    prompt = "".join(m.text() for m in req.messages)
    return Usage(
        input_tokens=estimate_tokens(prompt),
        output_tokens=estimate_tokens(response.content or ""),
        estimated=True,
    )


def _route_tier(reports: Sequence[StageReport]) -> str | None:
    """The tier the router sent the request to; None when it did not route it."""
    for report in reports:
        if report.stage == ROUTER and not report.shadow:
            tier = report.decisions.get("tier")
            return str(tier) if tier else None
    return None


def _price_route(reports: list[StageReport], catalog: Catalog, used: Usage) -> None:
    """Price the router's choice on the tokens the request used, at full price (it reports
    zero until then; the provider's cache reports its own discount)."""
    full = Usage(input_tokens=used.input_tokens, output_tokens=used.output_tokens)
    for i, report in enumerate(reports):
        if report.stage == ROUTER and report.applied:
            small = catalog.resolve(str(report.decisions.get("to")))
            large = catalog.resolve(str(report.decisions.get("from")))
            if small is not None and large is not None:
                delta = small.cost(full) - large.cost(full)
                reports[i] = replace(report, cost_delta_micro_usd=delta)


async def _replay_stream(
    response: CanonicalResponse, cost: int, outcome: list[StreamOutcome]
) -> AsyncGenerator[CanonicalChunk]:
    """A finished stream, replayed in one chunk for a retry with the same Idempotency-Key."""
    outcome.append(StreamOutcome(response.usage, response.finish_reason, cost))
    yield CanonicalChunk(
        content=response.content,
        tool_calls=tuple(
            ToolCallDelta(i, c.id, c.name, c.arguments) for i, c in enumerate(response.tool_calls)
        ),
        finish_reason=response.finish_reason,
        usage=response.usage,
    )


class _Relay:
    """Relays provider chunks and settles the request exactly once: when the stream ends or fails,
    or when it is closed, even before its first chunk was read. (An async generator closed before
    it started runs none of its body, so it could never settle.)"""

    def __init__(
        self,
        call: _Call,
        req: CanonicalRequest,
        first: CanonicalChunk | None,
        upstream: AsyncIterator[CanonicalChunk],
        outcome: list[StreamOutcome],
        on_end: Callable[[_StreamEnd], None],
        output: OutputFilter | None = None,
    ) -> None:
        self._call = call
        self._output = output
        self._text = output.stream() if output is not None else None
        self._arguments: dict[int, OutputStream] = {}  # tool-call index -> its filtered stream
        self._exhausted = False
        self._req = req
        self._next = first
        self._upstream = upstream
        self._outcome = outcome
        self._on_end = on_end
        self._started = False
        self._settled = False
        self._usage: Usage | None = None
        self._finish: FinishReason | None = None
        self._produced: list[str] = []
        self._calls: dict[int, list[str]] = {}  # tool-call index -> [id, name, arguments]

    def __aiter__(self) -> _Relay:
        return self

    async def __anext__(self) -> CanonicalChunk:
        if self._settled:
            raise StopAsyncIteration
        if self._started and not self._exhausted:
            try:
                self._next = await anext(self._upstream, None)
            except asyncio.CancelledError:
                self._settle("cancelled")
                raise
            except Exception:
                self._settle("error")
                raise
        elif self._exhausted:
            self._next = None
        self._started = True
        chunk = self._next
        if chunk is None:
            self._exhausted = True
            if (tail := self._held()) is not None:  # output the guardrail was still holding back
                self._track(tail)
                return tail
            self._settle("ok")
            raise StopAsyncIteration
        chunk = self._filtered(chunk)
        self._track(chunk)
        return chunk

    def _filtered(self, chunk: CanonicalChunk) -> CanonicalChunk:
        output, text = self._output, self._text
        if output is None or text is None:
            return chunk
        content = text.feed(chunk.content or "")
        deltas: list[ToolCallDelta] = []
        for d in chunk.tool_calls:
            if d.index not in self._arguments:
                self._arguments[d.index] = output.stream()
            deltas.append(replace(d, arguments=self._arguments[d.index].feed(d.arguments)))
        # Only a finish ends the content: some servers send usage with every event, and a
        # usage-only chunk must not release a value the guardrail is holding back.
        final = chunk.finish_reason is not None
        # Nothing may follow the final chunk: it carries everything still held back.
        if final and (tail := self._held()) is not None:
            content += tail.content or ""
            held = {d.index: d.arguments for d in tail.tool_calls}
            deltas = [replace(d, arguments=d.arguments + held.pop(d.index, "")) for d in deltas]
            deltas += [ToolCallDelta(i, arguments=rest) for i, rest in held.items()]
        return replace(chunk, content=content or None, tool_calls=tuple(deltas))

    def _held(self) -> CanonicalChunk | None:
        """What the output guardrail still holds back, as one chunk (None when nothing is)."""
        if self._text is None:
            return None
        content = self._text.flush()
        deltas = tuple(
            ToolCallDelta(i, arguments=rest)
            for i, stream in sorted(self._arguments.items())
            if (rest := stream.flush())
        )
        if not content and not deltas:
            return None
        return CanonicalChunk(content=content or None, tool_calls=deltas)

    async def aclose(self) -> None:
        self._settle("cancelled")  # a no-op once the stream has ended

    def _track(self, chunk: CanonicalChunk) -> None:
        if chunk.usage is not None:
            self._usage = chunk.usage
        self._finish = chunk.finish_reason or self._finish
        if chunk.content:
            self._produced.append(chunk.content)
        for delta in chunk.tool_calls:
            slot = self._calls.setdefault(delta.index, ["", "", ""])
            slot[0] = delta.id or slot[0]
            slot[1] = delta.name or slot[1]
            slot[2] += delta.arguments

    def _settle(self, status: str) -> None:
        if self._settled:
            return
        self._settled = True
        call, usage = self._call, self._usage
        # What the guardrail still held when the stream failed was never sent, but the model
        # produced it, so it counts (after a normal end the tail has already been sent).
        tail = self._held() or CanonicalChunk()
        held = (tail.content or "") + "".join(d.arguments for d in tail.tool_calls)
        if usage is None or usage.estimated:
            prompt = "".join(m.text() for m in self._req.messages)
            output = "".join(self._produced) + "".join(c[1] + c[2] for c in self._calls.values())
            usage = Usage(
                input_tokens=estimate_tokens(prompt),
                output_tokens=estimate_tokens(output + held),
                estimated=True,
            )
        # An error before any output is not charged; a cancelled stream bills its prompt.
        charged = status != "error" or bool(self._produced or self._calls or held)
        cost = call.r.entry.cost(usage) if charged else 0
        self._outcome.append(StreamOutcome(usage, self._finish, cost))
        assembled: CanonicalResponse | None = None
        stored = None
        if status == "ok":
            assembled = CanonicalResponse(
                id="",
                model=call.r.entry.model,
                content="".join(self._produced) or None,
                tool_calls=tuple(ToolCall(*self._calls[i]) for i in sorted(self._calls)),
                finish_reason=self._finish or "stop",
                usage=usage,
            )
            if call.idem is not None:
                stored = dump_chat(call.origin, cost, assembled)
        self._on_end(
            _StreamEnd(
                usage if charged else Usage(),
                cost,
                status,
                charged,
                self._finish,
                stored,
                assembled,
            )
        )


class ChatService:
    def __init__(
        self,
        *,
        db: Database,
        guardrails: Guardrails,
        catalog: CatalogCache,
        adapters: Mapping[str, LLMAdapter],
        usage: UsageRepository,
        budgets: BudgetRepository,
        idempotency: IdempotencyRepository,
        telemetry: Telemetry,
        default_max_tokens: int,
        retries: int,
        stages: StageRunner,
        stage_reports: StageRepository,
        overhead: OverheadRates,
        baselines: BaselineModels,
        cache: ResponseCache,
        verifier: Verifier | None = None,
        org_adapters: OrgAdapters | None = None,
    ) -> None:
        self._org_adapters = org_adapters
        self._db = db
        self._catalog = catalog
        self._adapters = adapters
        self._usage = usage
        self._budgets = budgets
        self._idempotency = idempotency
        self._telemetry = telemetry
        self._guardrails = guardrails
        self.default_max_tokens = default_max_tokens
        self._retries = retries
        self._stages = stages
        self._stage_reports = stage_reports
        self._overhead = overhead
        self._baselines = baselines
        self._cache = cache
        self._verifier = verifier
        self._judging = 0  # quality checks in flight
        self._draining = False  # set by drain(): no new quality check starts
        self._background: set[asyncio.Task[None]] = set()

    async def resolve(self, model: str, kind: ModelKind, org_id: UUID | None = None) -> Resolved:
        entry = (await self._catalog.get()).resolve(model)
        if entry is None or entry.kind != kind:
            raise NotFound(
                f"model '{model}' does not exist or is not available",
                code="model_not_found",
                param="model",
            )
        adapter = None
        if org_id is not None and self._org_adapters is not None:
            adapter = (await self._org_adapters.get(org_id)).get(entry.provider)
        adapter = adapter or self._adapters.get(entry.provider)
        if adapter is None:
            raise Unavailable(
                f"provider '{entry.provider}' is not configured on this gateway",
                code="provider_not_configured",
            )
        return Resolved(entry, adapter)

    async def models(self) -> list[ModelPrice]:
        catalog = await self._catalog.get()
        return [m for m in catalog.models() if m.provider in self._adapters and m.kind != "image"]

    async def _with_retries(self, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(self._retries + 1):
            try:
                return await call()
            except ProviderError as exc:
                if not exc.kind.retryable or attempt == self._retries:
                    raise
                delay = exc.retry_after_s if exc.retry_after_s is not None else 0.25 * 2**attempt
                await asyncio.sleep(min(delay, _MAX_BACKOFF_S) + random.uniform(0, 0.1))  # noqa: S311  # nosec B311
        raise AssertionError("unreachable")  # pragma: no cover

    async def _transact(
        self, who: Attribution, work: Callable[[AsyncConnection], Awaitable[T]]
    ) -> T:
        """One tenant transaction, retried on transient database failures. The whole transaction
        rolls back before a retry, and every unit of work here is idempotent per hold."""
        for attempt in range(_DB_ATTEMPTS):
            try:
                async with self._db.transaction(TenantContext(who.org_id, who.user_id)) as conn:
                    return await work(conn)
            except _TRANSIENT:
                if attempt == _DB_ATTEMPTS - 1:
                    raise
                log.warning(
                    "transient database failure; retrying",
                    extra={"request_id": who.request_id, "attempt": attempt + 1},
                )
                await asyncio.sleep(0.05 * 4**attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(work)
        self._background.add(task)  # keep a strong reference until the write finishes
        task.add_done_callback(self._background.discard)
        return task

    async def _begin(
        self,
        who: Attribution,
        r: Resolved,
        kind: str,
        demand: Demand,
        idem: Idempotency | None,
        notes: Mapping[str, object] | None = None,
    ) -> _Call | bytes:
        """Spec section 6, steps 2-3: hold the worst case against every applicable budget.
        With an Idempotency-Key whose earlier attempt finished, return that attempt's stored
        result instead: nothing is reserved and nothing will be charged."""
        holds: list[str] = []

        async def work(conn: AsyncConnection) -> bytes | None:
            # Never the client-supplied request id (holds must be unique), and a fresh one per
            # attempt: a retry after a lost commit acknowledgement must not collide with the
            # hold that committed (that stray hold expires through the janitor).
            holds.append(str(uuid7()))
            if idem is not None:
                stored = await self._idempotency.claim(conn, idem)
                if stored is not None:
                    return stored
            await self._budgets.reserve(
                conn,
                hold=holds[-1],
                team_id=who.team_id,
                user_id=who.user_id,
                demand=demand,
                at=who.at,
                expires_at=who.at + HOLD_TTL,
            )
            return None

        with self._telemetry.stage("budget.reserve") as span:
            try:
                stored = await self._transact(who, work)
            except BudgetExceeded as exc:
                self._telemetry.budget_refused(span, exc)
                raise
            if stored is not None:
                self._telemetry.metrics.idempotent_replays.labels(kind).inc()
                span.set_attribute("finops.idempotent_replay", True)
                return stored
        call = _Call(who, r, kind, holds[-1], time.perf_counter(), idem, notes=dict(notes or {}))
        if conversion := file_conversion_report(call.notes.get("files"), r.entry):
            call.reports.append(conversion)
        return call

    async def precheck(self, who: Attribution, r: Resolved, req: CanonicalRequest) -> None:
        """Refuse a request its budgets cannot cover before any costly preparation (converting
        attachments, embedding the question): the worst-case hold for the request as sent is
        taken and rolled back. The real hold, with everything attached, follows."""

        async def work(conn: AsyncConnection) -> None:
            await self._budgets.reserve(
                conn,
                hold=str(uuid7()),
                team_id=who.team_id,
                user_id=who.user_id,
                demand=chat_demand(r.entry, req),
                at=who.at,
                expires_at=who.at + HOLD_TTL,
            )
            raise _RolledBack  # leaves nothing behind

        with self._telemetry.stage("budget.precheck") as span:
            try:
                await self._transact(who, work)
            except _RolledBack:
                pass
            except BudgetExceeded as exc:
                self._telemetry.budget_refused(span, exc)
                raise

    async def passages(
        self, org_id: UUID, hits: Sequence[Hit]
    ) -> tuple[list[Hit], list[dict[str, str]]]:
        """The retrieved passages the organisation's policy lets into a prompt, and the dropped
        ones with the rule that dropped them."""
        return await self._guardrails.screen_passages(org_id, hits)

    async def _end(
        self,
        call: _Call,
        usage: Usage,
        cost: int,
        status: str,
        flags: dict[str, object] | None = None,
        *,
        charged: bool,
        result: bytes | None = None,
        cache_hit: str = "none",
        counterfactual: Usage | None = None,
    ) -> None:
        """Spec section 6, steps 5-6: settle the hold (or release it when nothing was charged),
        append the ledger row and complete (or forget) the idempotency key, atomically."""
        who, r = call.who, call.r
        baseline, overhead = await self._savings(
            call, usage, charged=charged, counterfactual=counterfactual
        )
        event = UsageEvent(
            org_id=who.org_id,
            team_id=who.team_id,
            user_id=who.user_id,
            api_key_id=who.api_key_id,
            request_id=who.request_id,
            kind=call.kind,
            provider=r.entry.provider,
            model=r.entry.model,
            adapter=r.adapter.name,
            catalog_version=r.entry.catalog_version,
            usage=usage,
            cost_micro_usd=cost,
            baseline_micro_usd=baseline,
            overhead_micro_usd=overhead,
            cache_hit=cache_hit,
            route_tier=_route_tier(call.reports),
            status=status,
            latency_ms=int((time.perf_counter() - call.started) * 1000),
            flags={
                **(flags or {}),
                **({"guardrails": list(call.decisions)} if call.decisions else {}),
                **call.notes,
            },
            ts=who.at,
            hold=call.hold,
        )

        # A request that was not served saved nothing, whatever its stages did.
        served = charged or counterfactual is not None
        reports = call.reports if served else unserved(call.reports)

        async def work(conn: AsyncConnection) -> int:
            # Skipped when this hold already has its row: its stage reports went in then.
            if await self._usage.record(conn, event) and reports:
                await self._stage_reports.record_reports(
                    conn, request_id=who.request_id, ts=who.at, reports=reports
                )
            fired = await self._budgets.settle(
                conn, hold=call.hold, actual=_spent(usage, cost) if charged else None
            )
            if call.idem is not None:
                if result is not None:
                    await self._idempotency.complete(conn, call.idem, result)
                else:
                    await self._idempotency.forget(conn, call.idem)
            return fired

        with self._telemetry.stage("budget.settle"):
            fired = await self._transact(who, work)
        self._telemetry.count(
            provider=r.entry.provider,
            model=r.entry.model,
            kind=call.kind,
            status=status,
            usage=usage,
            cost=cost,
        )
        if fired:
            self._telemetry.metrics.budget_alerts.inc(fired)
            log.info(
                "budget soft threshold crossed",
                extra={"request_id": who.request_id, "alerts": fired},
            )

    async def _end_quietly(
        self,
        call: _Call,
        usage: Usage,
        cost: int,
        status: str,
        flags: dict[str, object] | None = None,
        *,
        charged: bool,
        result: bytes | None = None,
    ) -> None:
        """Failure paths: a failed write must not mask the original error. The janitor returns
        the hold once it expires, and a stale idempotency key can then be taken over."""
        try:
            await self._end(call, usage, cost, status, flags, charged=charged, result=result)
        except Exception:
            log.exception("could not settle a request", extra={"request_id": call.who.request_id})

    async def _settle(self, work: Coroutine[Any, Any, None]) -> None:
        """Run a settlement as its own task and wait for it through a shield: if this request is
        cancelled meanwhile (disconnect, shutdown), the settlement still completes."""
        await asyncio.shield(self._spawn(work))

    @asynccontextmanager
    async def _settles_on_failure(
        self, call: _Call, usage: Usage | None = None, cost: int = 0
    ) -> AsyncIterator[None]:
        """If the block fails, settle the request: release the hold or, once the provider has
        answered (`usage` given), charge what it cost. Either way the failure is recorded."""
        spent, charged = usage or Usage(), usage is not None
        try:
            yield
        except asyncio.CancelledError:
            self._spawn(self._end_quietly(call, spent, cost, "cancelled", charged=charged))
            raise
        except Exception:
            await self._settle(self._end_quietly(call, spent, cost, "error", charged=charged))
            raise

    async def _block(self, call: _Call, error: DomainError) -> NoReturn:
        """Release the hold of a request the guardrails stopped, record why, and refuse it."""
        await self._settle(self._end_quietly(call, Usage(), 0, "blocked", charged=False))
        raise error

    async def _inspect(self, call: _Call, req: CanonicalRequest) -> Inspection:
        """Spec section 4, stage 5: input guardrails, after the budget hold (stage 4)."""
        async with self._settles_on_failure(call):
            inspection = await self._guardrails.inspect_chat(
                call.who.org_id, req, request_id=call.who.request_id
            )
        call.decisions.extend(inspection.decisions)
        if inspection.blocked is not None:
            await self._block(call, inspection.blocked)
        return inspection

    async def _with_prompt_cache(self, call: _Call, req: CanonicalRequest) -> CanonicalRequest:
        """Ask the provider to cache the request's stable prefix, unless the org turned that
        off. Unreadable settings leave the request unmarked."""
        async with self._settles_on_failure(call):  # a disconnect still releases the hold
            try:
                wanted = await self._cache.provider_cache(call.who.org_id)
            except Exception:
                log.exception(
                    "cache settings unavailable; the prompt is not marked",
                    extra={"request_id": call.who.request_id},
                )
                return req
        return replace(req, prompt_cache=True) if wanted else req

    async def _cached(
        self, call: _Call, req: CanonicalRequest, policy: GuardrailPolicy
    ) -> CacheHit | None:
        """Spec section 4, step 6: an answer from the cache, exact first, then semantic, inside
        the request's org and permission scope, keyed by the policy that inspected it. A cache
        failure is a miss, never an error; a disconnect still releases the hold."""
        who = call.who
        async with self._settles_on_failure(call):
            try:
                lookup = await self._cache.prepare(
                    org_id=who.org_id,
                    team_id=who.team_id,
                    user_id=who.user_id,
                    req=req,
                    policy=policy,
                )
                hit = (
                    await self._cache.find(who.org_id, lookup, request_id=who.request_id)
                    if lookup is not None
                    else None
                )
            except Exception:
                log.exception("cache lookup failed; a miss", extra={"request_id": who.request_id})
                return None
        if lookup is not None:
            call.cache.append(lookup)
            if lookup.embedded_tokens:
                call.notes["cache"] = {"query_tokens": lookup.embedded_tokens}
        if hit is not None:
            used = hit.response.usage
            report = cache_hit_report(hit.kind, used, hit.similarity, call.r.entry)
            call.reports.append(report)
        return hit

    async def _served(self, call: _Call, hit: CacheHit, result: bytes | None) -> None:
        """Settle a request answered from the cache: nothing is charged, and its baseline is
        what the answer would have cost."""
        await self._end(
            call,
            Usage(),
            0,
            "ok",
            charged=False,
            result=result,
            cache_hit=hit.kind,
            counterfactual=hit.response.usage,
        )

    async def _remember(self, call: _Call, response: CanonicalResponse, cost: int) -> None:
        """Keep a complete, plain answer for the cache (one insert, only when the org caches);
        a failure only loses the entry, never the answer."""
        if not call.cache or not cacheable(response):
            return
        try:
            await self._cache.store(call.who.org_id, call.cache[0], response, cost)
        except Exception:
            log.exception("caching an answer failed", extra={"request_id": call.who.request_id})

    def _verify(self, call: _Call, req: CanonicalRequest, answer: CanonicalResponse) -> None:
        """US-19: when the router sampled this request, the other tier answers it too, in the
        background, and the agreement of the two answers is recorded. Past the limit of checks
        in flight, a sample is skipped."""
        report = next(
            (r for r in call.reports if r.stage == ROUTER and r.decisions.get("judge")), None
        )
        busy = self._draining or self._judging >= _MAX_JUDGES
        if report is None or self._verifier is None or busy:
            return
        self._judging += 1
        self._spawn(self._judge(self._verifier, call, req, answer, report))

    async def _judge(
        self,
        verifier: Verifier,
        call: _Call,
        req: CanonicalRequest,
        answer: CanonicalResponse,
        report: StageReport,
    ) -> None:
        of = call.who.request_id
        try:
            await self._replay(verifier, call, req, answer, report)
        except BudgetExceeded:
            log.info("quality check skipped: over budget", extra={"request_id": of})
        except Exception:
            log.exception("quality check failed", extra={"request_id": of})
        finally:
            self._judging -= 1

    async def _replay(
        self,
        verifier: Verifier,
        call: _Call,
        req: CanonicalRequest,
        answer: CanonicalResponse,
        report: StageReport,
    ) -> None:
        """The other tier's answer, charged as its own request (`<request id>.judge`) with no
        baseline: checking the router's quality is overhead of the router."""
        routed = not report.shadow  # the small tier answered; the large one is the reference
        other = (await self._catalog.get()).resolve(
            str(report.decisions["from" if routed else "to"])
        )
        if other is None:
            return
        of = call.who.request_id
        # Charged to the org: the caller did not ask for the check, so no budget of their team,
        # their own or their key pays for it.
        who = replace(
            call.who, request_id=f"{of}.judge", team_id=None, user_id=None, api_key_id=None
        )
        r = Resolved(other, call.r.adapter)  # a model of the same provider
        replay = replace(req, model=other.model)
        demand = chat_demand(other, replay)
        judged = await self._begin(who, r, "chat", demand, None, {"judge": {"of": of}})
        if isinstance(judged, bytes):  # pragma: no cover - a replay has no idempotency key
            return
        with self._telemetry.provider_call(
            "chat", other.provider, other.model, replay.max_tokens
        ) as span:
            response = await self._call_provider(judged, lambda: r.adapter.complete(replay))
            usage = _measured(replay, response)
            cost = other.cost(usage)
            self._telemetry.annotate(span, usage, cost, response.finish_reason)
        small, large = (answer, response) if routed else (response, answer)
        score: float | None = None
        embedded = 0
        try:
            score, embedded = await verifier.agreement(said(small), said(large))
        except Exception:
            log.exception("scoring a quality check failed", extra={"request_id": of})
        judged.notes["judge"] = {"of": of, "query_tokens": embedded}
        await self._settle(self._end(judged, usage, cost, "ok", charged=True))
        if score is not None:
            await verifier.record(
                who.org_id,
                request_id=of,
                candidate=str(report.decisions["to"]),
                reference=str(report.decisions["from"]),
                score=score,
            )

    async def _savings(
        self, call: _Call, usage: Usage, *, charged: bool, counterfactual: Usage | None = None
    ) -> tuple[int, int]:
        """US-17: the counterfactual cost of the request, and the overhead of the gateway's own
        work (OCR, classifier calls, embeddings)."""
        gpu, calls, embedded = overhead_inputs(call.notes, call.decisions)
        overhead = overhead_cost(
            self._overhead, gpu_seconds=gpu, classifier_calls=calls, embedded_tokens=embedded
        )
        if "judge" in call.notes or (not charged and counterfactual is None):
            return 0, overhead  # a quality check saves nothing: all of it is overhead
        used = counterfactual or usage
        if charged and (prompt_cache := provider_cache_report(call.r.entry, usage)):
            call.reports.append(prompt_cache)
        price = call.requested or call.r.entry  # the model asked for, before routing
        try:
            catalog = await self._catalog.get()
            _price_route(call.reports, catalog, used)
            name = await self._baselines.get(call.who.org_id)
            chosen = catalog.resolve(name) if name else None
        except Exception:  # the counterfactual must never stop a settlement
            log.exception("pricing the counterfactual failed; the request model is used")
            chosen = None
        if chosen is not None and chosen.kind == price.kind:
            price = chosen
        reports = [r for r in call.reports if r.stage not in CACHE_STAGES]
        return baseline_cost(price, used, reports), overhead

    async def _optimize(self, call: _Call, req: CanonicalRequest) -> tuple[_Call, CanonicalRequest]:
        """Spec section 4, steps 7-8: the optimization stages, after the input guardrails.
        The router may pick a cheaper model of the same provider: the call then carries both."""
        ctx = RequestContext(call.who.org_id, req, call.r.entry, call.who.team_id)
        async with self._settles_on_failure(call):
            call.reports.extend(await self._stages.run(ctx, "pre_llm"))
        if ctx.price is not call.r.entry:
            call = replace(call, r=Resolved(ctx.price, call.r.adapter), requested=call.r.entry)
        return call, ctx.request

    async def _call_provider(self, call: _Call, attempt: Callable[[], Awaitable[T]]) -> T:
        """Run the provider call with retries; any failure releases the hold and is recorded."""
        try:
            return await self._with_retries(attempt)
        except asyncio.CancelledError:
            self._spawn(self._end_quietly(call, Usage(), 0, "cancelled", charged=False))
            raise
        except Exception as exc:
            error = exc.kind.value if isinstance(exc, ProviderError) else "internal"
            await self._settle(
                self._end_quietly(call, Usage(), 0, "error", {"error": error}, charged=False)
            )
            raise

    async def complete(
        self,
        who: Attribution,
        r: Resolved,
        req: CanonicalRequest,
        idem: Idempotency | None = None,
        notes: Mapping[str, object] | None = None,
    ) -> ChatResult:
        call = await self._begin(who, r, "chat", chat_demand(r.entry, req), idem, notes)
        if isinstance(call, bytes):
            return ChatResult(*load_chat(call))
        inspection = await self._inspect(call, req)
        call, req = await self._optimize(call, inspection.request)
        r = call.r
        hit = await self._cached(call, req, inspection.policy)
        if hit is not None:
            stored = dump_chat(call.origin, 0, hit.response) if idem is not None else None
            await self._settle(self._served(call, hit, stored))
            return ChatResult(hit.response, 0, call.origin, cache_hit=hit.kind)
        req = await self._with_prompt_cache(call, req)
        with self._telemetry.provider_call(
            "chat", r.entry.provider, r.entry.model, req.max_tokens
        ) as span:
            response = await self._call_provider(call, lambda: r.adapter.complete(req))
            usage = _measured(req, response)
            cost = r.entry.cost(usage)
            self._telemetry.annotate(span, usage, cost, response.finish_reason)
        async with self._settles_on_failure(call, usage, cost):  # the provider has charged
            response, output = await self._guardrails.filter_response(
                inspection.policy, response, request_id=who.request_id
            )
        call.decisions.extend(output)
        stored = dump_chat(call.origin, cost, response) if idem is not None else None
        await self._settle(self._end(call, usage, cost, "ok", charged=True, result=stored))
        await self._remember(call, response, cost)  # so the next identical request hits
        self._verify(call, req, response)
        return ChatResult(response, cost, call.origin)

    async def stream(
        self,
        who: Attribution,
        r: Resolved,
        req: CanonicalRequest,
        outcome: list[StreamOutcome],
        idem: Idempotency | None = None,
        notes: Mapping[str, object] | None = None,
    ) -> StreamResult:
        """Hold the budget and open the provider stream. Budget, idempotency and provider errors
        raise here, before any byte is sent. The returned stream settles exactly once: when it
        ends or fails, or when it is closed (the API closes it however the response ends)."""
        call = await self._begin(who, r, "chat", chat_demand(r.entry, req), idem, notes)
        if isinstance(call, bytes):
            response, cost, origin = load_chat(call)
            return StreamResult(_replay_stream(response, cost, outcome), origin)
        inspection = await self._inspect(call, req)
        call, req = await self._optimize(call, inspection.request)
        r = call.r
        hit = await self._cached(call, req, inspection.policy)
        if hit is not None:
            stored = dump_chat(call.origin, 0, hit.response) if idem is not None else None
            await self._settle(self._served(call, hit, stored))
            replay = _replay_stream(hit.response, 0, outcome)
            return StreamResult(replay, call.origin, cache_hit=hit.kind)
        req = await self._with_prompt_cache(call, req)
        current: list[AsyncIterator[CanonicalChunk]] = []

        async def open_stream() -> CanonicalChunk | None:
            stream = r.adapter.stream(req)  # a finished generator cannot be retried: open anew
            current[:] = [stream]
            return await anext(stream, None)

        span = self._telemetry.stream_span(r.entry.provider, r.entry.model, req.max_tokens)
        try:
            with self._telemetry.opening(span):
                first = await self._call_provider(call, open_stream)  # retry before output only
        except BaseException:
            span.end()
            raise

        output = self._guardrails.output_filter(inspection.policy)

        def on_end(end: _StreamEnd) -> None:
            if output is not None and (decisions := output.decisions()):
                call.decisions.extend(decisions)
                self._guardrails.record_output(decisions, who.request_id)
            self._spawn(self._finish_stream(call, req, current[0], end, span))

        relay = _Relay(call, req, first, current[0], outcome, on_end, output)
        return StreamResult(relay, call.origin)

    async def _finish_stream(
        self,
        call: _Call,
        req: CanonicalRequest,
        upstream: AsyncIterator[CanonicalChunk],
        end: _StreamEnd,
        span: Span,
    ) -> None:
        """Close the upstream stream (stops provider generation on disconnect), then settle."""
        try:
            aclose = getattr(upstream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except ProviderError, RuntimeError:
                    log.debug(
                        "upstream stream already closed",
                        extra={"request_id": call.who.request_id},
                    )
            self._telemetry.annotate(
                span, end.usage, end.cost, end.finish, failed=end.status == "error"
            )
        finally:
            span.end()
        await self._end_quietly(
            call, end.usage, end.cost, end.status, charged=end.charged, result=end.stored
        )
        if end.response is not None:
            await self._remember(call, end.response, end.cost)
            self._verify(call, req, end.response)

    async def embed(
        self, who: Attribution, r: Resolved, req: EmbedRequest, idem: Idempotency | None = None
    ) -> EmbedResult:
        call = await self._begin(who, r, "embed", embed_demand(r.entry, req), idem)
        if isinstance(call, bytes):
            return EmbedResult(*load_embed(call))
        async with self._settles_on_failure(call):
            inputs, decisions, blocked = await self._guardrails.inspect_inputs(
                who.org_id, req.inputs, request_id=who.request_id
            )
        call.decisions.extend(decisions)
        if blocked is not None:
            await self._block(call, blocked)
        req = replace(req, inputs=inputs)
        with self._telemetry.provider_call("embeddings", r.entry.provider, r.entry.model) as span:
            response = await self._call_provider(call, lambda: r.adapter.embed(req))
            cost = r.entry.cost(response.usage)
            self._telemetry.annotate(span, response.usage, cost, None)
        stored = dump_embed(call.origin, cost, response) if idem is not None else None
        await self._settle(self._end(call, response.usage, cost, "ok", charged=True, result=stored))
        return EmbedResult(response, cost, call.origin)

    async def drain(self) -> None:
        """Graceful shutdown: no new quality check starts, and every write in flight is awaited,
        those started while draining included."""
        self._draining = True
        try:
            while self._background:
                await asyncio.gather(*list(self._background), return_exceptions=True)
        finally:
            self._draining = False
