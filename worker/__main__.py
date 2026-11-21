"""`python -m worker`: background jobs. Outbox delivery (alert webhooks) runs every
OUTBOX_INTERVAL_SECONDS; the budget janitor every JANITOR_INTERVAL_SECONDS (with a ledger
reconcile every RECONCILE_INTERVAL_SECONDS); the price sync every
PRICE_SYNC_INTERVAL_SECONDS when PRICE_SYNC_SOURCE is set; the response cache janitor every
CACHE_JANITOR_INTERVAL_SECONDS.

Runs as the `worker` database role (WORKER_DATABASE_URL). Stops cleanly on SIGTERM or SIGINT.
Each pass touches a heartbeat file that the container health check reads.
"""

import asyncio
import contextlib
import logging
import signal
import sys
import tempfile
import time
from pathlib import Path

import httpx2

from gateway.adapters.extraction.openai_compat import OpenAICompatExtractor
from gateway.adapters.postgres.database import Database
from gateway.adapters.security.envelope import Envelope, LocalKeyManager, parse_master_keys
from gateway.app.logging import configure_logging
from gateway.app.settings import Settings
from worker.jobs.budget_janitor import BudgetJanitor
from worker.jobs.cache_janitor import CacheJanitor
from worker.jobs.graph_extraction import GraphExtraction
from worker.jobs.outbox_dispatcher import OutboxDispatcher
from worker.jobs.price_sync import PriceSync

log = logging.getLogger("worker")
HEARTBEAT = Path(tempfile.gettempdir()) / "finops-worker.alive"


def _envelope(settings: Settings) -> Envelope | None:
    """The gateway's envelope keys: webhook URLs and secrets are sealed with them."""
    if settings.master_key is None:
        log.warning("MASTER_KEY is not set: outbox events cannot be delivered")
        return None
    keys = parse_master_keys(
        settings.master_key.get_secret_value(),
        settings.master_key_id,
        settings.master_previous_keys.get_secret_value(),
    )
    return Envelope(LocalKeyManager(keys, settings.master_key_id))


async def _clean_cache(janitor: CacheJanitor) -> None:
    """A failed cache cleanup is logged; the other jobs go on."""
    try:
        report = await janitor.run_once()
    except Exception:
        log.exception("cache janitor failed; retrying next interval")
        return
    if report.purged or report.evicted:
        log.info("cache janitor pass", extra={"purged": report.purged, "evicted": report.evicted})


async def _sync_prices(prices: PriceSync) -> None:
    """A broken price list is logged and leaves the catalog as it was; the other jobs go on."""
    try:
        report = await prices.run_once()
    except Exception:
        log.exception("price sync failed; the catalog is unchanged")
        return
    if report.inserted or report.refused:
        log.info(
            "price sync",
            extra={
                "inserted": report.inserted,
                "refused": report.refused,
                "unchanged": report.unchanged,
            },
        )


def _graph_extraction(
    settings: Settings, db: Database, http: httpx2.AsyncClient
) -> GraphExtraction:
    key = settings.graph_extractor_api_key
    extractor = OpenAICompatExtractor(
        settings.graph_extractor_base_url or "",
        http=http,
        model=settings.graph_extractor_model or "",
        api_key=key.get_secret_value() if key is not None else None,
        timeout_s=settings.graph_extractor_timeout_s,
    )
    return GraphExtraction(db, extractor, concurrency=settings.graph_extractor_concurrency)


async def _extract_graphs(job: GraphExtraction, stop: asyncio.Event, settings: Settings) -> None:
    """Knowledge-domain documents into their graphs (ADR 0011): one after another while any
    waits, then a look every OUTBOX_INTERVAL_SECONDS."""
    while not stop.is_set():
        try:
            report = await job.run_once()
        except Exception:
            log.exception("graph extraction failed; retrying next interval")
            report = None
        if report is not None:
            log.info(
                "graph extraction",
                extra={
                    "document_id": str(report.document_id),
                    "status": report.status,
                    "relations": report.relations,
                    "tokens": report.tokens,
                },
            )
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.outbox_interval_seconds)


async def run(settings: Settings, stop: asyncio.Event) -> None:
    if settings.worker_database_url is None:
        raise SystemExit("WORKER_DATABASE_URL is required to run the worker")
    db = Database(
        settings.worker_database_url.get_secret_value(),
        pool_size=2,
        max_overflow=0,
        statement_timeout_ms=60_000,
        application_name="finops-worker",
    )
    janitor = BudgetJanitor(db)
    cache_janitor = CacheJanitor(db, max_entries_per_org=settings.cache_max_entries_per_org)
    envelope = _envelope(settings)
    next_janitor = next_reconcile = next_price_sync = next_cache = 0.0
    try:
        async with httpx2.AsyncClient(follow_redirects=False) as http:
            dispatcher = (
                OutboxDispatcher(db, envelope, http, allow_insecure=not settings.is_production_like)
                if envelope is not None
                else None
            )
            prices = (
                PriceSync(
                    db,
                    settings.price_sync_source,
                    http=http,
                    allow_insecure=not settings.is_production_like,
                )
                if settings.price_sync_source
                else None
            )
            extraction = (
                asyncio.create_task(
                    _extract_graphs(_graph_extraction(settings, db, http), stop, settings)
                )
                if settings.graph_extraction_enabled
                else None
            )
            while not stop.is_set():
                try:
                    if dispatcher is not None:
                        sent = await dispatcher.run_once()
                        if sent.delivered or sent.retried or sent.failed:
                            log.info(
                                "outbox pass",
                                extra={
                                    "delivered": sent.delivered,
                                    "retried": sent.retried,
                                    "failed": sent.failed,
                                },
                            )
                    now = time.monotonic()
                    if now >= next_janitor:
                        due = now >= next_reconcile
                        report = await janitor.run_once(reconcile=due)
                        next_janitor = now + settings.janitor_interval_seconds
                        if due:
                            next_reconcile = now + settings.reconcile_interval_seconds
                        if report.expired or report.reconciled or report.purged:
                            log.info(
                                "janitor pass",
                                extra={
                                    "expired": report.expired,
                                    "reconciled": report.reconciled,
                                    "purged": report.purged,
                                },
                            )
                    if prices is not None and now >= next_price_sync:
                        next_price_sync = now + settings.price_sync_interval_seconds
                        await _sync_prices(prices)
                    if now >= next_cache:
                        next_cache = now + settings.cache_janitor_interval_seconds
                        await _clean_cache(cache_janitor)
                    HEARTBEAT.touch()
                except Exception:
                    log.exception("worker pass failed; retrying next interval")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), settings.outbox_interval_seconds)
            if extraction is not None:  # a document cut short is taken again when its lease ends
                extraction.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await extraction
    finally:
        await db.dispose()


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # no signal handlers on Windows loops
            loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, "finops-worker")
    # psycopg's async driver needs the selector loop on Windows.
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(_main(settings), loop_factory=factory)


if __name__ == "__main__":
    main()
