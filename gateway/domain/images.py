"""Image generation (spec section 4, F7; US-24): what a request may ask for, what one image costs
and what a request reserves. Sizes, qualities and their prices are catalog data: a model's
`image_prices` names each "WxH/quality" it offers with its price per image; a model without the
table takes the request's options and charges `price_image` for each image."""

from dataclasses import dataclass
from typing import Final

from gateway.domain.budgets import Demand
from gateway.domain.errors import DomainError, InvalidInput
from gateway.domain.pricing import ModelPrice

MAX_PROMPT_CHARS: Final = 4_000
MAX_IMAGES: Final = 4  # in one request
DEFAULT_SIZE: Final = "1024x1024"
DEFAULT_QUALITY: Final = "standard"


class ModerationUnavailable(DomainError):
    """No moderation verdict could be had: image requests fail closed."""

    kind = "unavailable"
    default_code = "moderation_unavailable"


@dataclass(frozen=True, slots=True)
class Moderation:
    """A moderation verdict: whether the text is refused, and under which categories."""

    flagged: bool
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageOption:
    """What the provider is asked to render, and what each image costs. None leaves the option
    to the provider: only a model priced for its default image does."""

    size: str | None
    quality: str | None
    price: int  # micro-USD per image


def _offers(key: str, size: str | None, quality: str | None) -> bool:
    offered_size, _, offered_quality = key.partition("/")
    return size in (None, offered_size) and quality in (None, offered_quality)


def image_option(entry: ModelPrice, size: str | None, quality: str | None) -> ImageOption:
    """The size, quality and per-image price of a request: what is rendered is what is priced.

    - A model with an `image_prices` table offers exactly those options. An option the request
      leaves out is the default (DEFAULT_SIZE, DEFAULT_QUALITY) when the table offers it with
      the ones named, else the cheapest it offers with them; the provider is asked for it by
      name, so the image made is the image priced.
    - A model priced per image without a table is priced for its default image: a request may
      leave size and quality to it or name the defaults, nothing bigger or better.
    - A model with neither is refused: an unpriced image would escape the money budgets."""
    if entry.image_prices:
        prices = dict(entry.image_prices)
        usual = f"{size or DEFAULT_SIZE}/{quality or DEFAULT_QUALITY}"
        if usual in prices:
            usual_size, _, usual_quality = usual.partition("/")
            return ImageOption(usual_size, usual_quality, prices[usual])
        matching = [
            (price, key) for key, price in entry.image_prices if _offers(key, size, quality)
        ]
        if not matching:
            sizes = {key.partition("/")[0] for key, _ in entry.image_prices}
            offered = ", ".join(sorted(key for key, _ in entry.image_prices))
            raise InvalidInput(
                f"{entry.public_name} offers these size/quality options: {offered}",
                code="unsupported_image_option",
                param="size" if size is not None and size not in sizes else "quality",
            )
        price, key = min(matching)
        chosen_size, _, chosen_quality = key.partition("/")
        return ImageOption(chosen_size, chosen_quality, price)
    if entry.price_image is None:
        raise InvalidInput(
            f"{entry.public_name} has no image price in the catalog", code="model_not_priced"
        )
    if size not in (None, DEFAULT_SIZE) or quality not in (None, DEFAULT_QUALITY):
        raise InvalidInput(
            f"{entry.public_name} is priced for its default image only: leave size and quality"
            f" out, or ask for {DEFAULT_SIZE} at {DEFAULT_QUALITY} quality",
            code="unsupported_image_option",
            param="size" if size not in (None, DEFAULT_SIZE) else "quality",
        )
    return ImageOption(size, quality, entry.price_image)


def image_demand(option: ImageOption, n: int) -> Demand:
    """What an image request reserves: every image at its price, and the images themselves."""
    return Demand(usd_micro=option.price * n, tokens=0, images=n)
