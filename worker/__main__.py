"""`python -m worker`: the background jobs (the budget janitor) on a fixed cadence.

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

from gateway.adapters.postgres.database import Database
from gateway.app.logging import configure_logging
from gateway.app.settings import Settings
from worker.jobs.budget_janitor import BudgetJanitor

log = logging.getLogger("worker")
HEARTBEAT = Path(tempfile.gettempdir()) / "finops-worker.alive"


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
    next_reconcile = 0.0
    try:
        while not stop.is_set():
            due = time.monotonic() >= next_reconcile
            try:
                report = await janitor.run_once(reconcile=due)
                if due:
                    next_reconcile = time.monotonic() + settings.reconcile_interval_seconds
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
                log.exception("janitor pass failed; retrying next interval")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.janitor_interval_seconds)
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
