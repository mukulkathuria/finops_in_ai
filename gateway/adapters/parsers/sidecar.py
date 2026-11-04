"""Parsers behind the sidecar contract (spec section 3A, "OCR: one backend at a time").

`POST {base}/parse` takes the file and options (backend, model, confidence bar) and returns
Markdown with a method and confidence per page, plus the page image for a page kept as the
original; `GET {base}/info` names the backend version. GLM-OCR, PaddleOCR-VL and MarkItDown are
all reached this way: only the URL and the model name differ. Every call has a deadline, and a
circuit breaker stops calling a sidecar that keeps failing.
"""

import base64
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal

import httpx2
import orjson
from pydantic import BaseModel, Field, ValidationError

from gateway.adapters.resilience import CircuitBreaker
from gateway.domain.errors import InvalidInput, Unavailable
from gateway.domain.parsing import ParsedDocument, ParsedPage, ParseOptions, StoredDocument

_PNG: Final = b"\x89PNG\r\n\x1a\n"
_PNG_URL: Final = "data:image/png;base64,"
_MAX_PAGE_IMAGE: Final = 8 << 20
_VERSION_TTL_S: Final = 300.0


class _Page(BaseModel):
    page: int = Field(ge=1)
    method: Literal["text", "text_layer", "markitdown", "ocr"]
    confidence: float = Field(ge=0.0, le=1.0)
    markdown: str
    kept_original: bool = False
    image: str | None = None  # data:image/png;base64,... for a kept PDF page


class _Parsed(BaseModel):
    backend: str
    version: str = Field(min_length=1, max_length=200)
    markdown: str
    pages: list[_Page] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    gpu_seconds: float = Field(default=0.0, ge=0.0)


class _Info(BaseModel):
    backend: str
    version: str = Field(min_length=1, max_length=200)


def _png(image: str | None) -> bytes | None:
    if image is None:
        return None
    if not image.startswith(_PNG_URL):
        raise ValueError("the page image is not a PNG data URL")
    data = base64.b64decode(image[len(_PNG_URL) :], validate=True)
    if not data.startswith(_PNG) or len(data) > _MAX_PAGE_IMAGE:
        raise ValueError("the page image is not a usable PNG")
    return data


def _reason(resp: httpx2.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError, AttributeError:
        detail = None
    return str(detail)[:200] if detail else "the document is unreadable"


class SidecarParser:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        http: httpx2.AsyncClient,
        timeout_s: float,
        mimes: frozenset[str],
        model: str | None = None,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._base = base_url.rstrip("/")
        self._http = http
        self._timeout = timeout_s
        self._mimes = mimes
        self._model = model
        self._breaker = breaker or CircuitBreaker()
        self._clock = clock
        self._version: tuple[float, str] | None = None

    def supports(self, mime: str, lang_hint: str | None) -> bool:
        return mime in self._mimes

    def _versioned(self, version: str) -> str:
        return f"{version}|{self._model}" if self._model else version

    async def version(self) -> str:
        now = self._clock()
        if self._version is not None and self._version[0] > now:
            return self._version[1]
        body = await self._send(lambda: self._http.get(f"{self._base}/info", timeout=self._timeout))
        version = self._versioned(self._validate(_Info, body).version)
        self._version = (now + _VERSION_TTL_S, version)
        return version

    async def parse(self, document: StoredDocument, options: ParseOptions) -> ParsedDocument:
        settings = {
            "backend": self.name,
            "model": self._model,
            "min_confidence": options.min_confidence,
            "lang_hint": options.lang_hint,
        }
        body = await self._send(
            lambda: self._http.post(
                f"{self._base}/parse",
                data={"options": orjson.dumps(settings).decode()},
                files={"file": (document.filename, document.data, document.mime)},
                timeout=self._timeout,
            )
        )
        parsed = self._validate(_Parsed, body)
        try:
            pages = tuple(
                ParsedPage(
                    p.page, p.method, p.confidence, p.markdown, p.kept_original, _png(p.image)
                )
                for p in parsed.pages
            )
        except ValueError as exc:
            self._breaker.failure()
            raise self._unavailable() from exc
        return ParsedDocument(
            backend=self.name,
            version=self._versioned(parsed.version),
            markdown=parsed.markdown,
            pages=pages,
            warnings=tuple(parsed.warnings),
            gpu_seconds=parsed.gpu_seconds,
        )

    def _unavailable(self) -> Unavailable:
        return Unavailable(
            f"the {self.name} document parser is unavailable; try again shortly",
            code="parser_unavailable",
        )

    async def _send(self, request: Callable[[], Awaitable[httpx2.Response]]) -> object:
        if not self._breaker.allow():
            raise self._unavailable()
        try:
            resp = await request()
        except httpx2.HTTPError as exc:  # refused, reset, timed out
            self._breaker.failure()
            raise self._unavailable() from exc
        if resp.status_code == 422:  # the document itself cannot be converted
            self._breaker.success()
            raise InvalidInput(
                f"the document could not be converted: {_reason(resp)}",
                code="parse_failed",
                param="file",
            )
        if resp.status_code != 200:
            self._breaker.failure()
            raise self._unavailable()
        try:
            body: object = resp.json()
        except ValueError as exc:
            self._breaker.failure()
            raise self._unavailable() from exc
        self._breaker.success()
        return body

    def _validate[M: BaseModel](self, model: type[M], body: object) -> M:
        try:
            return model.model_validate(body)
        except ValidationError as exc:  # the sidecar answered outside its contract
            self._breaker.failure()
            raise self._unavailable() from exc
