"""Provider prompt caching (Phase 3): requests ask the provider to cache their stable prefix
unless the org turned that off."""

from typing import Any

import httpx2

from gateway.domain.llm import CanonicalRequest, CanonicalResponse
from tests.integration.test_us14_files import _team_key
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase


class _Spy:
    """The mock provider's adapter, noting whether each request was marked for its cache."""

    def __init__(self, inner: Any, seen: list[bool]) -> None:
        self._inner = inner
        self._seen = seen

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def complete(self, req: CanonicalRequest) -> CanonicalResponse:
        self._seen.append(req.prompt_cache)
        response: CanonicalResponse = await self._inner.complete(req)
        return response


async def _ask(client: httpx2.AsyncClient, key: str) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={"model": "mock/mock-small", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200, resp.text


async def test_requests_are_marked_for_the_provider_cache_unless_the_org_opts_out(
    test_db: DisposableDatabase,
) -> None:
    seen: list[bool] = []
    async with app_client(app_settings(test_db)) as (app, client):
        adapters = app.state.container.chat_service._adapters
        adapters["mock"] = _Spy(adapters["mock"], seen)
        admin = await signup(client)
        auth = bearer(admin["access_token"])
        key = await _team_key(client, admin)
        settings = await client.get("/v1/admin/cache-settings", headers=auth)
        assert settings.json()["provider_cache"] is True
        await _ask(client, key)
        off = await client.put(
            "/v1/admin/cache-settings", json={"provider_cache": False}, headers=auth
        )
        assert off.status_code == 200, off.text
        assert off.json() == {**settings.json(), "provider_cache": False}
        await _ask(client, key)
    assert seen == [True, False]
