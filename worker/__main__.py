"""`python -m worker`: background jobs. Outbox delivery (alert webhooks) runs every
OUTBOX_INTERVAL_SECONDS; the budget janitor every JANITOR_INTERVAL_SECONDS (with a ledger
reconcile every RECONCILE_INTERVAL_SECONDS).

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

from gateway.adapters.postgres.database import Database
from gateway.adapters.security.envelope import Envelope, LocalKeyManager, parse_master_keys
from gateway.app.logging import configure_logging
from gateway.app.settings import Settings
from worker.jobs.budget_janitor import BudgetJanitor
from worker.jobs.outbox_dispatcher import OutboxDispatcher

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
    envelope = _envelope(settings)
    next_janitor = next_reconcile = 0.0
    try:
        async with httpx2.AsyncClient(follow_redirects=False) as http:
            dispatcher = (
                OutboxDispatcher(db, envelope, http, allow_insecure=not settings.is_production_like)
                if envelope is not None
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
                    HEARTBEAT.touch()
                except Exception:
                    log.exception("worker pass failed; retrying next interval")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), settings.outbox_interval_seconds)
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
