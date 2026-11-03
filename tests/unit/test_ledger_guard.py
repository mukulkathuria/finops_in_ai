"""The ledger insert deduplicates per hold on the request time; a hold without one is a bug."""

from uuid import uuid4

import pytest

from gateway.adapters.postgres.usage import UsageEvent, UsageRepository
from gateway.domain.llm import Usage


async def test_a_ledger_row_with_a_hold_needs_its_request_time() -> None:
    event = UsageEvent(
        org_id=uuid4(),
        team_id=None,
        user_id=None,
        api_key_id=None,
        request_id="r",
        kind="chat",
        provider="p",
        model="m",
        adapter="a",
        catalog_version=1,
        usage=Usage(),
        cost_micro_usd=0,
        status="ok",
        latency_ms=0,
        hold="h",
    )
    with pytest.raises(ValueError, match="request time"):
        await UsageRepository().record(None, event)  # type: ignore[arg-type]
