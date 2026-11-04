"""Price sync (Phase 3): the worker appends catalog rows for changed prices, at once or at a later
date; suspicious changes are refused; a run with the same list changes nothing; one sync runs at
a time."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import orjson

from gateway.adapters.postgres.catalog import CatalogRepository
from gateway.adapters.postgres.database import Database
from worker.jobs.price_sync import PriceSync, SyncReport

SHIPPED = Path(__file__).parents[2] / "config" / "prices.json"


def _model(provider: str, name: str, price_input: int) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": name,
        "kind": "chat",
        "price_input": price_input,
        "price_output": 4 * price_input,
        "context_window": 8_192,
        "max_output_tokens": 1_024,
    }


async def test_the_shipped_list_matches_the_seeded_catalog(
    worker_db: Database, tmp_path: Path
) -> None:
    count = len(orjson.loads(SHIPPED.read_bytes())["models"])
    async with httpx2.AsyncClient() as http:
        report = await PriceSync(worker_db, str(SHIPPED), http=http).run_once()
    assert report == SyncReport(0, 0, count)


async def test_the_catalog_follows_the_price_list(worker_db: Database, tmp_path: Path) -> None:
    provider = f"sync{uuid4().hex[:8]}"  # its own models: other tests' prices stay as seeded
    listed = tmp_path / "prices.json"
    a, b = _model(provider, "a", 1_000_000), _model(provider, "b", 2_000_000)
    listed.write_bytes(orjson.dumps({"models": [a, b]}))
    async with httpx2.AsyncClient() as http:
        sync = PriceSync(worker_db, str(listed), http=http)
        assert await sync.run_once() == SyncReport(2, 0, 0)  # new models
        assert await sync.run_once() == SyncReport(0, 0, 2)  # in step

        later = {**a, "price_input": 1_500_000, "effective_from": "2099-01-01T00:00:00Z"}
        changed = [{**a, "price_input": 1_200_000}, {**b, "price_input": 0}, later]
        listed.write_bytes(orjson.dumps({"models": changed}))
        assert await sync.run_once() == SyncReport(2, 1, 0)  # b's drop to zero is refused
        assert await sync.run_once() == SyncReport(0, 1, 1)  # a is in step; b still refused

        async with worker_db.transaction() as conn:
            assert await CatalogRepository().lock_sync(conn)
            assert (await sync.run_once()).skipped  # another worker is syncing

    async with worker_db.transaction() as conn:
        current = {
            p.model: p for p in await CatalogRepository().current(conn) if p.provider == provider
        }
    assert current["a"].price_input == 1_200_000  # the 2099 price is not in effect yet
    assert current["b"].price_input == 2_000_000  # the refused change left it alone
    assert current["a"].catalog_version > current["b"].catalog_version  # a newer row
