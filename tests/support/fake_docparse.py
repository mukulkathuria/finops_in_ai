"""A stand-in for the docparse sidecar that speaks the parser contract (`GET /v1/info`,
`POST /v1/parse`), deterministic and light: PDF pages with a text layer are read with pypdf,
pages without one are "recognised" with a fixed confidence, HTML and Office files are reduced to
their text. It counts the parses it serves per backend."""

import base64
import io
import re
import zipfile
import zlib
from typing import Annotated, Any

import orjson
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pypdf import PdfReader

VERSION = "fake-docparse-1"
OCR_CONFIDENCE = 0.9
PARSES: dict[str, int] = {}
_OOXML = "application/vnd.openxmlformats-officedocument."

app = FastAPI()


def _pixel() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    header = (1).to_bytes(4, "big") * 2 + bytes((8, 2, 0, 0, 0))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    png += chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _recognised(number: int, min_confidence: float, *, image_page: bool) -> dict[str, Any]:
    if min_confidence > OCR_CONFIDENCE:
        return {
            "page": number,
            "method": "ocr",
            "confidence": OCR_CONFIDENCE,
            "markdown": f"[page {number} is attached as an image]",
            "kept_original": True,
            "image": None if image_page else _pixel(),
        }
    return {
        "page": number,
        "method": "ocr",
        "confidence": OCR_CONFIDENCE,
        "markdown": f"Recognised text of page {number}.",
    }


def _pdf(data: bytes, min_confidence: float) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for number, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
        text = (page.extract_text() or "").strip()
        if len(text) >= 8:
            pages.append(
                {"page": number, "method": "text_layer", "confidence": 1.0, "markdown": text}
            )
        else:
            pages.append(_recognised(number, min_confidence, image_page=False))
    return pages


def _office(data: bytes, mime: str) -> str:
    if mime == "text/html":
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", data.decode("utf-8"))).strip()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = b" ".join(archive.read(n) for n in archive.namelist() if n.endswith(".xml"))
    return re.sub(r"\s+", " ", re.sub(rb"<[^>]+>", b" ", xml).decode("utf-8")).strip()


@app.get("/v1/info")
async def info() -> dict[str, str]:
    return {"backend": "fake", "version": VERSION}


@app.post("/v1/parse")
async def parse(
    file: Annotated[UploadFile, File()], options: Annotated[str, Form()] = "{}"
) -> JSONResponse:
    settings = orjson.loads(options)
    backend = str(settings.get("backend") or "unknown")
    PARSES[backend] = PARSES.get(backend, 0) + 1
    min_confidence = float(settings.get("min_confidence") or 0.0)
    data = await file.read()
    mime = file.content_type or ""
    if mime == "application/pdf":
        pages = _pdf(data, min_confidence)
    elif mime.startswith("image/"):
        pages = [_recognised(1, min_confidence, image_page=True)]
    elif mime == "text/html" or mime.startswith(_OOXML):
        pages = [
            {"page": 1, "method": "markitdown", "confidence": 1.0, "markdown": _office(data, mime)}
        ]
    else:
        return JSONResponse({"detail": f"{mime} is not supported"}, status_code=422)
    ocr_pages = sum(1 for p in pages if p["method"] == "ocr")
    return JSONResponse(
        {
            "backend": backend,
            "version": VERSION,
            "markdown": "\n\n".join(p["markdown"] for p in pages),
            "pages": pages,
            "warnings": [
                f"page {p['page']}: low confidence" for p in pages if p.get("kept_original")
            ],
            "gpu_seconds": 0.1 * ocr_pages,
        }
    )
