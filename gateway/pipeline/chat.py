"""Chat and embeddings use cases (US-05): resolve the model, call the provider with retries,
price the usage from the catalog and append one ledger row per request, success or failure.
Budgets (US-07) and the optimisation stages wrap around these calls."""

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from gateway.adapters.postgres.catalog import CatalogCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.usage import UsageEvent, UsageRepository
from gateway.domain.errors import NotFound, Unavailable
from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    CanonicalResponse,
    EmbedRequest,
    EmbedResponse,
    FinishReason,
    ProviderError,
    Usage,
)
from gateway.domain.pricing import ModelKind, ModelPrice
from gateway.ports.llm import LLMAdapter

T = TypeVar("T")
log = logging.getLogger(__name__)
_MAX_BACKOFF_S = 4.0


@dataclass(frozen=True, slots=True)
class Attribution:
    org_id: UUID
    team_id: UUID | None
    user_id: UUID | None
    api_key_id: UUID | None
    request_id: str


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


def estimate_tokens(text: str) -> int:
    """Rough count (about 4 characters per token) used only when a provider reports nothing."""
    return (len(text) + 3) // 4


class ChatService:
    def __init__(
        self,
        *,
        db: Database,
        catalog: CatalogCache,
        adapters: Mapping[str, LLMAdapter],
        usage: UsageRepository,
        default_max_tokens: int,
        retries: int,
    ) -> None:
        self._db = db
        self._catalog = catalog
        self._adapters = adapters
        self._usage = usage
        self.default_max_tokens = default_max_tokens
        self._retries = retries
        self._background: set[asyncio.Task[None]] = set()

    async def resolve(self, model: str, kind: ModelKind) -> Resolved:
        entry = (await self._catalog.get()).resolve(model)
        if entry is None or entry.kind != kind:
            raise NotFound(
                f"model '{model}' does not exist or is not available",
                code="model_not_found",
                param="model",
            )
        adapter = self._adapters.get(entry.provider)
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

    async def _record(
        self,
        who: Attribution,
        r: Resolved,
        kind: str,
        usage: Usage,
        cost: int,
        status: str,
        started: float,
        flags: dict[str, object] | None = None,
    ) -> None:
        event = UsageEvent(
            org_id=who.org_id,
            team_id=who.team_id,
            user_id=who.user_id,
            api_key_id=who.api_key_id,
            request_id=who.request_id,
            kind=kind,
            provider=r.entry.provider,
            model=r.entry.model,
            adapter=r.adapter.name,
            catalog_version=r.entry.catalog_version,
            usage=usage,
            cost_micro_usd=cost,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            flags=dict(flags or {}),
        )
        async with self._db.transaction(TenantContext(who.org_id, who.user_id)) as conn:
            await self._usage.record(conn, event)

    async def complete(
        self, who: Attribution, r: Resolved, req: CanonicalRequest
    ) -> tuple[CanonicalResponse, int]:
        started = time.perf_counter()
        try:
            response = await self._with_retries(lambda: r.adapter.complete(req))
        except ProviderError as exc:
            await self._record(
                who, r, "chat", Usage(), 0, "error", started, {"error": exc.kind.value}
            )
            raise
        usage = response.usage
        if usage.estimated:
            prompt = "".join(m.text() for m in req.messages)
            usage = Usage(
                input_tokens=estimate_tokens(prompt),
                output_tokens=estimate_tokens(response.content or ""),
                estimated=True,
            )
        cost = r.entry.cost(usage)
        await self._record(who, r, "chat", usage, cost, "ok", started)
        return response, cost

    async def stream(
        self, who: Attribution, r: Resolved, req: CanonicalRequest, outcome: list[StreamOutcome]
    ) -> AsyncIterator[CanonicalChunk]:
        """Yield provider chunks; always record the ledger row, even when the client disconnects."""
        started = time.perf_counter()
        usage: Usage | None = None
        finish: FinishReason | None = None
        produced: list[str] = []
        status = "cancelled"
        current: list[AsyncIterator[CanonicalChunk]] = []

        async def open_stream() -> CanonicalChunk | None:
            stream = r.adapter.stream(req)  # a finished generator cannot be retried: open anew
            current[:] = [stream]
            return await anext(stream, None)

        try:
            pending = await self._with_retries(open_stream)  # retry only before any output
            while pending is not None:
                if pending.usage is not None:
                    usage = pending.usage
                finish = pending.finish_reason or finish
                if pending.content:
                    produced.append(pending.content)
                yield pending
                pending = await anext(current[0], None)
            status = "ok"
        except ProviderError:
            status = "error"
            raise
        finally:
            if usage is None or usage.estimated:
                prompt = "".join(m.text() for m in req.messages)
                usage = Usage(
                    input_tokens=estimate_tokens(prompt),
                    output_tokens=estimate_tokens("".join(produced)),
                    estimated=True,
                )
            cost = r.entry.cost(usage) if status != "error" or produced else 0
            outcome.append(StreamOutcome(usage=usage, finish_reason=finish, cost_micro_usd=cost))
            task = asyncio.create_task(
                self._finish_stream(current, who, r, usage, cost, status, started)
            )
            self._background.add(task)  # keep a strong reference until the write finishes
            task.add_done_callback(self._background.discard)

    async def _finish_stream(
        self,
        current: list[AsyncIterator[CanonicalChunk]],
        who: Attribution,
        r: Resolved,
        usage: Usage,
        cost: int,
        status: str,
        started: float,
    ) -> None:
        """Close the upstream stream (stops provider generation on disconnect), then bill."""
        for stream in current:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except ProviderError, RuntimeError:
                    log.debug(
                        "upstream stream already closed", extra={"request_id": who.request_id}
                    )
        try:
            await self._record(who, r, "chat", usage, cost, status, started)
        except Exception:
            log.exception(
                "could not record a streamed request", extra={"request_id": who.request_id}
            )

    async def embed(
        self, who: Attribution, r: Resolved, req: EmbedRequest
    ) -> tuple[EmbedResponse, int]:
        started = time.perf_counter()
        try:
            response = await self._with_retries(lambda: r.adapter.embed(req))
        except ProviderError as exc:
            await self._record(
                who, r, "embed", Usage(), 0, "error", started, {"error": exc.kind.value}
            )
            raise
        cost = r.entry.cost(response.usage)
        await self._record(who, r, "embed", response.usage, cost, "ok", started)
        return response, cost

    async def drain(self) -> None:
        """Wait for in-flight ledger writes (graceful shutdown)."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
