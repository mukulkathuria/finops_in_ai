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
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, TypeVar
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
from gateway.adapters.postgres.usage import UsageEvent, UsageRepository
from gateway.domain.budgets import BudgetExceeded, Demand, chat_demand, embed_demand
from gateway.domain.errors import NotFound, Unavailable
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
from gateway.domain.pricing import ModelKind, ModelPrice
from gateway.pipeline.idempotency import Origin, dump_chat, dump_embed, load_chat, load_embed
from gateway.ports.llm import LLMAdapter

T = TypeVar("T")
log = logging.getLogger(__name__)
_MAX_BACKOFF_S: Final = 4.0
# A hold must outlive the longest provider call; the janitor returns holds of crashed processes.
HOLD_TTL: Final = timedelta(minutes=15)
# A dropped connection, a pool or statement timeout or a deadlock: worth another attempt.
_TRANSIENT: Final = (OperationalError, InterfaceError, PoolTimeoutError)
_DB_ATTEMPTS: Final = 3


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


@dataclass(frozen=True, slots=True)
class EmbedResult:
    response: EmbedResponse
    cost: int
    origin: Origin


@dataclass(frozen=True, slots=True)
class StreamResult:
    chunks: ChunkStream
    origin: Origin


@dataclass(frozen=True, slots=True)
class _Call:
    """One request in flight, the budget hold it carries and its idempotency key, if any."""

    who: Attribution
    r: Resolved
    kind: str
    hold: str
    started: float
    idem: Idempotency | None

    @property
    def origin(self) -> Origin:
        return Origin(self.who.request_id, self.who.at)


@dataclass(frozen=True, slots=True)
class _StreamEnd:
    """What a finished stream settles with."""

    usage: Usage
    cost: int
    status: str
    charged: bool
    finish: FinishReason | None
    stored: bytes | None


def estimate_tokens(text: str) -> int:
    """Rough count (about 4 characters per token) used only when a provider reports nothing."""
    return (len(text) + 3) // 4


def _spent(usage: Usage, cost: int) -> Demand:
    return Demand(usd_micro=cost, tokens=usage.input_tokens + usage.output_tokens)


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
    ) -> None:
        self._call = call
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
        if self._started:
            try:
                self._next = await anext(self._upstream, None)
            except asyncio.CancelledError:
                self._settle("cancelled")
                raise
            except Exception:
                self._settle("error")
                raise
        self._started = True
        chunk = self._next
        if chunk is None:
            self._settle("ok")
            raise StopAsyncIteration
        self._track(chunk)
        return chunk

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
        if usage is None or usage.estimated:
            prompt = "".join(m.text() for m in self._req.messages)
            output = "".join(self._produced) + "".join(c[1] + c[2] for c in self._calls.values())
            usage = Usage(
                input_tokens=estimate_tokens(prompt),
                output_tokens=estimate_tokens(output),
                estimated=True,
            )
        # An error before any output is not charged; a cancelled stream bills its prompt.
        charged = status != "error" or bool(self._produced or self._calls)
        cost = call.r.entry.cost(usage) if charged else 0
        self._outcome.append(StreamOutcome(usage, self._finish, cost))
        stored = None
        if status == "ok" and call.idem is not None:
            assembled = CanonicalResponse(
                id="",
                model=call.r.entry.model,
                content="".join(self._produced) or None,
                tool_calls=tuple(ToolCall(*self._calls[i]) for i in sorted(self._calls)),
                finish_reason=self._finish or "stop",
                usage=usage,
            )
            stored = dump_chat(call.origin, cost, assembled)
        self._on_end(
            _StreamEnd(usage if charged else Usage(), cost, status, charged, self._finish, stored)
        )


class ChatService:
    def __init__(
        self,
        *,
        db: Database,
        catalog: CatalogCache,
        adapters: Mapping[str, LLMAdapter],
        usage: UsageRepository,
        budgets: BudgetRepository,
        idempotency: IdempotencyRepository,
        telemetry: Telemetry,
        default_max_tokens: int,
        retries: int,
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
        self.default_max_tokens = default_max_tokens
        self._retries = retries
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
        self, who: Attribution, r: Resolved, kind: str, demand: Demand, idem: Idempotency | None
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
        return _Call(who, r, kind, holds[-1], time.perf_counter(), idem)

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
    ) -> None:
        """Spec section 6, steps 5-6: settle the hold (or release it when nothing was charged),
        append the ledger row and complete (or forget) the idempotency key, atomically."""
        who, r = call.who, call.r
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
            status=status,
            latency_ms=int((time.perf_counter() - call.started) * 1000),
            flags=dict(flags or {}),
            ts=who.at,
            hold=call.hold,
        )

        async def work(conn: AsyncConnection) -> int:
            await self._usage.record(conn, event)  # skipped when this hold's row already exists
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
    ) -> ChatResult:
        call = await self._begin(who, r, "chat", chat_demand(r.entry, req), idem)
        if isinstance(call, bytes):
            return ChatResult(*load_chat(call))
        with self._telemetry.provider_call(
            "chat", r.entry.provider, r.entry.model, req.max_tokens
        ) as span:
            response = await self._call_provider(call, lambda: r.adapter.complete(req))
            usage = response.usage
            if usage.estimated:
                prompt = "".join(m.text() for m in req.messages)
                usage = Usage(
                    input_tokens=estimate_tokens(prompt),
                    output_tokens=estimate_tokens(response.content or ""),
                    estimated=True,
                )
            cost = r.entry.cost(usage)
            self._telemetry.annotate(span, usage, cost, response.finish_reason)
        stored = dump_chat(call.origin, cost, response) if idem is not None else None
        await self._settle(self._end(call, usage, cost, "ok", charged=True, result=stored))
        return ChatResult(response, cost, call.origin)

    async def stream(
        self,
        who: Attribution,
        r: Resolved,
        req: CanonicalRequest,
        outcome: list[StreamOutcome],
        idem: Idempotency | None = None,
    ) -> StreamResult:
        """Hold the budget and open the provider stream. Budget, idempotency and provider errors
        raise here, before any byte is sent. The returned stream settles exactly once: when it
        ends or fails, or when it is closed (the API closes it however the response ends)."""
        call = await self._begin(who, r, "chat", chat_demand(r.entry, req), idem)
        if isinstance(call, bytes):
            response, cost, origin = load_chat(call)
            return StreamResult(_replay_stream(response, cost, outcome), origin)
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

        def on_end(end: _StreamEnd) -> None:
            self._spawn(self._finish_stream(call, current[0], end, span))

        relay = _Relay(call, req, first, current[0], outcome, on_end)
        return StreamResult(relay, call.origin)

    async def _finish_stream(
        self,
        call: _Call,
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

    async def embed(
        self, who: Attribution, r: Resolved, req: EmbedRequest, idem: Idempotency | None = None
    ) -> EmbedResult:
        call = await self._begin(who, r, "embed", embed_demand(r.entry, req), idem)
        if isinstance(call, bytes):
            return EmbedResult(*load_embed(call))
        with self._telemetry.provider_call("embeddings", r.entry.provider, r.entry.model) as span:
            response = await self._call_provider(call, lambda: r.adapter.embed(req))
            cost = r.entry.cost(response.usage)
            self._telemetry.annotate(span, response.usage, cost, None)
        stored = dump_embed(call.origin, cost, response) if idem is not None else None
        await self._settle(self._end(call, response.usage, cost, "ok", charged=True, result=stored))
        return EmbedResult(response, cost, call.origin)

    async def drain(self) -> None:
        """Wait for in-flight ledger writes (graceful shutdown)."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
