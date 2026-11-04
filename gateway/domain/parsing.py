"""Document parsing vocabulary (spec section 3, family B; US-15, US-16).

A parser turns a stored file into Markdown, page by page, with a confidence for each page. Pages
read below the confidence bar keep their original image instead of unreliable text. Token figures
are estimates of what a model is charged for the original and for the derived form.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

PageMethod = Literal["text", "text_layer", "markitdown", "ocr"]
PAGE_IMAGE_TOKENS: Final = 1_600  # a document page sent to a vision model as an image, about
_MAX_IMAGE_EDGE: Final = 1_568
_MAX_IMAGE_PIXELS: Final = 1_150_000


@dataclass(frozen=True, slots=True)
class StoredDocument:
    filename: str
    kind: str  # the validated kind (domain/files.py): pdf, png, docx, text, ...
    mime: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ParseOptions:
    min_confidence: float
    lang_hint: str | None = None

    def digest(self) -> bytes:
        """Part of the parse-cache key: equal options give an equal digest."""
        canonical = json.dumps(
            {"min_confidence": round(self.min_confidence, 4), "lang_hint": self.lang_hint},
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).digest()


@dataclass(frozen=True, slots=True)
class ParsedPage:
    page: int
    method: PageMethod
    confidence: float
    markdown: str
    kept_original: bool = False
    image: bytes | None = None  # the page as a PNG when it is kept (PDF pages)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    backend: str
    version: str
    markdown: str
    pages: tuple[ParsedPage, ...]
    warnings: tuple[str, ...] = ()
    gpu_seconds: float = 0.0


def text_tokens(text: str) -> int:
    """About four characters per token: the estimate used when nothing better is known."""
    return (len(text) + 3) // 4


# The parser contract puts this paragraph in place of the text of a page kept as an image.
_KEPT_PAGE: Final = re.compile(r"\[page (\d+) is attached as an image\]")


def indexable_text(markdown: str, pages: Sequence[Mapping[str, Any]]) -> tuple[str, list[int]]:
    """A document's text without the stand-ins for pages kept as images (they say nothing about
    those pages), and the numbers of those pages."""
    kept = sorted({int(p["page"]) for p in pages if p.get("kept_original")})
    numbers = set(kept)
    text = _KEPT_PAGE.sub(lambda m: "" if int(m[1]) in numbers else m[0], markdown)
    return text, kept


def image_tokens(width: int, height: int) -> int:
    """About what vision models charge for an image: scaled to fit 1568 pixels on the long edge
    and 1.15 megapixels, then one token per 750 pixels."""
    if width <= 0 or height <= 0:
        return 0
    scale = min(
        1.0, _MAX_IMAGE_EDGE / max(width, height), math.sqrt(_MAX_IMAGE_PIXELS / (width * height))
    )
    return max(1, math.ceil(width * height * scale * scale / 750))
