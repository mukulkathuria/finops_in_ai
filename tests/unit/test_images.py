"""US-24: image options and prices come from the catalog, a request reserves its images and
their price, moderation refuses disallowed prompts (by rules, or through a moderation API that
fails closed), and a price list carries image prices."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

import httpx2
import orjson
import pytest

from gateway.adapters.moderation.openai_compat import OpenAICompatModerator
from gateway.adapters.moderation.rules import RulesModerator
from gateway.domain.errors import InvalidInput
from gateway.domain.images import ImageOption, ModerationUnavailable, image_demand, image_option
from gateway.domain.llm import ImageResponse, ProviderError, ProviderErrorKind
from gateway.domain.pricing import ModelPrice
from gateway.pipeline.cache import MAX_CACHED_IMAGE_BYTES, ResponseCache
from gateway.pipeline.chat import ChatService
from worker.jobs.price_sync import parse_price_list

TABLE = (("1024x1024/hd", 80_000), ("1024x1024/standard", 40_000))
PRICED = ModelPrice(1, "mock", "mock-image", "image", 0, 0, price_image=40_000, image_prices=TABLE)


def test_options_and_prices_come_from_the_catalog() -> None:
    assert image_option(PRICED, None, None) == ImageOption("1024x1024", "standard", 40_000)
    assert image_option(PRICED, "1024x1024", "hd").price == 80_000
    with pytest.raises(InvalidInput, match="1024x1024/hd") as refused:
        image_option(PRICED, "512x512", None)
    assert (refused.value.code, refused.value.param) == ("unsupported_image_option", "size")
    with pytest.raises(InvalidInput, match="no image price"):
        image_option(ModelPrice(1, "p", "free", "image", 0, 0), None, None)
    demand = image_demand(image_option(PRICED, None, "hd"), 3)
    assert (demand.usd_micro, demand.images, demand.tokens) == (240_000, 3, 0)


def test_what_is_rendered_is_what_is_priced() -> None:
    """Phase 5 review S-B1, C-I1: no request gets a bigger or better image than it pays for."""
    tiers = (
        ("1024x1024/high", 167_000),
        ("1024x1024/low", 11_000),
        ("1024x1024/medium", 42_000),
        ("1536x1024/low", 16_000),
    )
    tiered = replace(PRICED, image_prices=tiers)
    # An option left out is the cheapest offered with the ones named, and is asked for by name.
    assert image_option(tiered, None, None) == ImageOption("1024x1024", "low", 11_000)
    assert image_option(tiered, "1536x1024", None) == ImageOption("1536x1024", "low", 16_000)
    assert image_option(tiered, None, "high") == ImageOption("1024x1024", "high", 167_000)
    with pytest.raises(InvalidInput) as unknown:
        image_option(tiered, None, "hd")
    assert unknown.value.param == "quality"
    # Priced per image without a table: the default image, left to the provider or named.
    flat = ModelPrice(1, "p", "m", "image", 0, 0, price_image=10_000)
    assert image_option(flat, None, None) == ImageOption(None, None, 10_000)
    assert image_option(flat, "1024x1024", "standard").price == 10_000
    for size, quality, param in (
        ("4096x4096", None, "size"),
        (None, "hd", "quality"),
        ("auto", None, "size"),
    ):
        with pytest.raises(InvalidInput, match="default image only") as bigger:
            image_option(flat, size, quality)
        assert (bigger.value.code, bigger.value.param) == ("unsupported_image_option", param)


async def test_rules_refuse_what_image_providers_refuse() -> None:
    rules = RulesModerator()
    assert not (await rules.check("A watercolour of a lighthouse at dusk")).flagged
    gore = await rules.check("A battlefield full of gore and dismembered bodies")
    assert (gore.flagged, gore.categories) == (True, ("violence/graphic",))
    assert (await rules.check("a naked child")).categories == ("sexual/minors",)
    assert (await rules.check("an explicit nude portrait")).categories == ("sexual",)
    assert not (await rules.check("child " * 2_000)).flagged  # one term alone is not a match


async def test_rules_see_through_spelling_tricks() -> None:
    """Phase 5 review S-F1: the same words, however they are spelled."""
    rules = RulesModerator()
    fullwidth = "".join(chr(ord(c) - ord("a") + 0xFF41) for c in "nude")
    for word in (
        "nu" + chr(0x200B) + "de",  # a zero-width space inside
        "nud" + chr(0x0435),  # a Cyrillic e
        fullwidth,
        "n u d e",
        "n.u.d.e",
        "nud3",
    ):
        assert (await rules.check(f"a {word} portrait")).categories == ("sexual",), word
    assert (await rules.check("a n4ked ch1ld")).categories == ("sexual/minors",)
    assert (await rules.check("g0r3 everywhere")).categories == ("violence/graphic",)
    for clean in ("A 1024x1024 watercolour of a lighthouse, 4k", "Plan A B C for the team"):
        assert not (await rules.check(clean)).flagged, clean


async def test_a_render_is_retried_only_when_the_provider_refused_to_start_it() -> None:
    """Phase 5 review C-I3: after a timeout the images may have been made and billed."""
    chat = _bare_chat(retries=2)
    calls: list[ProviderErrorKind] = []

    def failing(kind: ProviderErrorKind) -> Callable[[], Awaitable[None]]:
        async def call() -> None:
            calls.append(kind)
            raise ProviderError(kind, "no", provider="p", retry_after_s=0)

        return call

    for kind, idempotent, tries in (
        (ProviderErrorKind.TIMEOUT, False, 1),
        (ProviderErrorKind.UNAVAILABLE, False, 1),
        (ProviderErrorKind.RATE_LIMIT, False, 3),  # refused before any work: safe again
        (ProviderErrorKind.TIMEOUT, True, 3),  # a chat completion is retried as before
    ):
        calls.clear()
        with pytest.raises(ProviderError):
            await chat._with_retries(failing(kind), idempotent=idempotent)
        assert len(calls) == tries, (kind, idempotent)


async def test_an_oversized_image_answer_is_served_but_not_cached() -> None:
    """Phase 5 review S-G1: the janitor caps an org's entries, and this caps their size."""
    unused: Any = object()  # touching any of them raises AttributeError
    cache = ResponseCache(db=unused, repo=unused, settings=unused, envelope=unused, embedder=None)
    big = ImageResponse("m", ("A" * (MAX_CACHED_IMAGE_BYTES + 1),))
    await cache.store_images(uuid4(), unused, big, 0)  # returns before keeping anything
    with pytest.raises(AttributeError):  # a small answer goes on to be sealed and stored
        await cache.store_images(uuid4(), unused, ImageResponse("m", ("AAAA",)), 0)


def _bare_chat(*, retries: int) -> ChatService:
    unused: dict[str, Any] = dict.fromkeys(
        (
            "db",
            "guardrails",
            "catalog",
            "usage",
            "budgets",
            "idempotency",
            "telemetry",
            "stages",
            "stage_reports",
            "overhead",
            "baselines",
            "cache",
        )
    )
    return ChatService(
        adapters={}, default_max_tokens=1, retries=retries, verifier=object(), **unused
    )  # type: ignore[arg-type]


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
