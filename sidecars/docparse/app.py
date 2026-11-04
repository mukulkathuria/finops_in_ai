"""docparse: the document-parsing sidecar behind the gateway's parser contract (spec section 3A).

GET /v1/info names this build; POST /v1/parse takes a file and options and returns Markdown with a
method and confidence for every page. Office files and HTML go through MarkItDown. PDF pages with
a text layer are read as text; pages without one, and images, are recognised by the OCR model
behind DOCPARSE_VLM_BASE_URL with the prompt of the backend the gateway names (GLM-OCR or
PaddleOCR-VL). A page recognised below the gateway's confidence bar comes back with its image, so
the gateway can keep the original instead of unreliable text.

Refusals: 400 for options this instance cannot serve, 413 for an oversized file, 422 for a
document that cannot be read, 503 when OCR is needed but unavailable.
"""

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

import httpx2
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from docparse.config import Config
from docparse.documents import (
    OFFICE,
    Unreadable,
    close_pdf,
    office_markdown,
    on_pdfium,
    open_pdf,
    page_texts,
    render_png,
)
from docparse.vision import PROMPTS, VisionClient, VisionError

VERSION: Final = "docparse-0.1.0"
IMAGES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


class Options(BaseModel):
    backend: str
    model: str | None = None
    min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    lang_hint: str | None = None


class Refused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _page(number: int, method: str, confidence: float, markdown: str) -> dict[str, Any]:
    return {
        "page": number,
        "method": method,
        "confidence": round(confidence, 4),
        "markdown": markdown,
    }


@dataclass
class _Run:
    """The work of one parse request."""

    config: Config
    vision: VisionClient | None
    options: Options
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    async def recognise(
        self, number: int, image: bytes, mime: str, *, return_image: bool
    ) -> dict[str, Any]:
        if self.vision is None:
            raise Refused(503, "OCR is not configured on this instance (DOCPARSE_VLM_BASE_URL)")
        if self.options.backend not in PROMPTS:
            raise Refused(400, f"the {self.options.backend} backend does not recognise images")
        model = self.options.model or self.config.vlm_model
        if not model:
            raise Refused(400, "no OCR model was named (options.model or DOCPARSE_VLM_MODEL)")
        seen = await self.vision.recognise(
            image,
            mime,
            backend=self.options.backend,
            model=model,
            max_tokens=self.config.max_output_tokens,
        )
        self.seconds += seen.seconds
        if seen.warning:
            self.warnings.append(f"page {number}: {seen.warning}")
        page = _page(number, "ocr", seen.confidence, seen.markdown)
        if seen.confidence < self.options.min_confidence:
            self.warnings.append(
                f"page {number}: confidence {seen.confidence:.2f} is below"
                f" {self.options.min_confidence:.2f}; the original is kept"
            )
            page["markdown"] = f"[page {number} is attached as an image]"
            page["kept_original"] = True
            if return_image:
                page["image"] = "data:image/png;base64," + base64.b64encode(image).decode()
        return page

    async def pdf(self, data: bytes) -> list[dict[str, Any]]:
        pdf = await on_pdfium(open_pdf, data, self.config.max_pages)
        try:
            texts = await on_pdfium(page_texts, pdf)
            pages: list[dict[str, Any] | None] = [None] * len(texts)
            scans: list[int] = []
            for index, text in enumerate(texts):
                if len(text) >= self.config.text_layer_min_chars:
                    pages[index] = _page(index + 1, "text_layer", 1.0, text)
                else:
                    scans.append(index)
            slots = asyncio.Semaphore(self.config.ocr_concurrency)

            async def scan(index: int) -> None:
                async with slots:
                    png = await on_pdfium(render_png, pdf, index, self.config.render_dpi)
                    pages[index] = await self.recognise(
                        index + 1, png, "image/png", return_image=True
                    )

            # A task group stops queueing renders as soon as one page fails, so nothing touches
            # the document after it is closed.
            try:
                async with asyncio.TaskGroup() as group:
                    for index in scans:
                        group.create_task(scan(index))
            except ExceptionGroup as failed:
                raise failed.exceptions[0] from None
        finally:
            await on_pdfium(close_pdf, pdf)
        return [p for p in pages if p is not None]

    async def image(self, data: bytes, mime: str) -> list[dict[str, Any]]:
        return [await self.recognise(1, data, mime, return_image=False)]

    async def office(self, data: bytes, mime: str) -> list[dict[str, Any]]:
        markdown = await asyncio.to_thread(office_markdown, data, mime)
        return [_page(1, "markitdown", 1.0, markdown)]


def _refusal(exc: Refused) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


def create_app(config: Config | None = None, http: httpx2.AsyncClient | None = None) -> FastAPI:
    settings = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http or httpx2.AsyncClient()
        app.state.vision = (
            VisionClient(
                settings.vlm_base_url,
                api_key=settings.vlm_api_key,
                timeout_s=settings.vlm_timeout_s,
                http=client,
            )
            if settings.vlm_base_url
            else None
        )
        try:
            yield
        finally:
            if http is None:
                await client.aclose()

    app = FastAPI(
        title="docparse",
        version=VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/info")
    async def info() -> dict[str, Any]:
        return {
            "backend": "docparse",
            "version": VERSION,
            "backends": sorted(settings.backends),
            "ocr": settings.vlm_base_url is not None,
        }

    @app.post("/v1/parse", response_model=None)
    async def parse(
        request: Request,
        file: Annotated[UploadFile, File()],
        options: Annotated[str, Form()] = "{}",
    ) -> JSONResponse:
        try:
            opts = Options.model_validate_json(options)
        except ValidationError:
            return _refusal(Refused(400, "options must be the parser contract's JSON object"))
        if opts.backend not in settings.backends:
            return _refusal(Refused(400, f"this instance does not serve {opts.backend}"))
        data = await file.read()
        if len(data) > settings.max_bytes:
            return _refusal(Refused(413, "the file is too large"))
        mime = (file.content_type or "").split(";")[0].strip().lower()
        run = _Run(settings, request.app.state.vision, opts)
        try:
            if mime in OFFICE:
                pages = await run.office(data, mime)
            elif mime == "application/pdf":
                pages = await run.pdf(data)
            elif mime in IMAGES:
                pages = await run.image(data, mime)
            else:
                raise Refused(422, f"{mime or 'files of this type'} cannot be parsed")
        except Unreadable as exc:
            return _refusal(Refused(422, str(exc)))
        except VisionError as exc:
            return _refusal(Refused(503, str(exc)))
        except Refused as exc:
            return _refusal(exc)
        return JSONResponse(
            {
                "backend": opts.backend,
                "version": VERSION,
                "markdown": "\n\n".join(p["markdown"] for p in pages),
                "pages": pages,
                "warnings": run.warnings,
                "gpu_seconds": round(run.seconds, 3),
            }
        )

    return app
