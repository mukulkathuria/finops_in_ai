"""The price sync job (spec section 3, "Model and price registry"; Phase 3).

The catalog follows a price list: a JSON file (`config/prices.json`) or an https URL, set by
PRICE_SYNC_SOURCE. For each model the entry in effect now is compared with the catalog; a change
becomes a new row in effect at once (the gateway picks it up within a minute, and every request
records the catalog version it was priced at), and an entry dated later is added for its time.
Suspicious changes are refused and logged (`gateway.domain.price_list`). The whole list is
rejected when any entry is invalid, and one sync runs at a time across workers.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

import httpx2
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from gateway.adapters.postgres.catalog import CatalogRepository
from gateway.adapters.postgres.database import Database
from gateway.domain.price_list import PriceEntry, plan_sync
from gateway.domain.pricing import ModelPrice

log = logging.getLogger(__name__)
MAX_BYTES: Final = 5 * 1024 * 1024
_FETCH_TIMEOUT_S: Final = 30.0

Price = Annotated[int, Field(ge=0, le=10**12)]  # micro-USD per 1M tokens (images: each)
Count = Annotated[int, Field(ge=0, le=2**31 - 1)]


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    model: str = Field(min_length=1, max_length=200)
    kind: Literal["chat", "embedding", "image"]
    price_input: Price
    price_output: Price
    price_cached_input: Price | None = None
    price_cache_write: Price | None = None
    price_image: Price | None = None
    tier_threshold_tokens: Annotated[int, Field(gt=0, le=2**31 - 1)] | None = None
    tier_price_input: Price | None = None
    tier_price_output: Price | None = None
    context_window: Count
    max_output_tokens: Count
    effective_from: AwareDatetime | None = None

    @model_validator(mode="after")
    def _chat_limits(self) -> Self:
        # The router reads 0 as unknown: a chat model must say what it can take.
        if self.kind == "chat" and not (self.context_window and self.max_output_tokens):
            raise ValueError("a chat model needs context_window and max_output_tokens")
        return self


class _PriceList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str | None = Field(default=None, max_length=10_000)
    models: list[_Entry] = Field(max_length=5_000)


def parse_price_list(raw: bytes) -> list[PriceEntry]:
    """The entries of a price list. One invalid entry rejects the whole list (ValueError)."""
    try:
        listed = _PriceList.model_validate_json(raw)
    except ValidationError as exc:
        where = "; ".join(
            ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
            for error in exc.errors()[:5]
        )
        raise ValueError(f"invalid price list ({exc.error_count()} errors): {where}") from exc
    entries: list[PriceEntry] = []
    seen: set[tuple[str, str, datetime | None]] = set()
    for e in listed.models:
        key = (e.provider, e.model, e.effective_from)
        if key in seen:
            raise ValueError(f"invalid price list: {e.provider}/{e.model} is listed twice")
        seen.add(key)
        price = ModelPrice(catalog_version=0, **e.model_dump(exclude={"effective_from"}))
        entries.append(PriceEntry(price, e.effective_from))
    return entries


def source_label(source: str) -> str:
    """Where catalog rows came from, as stored with them: a URL without its credentials or query
    (either may carry a token), or a file's name."""
    parts = urlsplit(source)
    if parts.scheme in ("http", "https"):
        return f"sync:{parts.scheme}://{parts.hostname or ''}{parts.path}"[:200]
    return f"sync:{Path(source).name}"[:200]


def _read(path: Path) -> bytes:
    with path.open("rb") as f:
        body = f.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("the price list is larger than 5 MiB")
    return body


@dataclass(frozen=True, slots=True)
class SyncReport:
    inserted: int
    refused: int
    unchanged: int
    skipped: bool = False  # another worker was syncing


class PriceSync:
    def __init__(
        self,
        db: Database,
        source: str,
        *,
        http: httpx2.AsyncClient,
        allow_insecure: bool = False,
    ) -> None:
        self._db = db
        self._source = source
        self._http = http
        self._allow_insecure = allow_insecure
        self._label = source_label(source)
        self._repo = CatalogRepository()

    async def _load(self) -> bytes:
        scheme = urlsplit(self._source).scheme
        if scheme not in ("http", "https"):
            return await asyncio.to_thread(_read, Path(self._source))
        if scheme == "http" and not self._allow_insecure:
            raise ValueError("PRICE_SYNC_SOURCE must be an https URL")
        body = bytearray()
        try:
            async with self._http.stream("GET", self._source, timeout=_FETCH_TIMEOUT_S) as resp:
                if resp.status_code != 200:  # redirects are not followed either
                    raise ValueError(f"fetching the price list failed: HTTP {resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ValueError("the price list is larger than 5 MiB")
        except httpx2.HTTPError as exc:  # its message names the URL, which may hold a token
            raise ValueError(f"fetching the price list failed: {type(exc).__name__}") from None
        return bytes(body)

    async def run_once(self) -> SyncReport:
        entries = parse_price_list(await self._load())  # read before any connection is taken
        async with self._db.transaction() as conn:
            if not await self._repo.lock_sync(conn):
                return SyncReport(0, 0, 0, skipped=True)
            current = {(p.provider, p.model): p for p in await self._repo.current(conn)}
            plan = plan_sync(entries, current, datetime.now(UTC))
            inserted = 0
            for entry in plan.inserts:
                if await self._repo.insert(conn, entry, source=self._label):
                    inserted += 1
        for entry, reason in plan.refused:
            log.warning(
                "price change refused",
                extra={
                    "provider": entry.price.provider,
                    "model": entry.price.model,
                    "reason": reason,
                    "source": self._label,
                },
            )
        return SyncReport(inserted, len(plan.refused), plan.unchanged)
