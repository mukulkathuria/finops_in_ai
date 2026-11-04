"""Reading documents: MarkItDown for Office files and HTML, PDFium for PDF text layers and page
images. PDFium is not thread-safe, so every PDFium call runs on one dedicated thread."""

import asyncio
import functools
import io
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final

import pypdfium2 as pdfium
from markitdown import MarkItDown, StreamInfo
from PIL import Image

_PDFIUM: Final = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdfium")
_MAX_RENDER_PIXELS: Final = 4_000_000  # about 2000 x 2000: enough to read, bounded memory
_MAX_PAGE_IMAGE_BYTES: Final = 6 << 20  # below the gateway's cap on a kept page image
_MARKITDOWN: Final = MarkItDown(enable_plugins=False)
OFFICE: Final = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/html": ".html",
}


class Unreadable(Exception):
    """The document cannot be read: damaged, password-protected or too long."""


async def on_pdfium[T](call: Callable[..., T], *args: object) -> T:
    """Run a PDFium call on the PDFium thread. The job is queued before the first suspension,
    so a cancelled caller never skips a queued close."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_PDFIUM, functools.partial(call, *args))


def open_pdf(data: bytes, max_pages: int) -> pdfium.PdfDocument:
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise Unreadable("the PDF is damaged or password-protected") from exc
    pages = len(pdf)
    if pages > max_pages:
        pdf.close()
        raise Unreadable(f"the PDF has {pages} pages; the limit is {max_pages}")
    return pdf


def page_texts(pdf: pdfium.PdfDocument) -> list[str]:
    """The text layer of every page (empty for a scanned page). A page PDFium cannot load is an
    unreadable document, not a sidecar failure."""
    texts: list[str] = []
    for index in range(len(pdf)):
        try:
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    texts.append(str(textpage.get_text_bounded()).strip())
                finally:
                    textpage.close()
            finally:
                page.close()
        except pdfium.PdfiumError as exc:
            raise Unreadable(f"page {index + 1} of the PDF cannot be read") from exc
    return texts


def render_png(pdf: pdfium.PdfDocument, index: int, dpi: int) -> bytes:
    """One page as a PNG of at most _MAX_RENDER_PIXELS (a huge page is scaled down, never rendered
    whole) and _MAX_PAGE_IMAGE_BYTES. The bitmap stays alive until it is copied: PIL may share
    its memory."""
    try:
        page = pdf[index]
        try:
            width, height = page.get_size()
            scale = min(dpi / 72, math.sqrt(_MAX_RENDER_PIXELS / max(width * height, 1.0)))
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        finally:
            page.close()
    except pdfium.PdfiumError as exc:
        raise Unreadable(f"page {index + 1} of the PDF cannot be rendered") from exc
    return _png_within(image)


def _png_within(image: Image.Image) -> bytes:
    """PNG bytes under the size cap: the resolution halves until they fit."""
    data = b""
    for _ in range(5):
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= _MAX_PAGE_IMAGE_BYTES:
            break
        image = image.resize((max(1, image.width // 2), max(1, image.height // 2)))
    return data


def close_pdf(pdf: pdfium.PdfDocument) -> None:
    pdf.close()


def office_markdown(data: bytes, mime: str) -> str:
    """Office files and HTML through MarkItDown, from bytes only: it is never handed a path or a
    URL, so it cannot be steered into reading anything else."""
    stream_info = StreamInfo(extension=OFFICE[mime], mimetype=mime)
    try:
        result = _MARKITDOWN.convert_stream(io.BytesIO(data), stream_info=stream_info)
    except Exception as exc:
        raise Unreadable("the document cannot be converted") from exc
    return str(result.markdown).strip()
