"""The docparse sidecar against the parser contract: MarkItDown, PDF text layers, OCR through a
stand-in vision server, confidence and kept pages, and refusals. Run in the sidecar's own pinned
environment (requirements-test.txt), from the repository root."""

import base64
import io
import json
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from docparse.app import create_app
from docparse.config import Config
from openpyxl import Workbook
from pptx import Presentation

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
VLM = "http://vlm:8000/v1"


def text_pdf(*texts: str) -> bytes:
    """A PDF whose pages carry the given text layer (Helvetica); an empty text is a scan."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(texts)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * i)
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    trailer = b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
    out.write(trailer % (len(objects) + 1, xref))
    return out.getvalue()


def docx(text: str) -> bytes:
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return out.getvalue()


def xlsx() -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["region", "amount"])
    sheet.append(["emea", 42])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def pptx(title: str) -> bytes:
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = title
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


def vision(
    *,
    logprob: float = -0.05,
    content: str = "# Invoice\n\nTotal: 42 EUR",
    finish: str = "stop",
    status: int = 200,
    seen: list[dict[str, Any]] | None = None,
) -> httpx2.AsyncClient:
    """A stand-in OpenAI-compatible vision server."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        if status != 200:
            return httpx2.Response(status, json={"error": "down"})
        tokens = [{"token": word, "logprob": logprob} for word in content.split()]
        choice = {
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish,
            "logprobs": {"content": tokens},
        }
        return httpx2.Response(200, json={"choices": [choice]})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(answer))


@asynccontextmanager
async def sidecar(
    http: httpx2.AsyncClient | None = None, **config: Any
) -> AsyncIterator[httpx2.AsyncClient]:
    settings = Config(vlm_base_url=VLM if http is not None else None, vlm_model="ocr", **config)
    app = create_app(settings, http or httpx2.AsyncClient())
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://docparse") as client:
            yield client


async def parse(
    client: httpx2.AsyncClient, name: str, data: bytes, mime: str, **options: Any
) -> httpx2.Response:
    settings = {"backend": "glm_ocr", "model": "zai-org/GLM-OCR", "min_confidence": 0.8}
    return await client.post(
        "/v1/parse",
        files={"file": (name, data, mime)},
        data={"options": json.dumps({**settings, **options})},
    )


def _ok(resp: httpx2.Response) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert {"backend", "version", "markdown", "pages", "warnings", "gpu_seconds"} <= set(body)
    return body


async def test_info_names_the_build_and_its_backends() -> None:
    async with sidecar() as client:
        info = (await client.get("/v1/info")).json()
    assert info["version"].startswith("docparse-")
    assert info["backends"] == ["glm_ocr", "markitdown", "paddleocr_vl"]
    assert info["ocr"] is False


async def test_text_layers_are_read_without_the_ocr_model() -> None:
    seen: list[dict[str, Any]] = []
    long = "Revenue grew twelve percent in the third quarter."
    async with sidecar(vision(seen=seen)) as client:
        body = _ok(await parse(client, "r.pdf", text_pdf(long, long), "application/pdf"))
    assert [(p["page"], p["method"], p["confidence"]) for p in body["pages"]] == [
        (1, "text_layer", 1.0),
        (2, "text_layer", 1.0),
    ]
    assert long in body["markdown"]
    assert seen == []


async def test_scans_are_recognised_with_the_backend_prompt_and_a_confidence() -> None:
    for backend, prompt in (("glm_ocr", "Text Recognition:"), ("paddleocr_vl", "OCR:")):
        seen: list[dict[str, Any]] = []
        async with sidecar(vision(seen=seen)) as client:
            resp = await parse(
                client, "s.pdf", text_pdf("", ""), "application/pdf", backend=backend
            )
        body = _ok(resp)
        assert [p["method"] for p in body["pages"]] == ["ocr", "ocr"]
        assert all(0.95 < p["confidence"] < 0.96 for p in body["pages"])  # exp(-0.05)
        assert "Total: 42 EUR" in body["markdown"]
        assert len(seen) == 2
        assert seen[0]["model"] == "zai-org/GLM-OCR"
        assert seen[0]["logprobs"] is True
        content = seen[0]["messages"][0]["content"]
        assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert content[1]["text"] == prompt


async def test_pages_below_the_bar_come_back_with_their_image() -> None:
    async with sidecar(vision(logprob=-1.0)) as client:
        body = _ok(await parse(client, "s.pdf", text_pdf(""), "application/pdf"))
    (page,) = body["pages"]
    assert page["kept_original"] is True
    assert page["markdown"] == "[page 1 is attached as an image]"
    png = base64.b64decode(page["image"].removeprefix("data:image/png;base64,"))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert any("original is kept" in w for w in body["warnings"])


async def test_cut_off_and_fenced_answers() -> None:
    async with sidecar(vision(finish="length")) as client:
        cut = _ok(await parse(client, "s.pdf", text_pdf(""), "application/pdf"))
    assert cut["pages"][0]["kept_original"] is True  # a cut-off page is not trusted
    fenced = "```markdown\n# Title\n\nBody\n```"
    async with sidecar(vision(content=fenced)) as client:
        body = _ok(await parse(client, "s.pdf", text_pdf(""), "application/pdf"))
    assert body["pages"][0]["markdown"] == "# Title\n\nBody"


async def test_images_are_recognised_and_kept_without_a_copy() -> None:
    picture = b"\x89PNG\r\n\x1a\n" + bytes(32)  # the stand-in vision server does not decode it
    async with sidecar(vision(logprob=-2.0)) as client:
        body = _ok(await parse(client, "photo.png", picture, "image/png"))
    (page,) = body["pages"]
    assert (page["method"], page["kept_original"]) == ("ocr", True)
    assert "image" not in page  # the gateway already holds the original image


async def test_office_files_and_html_go_through_markitdown() -> None:
    files: list[tuple[str, bytes, str, Callable[[str], bool]]] = [
        ("plan.docx", docx("Quarterly plan"), DOCX, lambda md: "Quarterly plan" in md),
        ("sales.xlsx", xlsx(), XLSX, lambda md: "emea" in md and "42" in md),
        ("deck.pptx", pptx("Roadmap"), PPTX, lambda md: "Roadmap" in md),
        ("page.html", b"<h1>Title</h1><p>Body text</p>", "text/html", lambda md: "# Title" in md),
    ]
    async with sidecar() as client:
        for name, data, mime, check in files:
            body = _ok(await parse(client, name, data, mime, backend="markitdown"))
            assert body["pages"][0]["method"] == "markitdown", name
            assert check(body["markdown"]), (name, body["markdown"])


async def test_refusals_carry_a_reason() -> None:
    async with sidecar(vision()) as client:
        damaged = await parse(client, "bad.pdf", b"%PDF-1.7\n" + b"\x00" * 64, "application/pdf")
        unsupported = await parse(client, "a.zip", b"PK\x03\x04", "application/zip")
        bad_options = await client.post(
            "/v1/parse", files={"file": ("a.pdf", b"x", "application/pdf")}, data={"options": "["}
        )
    statuses = (damaged.status_code, unsupported.status_code, bad_options.status_code)
    assert statuses == (422, 422, 400)
    assert damaged.json()["detail"]
    async with sidecar(vision(), backends=frozenset({"markitdown"})) as client:
        assert (await parse(client, "s.pdf", text_pdf(""), "application/pdf")).status_code == 400
    async with sidecar(vision(status=500)) as client:
        assert (await parse(client, "s.pdf", text_pdf(""), "application/pdf")).status_code == 503
    async with sidecar() as client:  # no OCR model configured
        assert (await parse(client, "s.pdf", text_pdf(""), "application/pdf")).status_code == 503


async def test_many_scanned_pages_are_recognised_in_order() -> None:
    seen: list[dict[str, Any]] = []
    async with sidecar(vision(seen=seen), ocr_concurrency=2) as client:
        body = _ok(await parse(client, "s.pdf", text_pdf(*[""] * 6), "application/pdf"))
    assert [p["page"] for p in body["pages"]] == [1, 2, 3, 4, 5, 6]
    assert len(seen) == 6
