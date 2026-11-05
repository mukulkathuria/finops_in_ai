"""US-24: image options and prices come from the catalog, a request reserves its images and
their price, moderation refuses disallowed prompts (by rules, or through a moderation API that
fails closed), and a price list carries image prices."""

from typing import Any

import httpx2
import orjson
import pytest

from gateway.adapters.moderation.openai_compat import OpenAICompatModerator
from gateway.adapters.moderation.rules import RulesModerator
from gateway.domain.errors import InvalidInput
from gateway.domain.images import ModerationUnavailable, image_demand, image_option
from gateway.domain.pricing import ModelPrice
from worker.jobs.price_sync import parse_price_list

TABLE = (("1024x1024/hd", 80_000), ("1024x1024/standard", 40_000))
PRICED = ModelPrice(1, "mock", "mock-image", "image", 0, 0, price_image=40_000, image_prices=TABLE)


def test_options_and_prices_come_from_the_catalog() -> None:
    standard = image_option(PRICED, None, None)
    assert (standard.size, standard.quality, standard.price) == ("1024x1024", "standard", 40_000)
    assert image_option(PRICED, "1024x1024", "hd").price == 80_000
    with pytest.raises(InvalidInput, match="1024x1024/hd") as refused:
        image_option(PRICED, "512x512", None)
    assert refused.value.code == "unsupported_image_option"
    flat = ModelPrice(1, "p", "m", "image", 0, 0, price_image=10_000)
    assert image_option(flat, "256x256", "low").price == 10_000  # no table: one price for all
    with pytest.raises(InvalidInput, match="no image price"):
        image_option(ModelPrice(1, "p", "free", "image", 0, 0), None, None)
    demand = image_demand(image_option(PRICED, None, "hd"), 3)
    assert (demand.usd_micro, demand.images, demand.tokens) == (240_000, 3, 0)


async def test_rules_refuse_what_image_providers_refuse() -> None:
    rules = RulesModerator()
    assert not (await rules.check("A watercolour of a lighthouse at dusk")).flagged
    gore = await rules.check("A battlefield full of gore and dismembered bodies")
    assert (gore.flagged, gore.categories) == (True, ("violence/graphic",))
    assert (await rules.check("a naked child")).categories == ("sexual/minors",)
    assert (await rules.check("an explicit nude portrait")).categories == ("sexual",)
    assert not (await rules.check("child " * 2_000)).flagged  # one term alone is not a match


def _moderator(
    reply: httpx2.Response | Exception,
) -> tuple[OpenAICompatModerator, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "url": str(request.url),
                "body": orjson.loads(request.content),
                "auth": request.headers.get("authorization"),
            }
        )
        if isinstance(reply, Exception):
            raise reply
        return reply

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    moderator = OpenAICompatModerator(
        "https://mod.example.test/v1",
        http=http,
        model="omni-moderation-latest",
        api_key="k",
        timeout_s=5,
    )
    return moderator, seen


async def test_a_moderation_api_decides_and_fails_closed() -> None:
    flagged = {"results": [{"flagged": True, "categories": {"violence": True, "hate": False}}]}
    moderator, seen = _moderator(httpx2.Response(200, json=flagged))
    verdict = await moderator.check("a prompt")
    assert (verdict.flagged, verdict.categories) == (True, ("violence",))
    assert seen[0]["body"] == {"model": "omni-moderation-latest", "input": "a prompt"}
    assert seen[0]["url"] == "https://mod.example.test/v1/moderations"
    assert seen[0]["auth"] == "Bearer k"
    clean = {"results": [{"flagged": False, "categories": {"violence": False}}]}
    assert not (await _moderator(httpx2.Response(200, json=clean))[0].check("x")).flagged
    for broken in (
        httpx2.Response(500, json={}),
        httpx2.Response(200, content=b"not json"),
        httpx2.Response(200, json={"results": []}),
        httpx2.ConnectError("down"),
    ):
        with pytest.raises(ModerationUnavailable):
            await _moderator(broken)[0].check("x")


def test_a_price_list_carries_image_prices() -> None:
    model: dict[str, Any] = {
        "provider": "openai",
        "model": "dall-e-3",
        "kind": "image",
        "price_input": 0,
        "price_output": 0,
        "price_image": 40_000,
        "image_prices": {"1024x1024/standard": 40_000, "1024x1792/hd": 120_000},
        "context_window": 0,
        "max_output_tokens": 0,
    }
    (entry,) = parse_price_list(orjson.dumps({"models": [model]}))
    assert entry.price.image_prices == (("1024x1024/standard", 40_000), ("1024x1792/hd", 120_000))
    model["image_prices"] = {"huge": 1}
    with pytest.raises(ValueError, match="invalid price list"):
        parse_price_list(orjson.dumps({"models": [model]}))
