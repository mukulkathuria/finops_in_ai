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
    size: str
    quality: str
    price: int  # micro-USD per image


def image_option(entry: ModelPrice, size: str | None, quality: str | None) -> ImageOption:
    """The size, quality and per-image price a request gets. A model with an `image_prices`
    table offers exactly those options; without one, every image costs `price_image`. A model
    with neither price is refused: an unpriced image would escape the money budgets."""
    size, quality = size or DEFAULT_SIZE, quality or DEFAULT_QUALITY
    if entry.image_prices:
        prices = dict(entry.image_prices)
        price = prices.get(f"{size}/{quality}")
        if price is None:
            offered = ", ".join(sorted(prices))
            raise InvalidInput(
                f"{entry.public_name} offers these size/quality options: {offered}",
                code="unsupported_image_option",
                param="size",
            )
        return ImageOption(size, quality, price)
    if entry.price_image is None:
        raise InvalidInput(
            f"{entry.public_name} has no image price in the catalog", code="model_not_priced"
        )
    return ImageOption(size, quality, entry.price_image)


def image_demand(option: ImageOption, n: int) -> Demand:
    """What an image request reserves: every image at its price, and the images themselves."""
    return Demand(usd_micro=option.price * n, tokens=0, images=n)
