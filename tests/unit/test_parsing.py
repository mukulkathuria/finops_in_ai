"""Document parsing (US-15, US-16): token estimates, the cache key, the circuit breaker, the
registry and the sidecar contract, against the stand-in sidecar and against failing ones."""

import httpx2
import orjson
import pytest

from gateway.adapters.parsers.registry import OCR_MIMES, OFFICE_MIMES, build_parsers
from gateway.adapters.parsers.sidecar import CircuitBreaker, SidecarParser
from gateway.adapters.parsers.text import TextParser
from gateway.domain.errors import InvalidInput, Unavailable
from gateway.domain.parsing import ParseOptions, StoredDocument, image_tokens, text_tokens
from tests.support import fake_docparse
from tests.unit.test_files import office, pdf, text_pdf

OPTIONS = ParseOptions(min_confidence=0.8)
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SERVERS = {
    "glm_ocr": ("http://glm-ocr:8000/v1", "zai-org/GLM-OCR"),
    "paddleocr_vl": ("http://paddleocr-vl:8080/v1", "PaddleOCR-VL-1.5-0.9B"),
}


def _fake() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        mounts={"http://docparse:8000": httpx2.ASGITransport(app=fake_docparse.app)}
    )


def _parser(http: httpx2.AsyncClient, breaker: CircuitBreaker | None = None) -> SidecarParser:
    return SidecarParser(
        "glm_ocr",
        "http://docparse:8000/v1",
        http=http,
        timeout_s=10,
        mimes=OCR_MIMES,
        model="zai-org/GLM-OCR",
        breaker=breaker,
    )


def test_token_estimates_and_the_cache_key() -> None:
    assert text_tokens("abcd" * 10) == 10
    assert image_tokens(1024, 1024) == 1399
    assert image_tokens(4000, 3000) == 1534  # scaled down to about 1.15 megapixels first
    assert image_tokens(0, 10) == 0
    assert ParseOptions(0.8).digest() == ParseOptions(0.8).digest()
    assert ParseOptions(0.8).digest() != ParseOptions(0.9).digest()


def test_the_circuit_breaker_opens_after_failures_and_closes_after_the_cooldown() -> None:
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, cooldown_s=10, clock=lambda: now[0])
    breaker.failure()
    assert breaker.allow()
    breaker.failure()
    assert not breaker.allow()
    now[0] = 10.0
    assert breaker.allow()
    breaker.failure()
    breaker.success()  # a success resets the count
    breaker.failure()
    assert breaker.allow()


async def test_the_sidecar_contract_for_a_text_pdf_and_a_scan() -> None:
    async with _fake() as http:
        parser = _parser(http)
        assert await parser.version() == "fake-docparse-1|zai-org/GLM-OCR"
        text = text_pdf("Revenue grew 12 percent.")
        doc = await parser.parse(StoredDocument("r.pdf", "pdf", "application/pdf", text), OPTIONS)
        assert (doc.backend, doc.version) == ("glm_ocr", "fake-docparse-1|zai-org/GLM-OCR")
        assert [p.method for p in doc.pages] == ["text_layer"]
        assert "Revenue grew 12 percent." in doc.markdown
        scan = await parser.parse(
            StoredDocument("s.pdf", "pdf", "application/pdf", pdf(2)), ParseOptions(0.95)
        )
        assert [(p.method, p.kept_original) for p in scan.pages] == [("ocr", True)] * 2
        assert all(p.image is not None and p.image.startswith(b"\x89PNG") for p in scan.pages)


async def test_failures_map_to_clear_errors_and_open_the_breaker() -> None:
    calls = [0]

    def answering(status: int, body: object) -> httpx2.AsyncClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            calls[0] += 1
            return httpx2.Response(status, content=orjson.dumps(body))

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))

    document = StoredDocument("s.pdf", "pdf", "application/pdf", pdf())
    page = {"page": 1, "method": "ocr", "confidence": 0.9, "markdown": "x"}
    unusable_image = {**page, "kept_original": True, "image": "data:image/png;base64,QUJD"}
    cases: list[tuple[int, object, type[Exception], str]] = [
        (422, {"detail": "the PDF is damaged"}, InvalidInput, "parse_failed"),
        (500, {"detail": "boom"}, Unavailable, "parser_unavailable"),
        (200, {"unexpected": True}, Unavailable, "parser_unavailable"),
        (
            200,
            {"backend": "x", "version": "1", "markdown": "x", "pages": [unusable_image]},
            Unavailable,
            "parser_unavailable",
        ),
    ]
    for status, body, error, code in cases:
        async with answering(status, body) as http:
            with pytest.raises(error) as exc:
                await _parser(http).parse(document, OPTIONS)
            assert getattr(exc.value, "code", None) == code

    def refuse(request: httpx2.Request) -> httpx2.Response:
        calls[0] += 1
        raise httpx2.ConnectError("refused", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(refuse)) as http:
        parser = _parser(http, CircuitBreaker(threshold=2, cooldown_s=60))
        calls[0] = 0
        for _ in range(4):
            with pytest.raises(Unavailable):
                await parser.parse(document, OPTIONS)
        assert calls[0] == 2  # open after two failures: the sidecar is not called again


async def test_the_registry_routes_by_type_and_follows_ocr_backend() -> None:
    async with httpx2.AsyncClient() as http:
        for backend in SERVERS:
            parsers = build_parsers(
                ocr_backend=backend,
                ocr_servers=SERVERS,
                parser_base_url="http://docparse:8000/v1",
                http=http,
                timeout_s=5,
            )
            assert {parsers.for_document(m).name for m in OCR_MIMES} == {backend}
            assert {parsers.for_document(m).name for m in OFFICE_MIMES} == {"markitdown"}
            assert parsers.for_document("text/csv").name == "text"
        with pytest.raises(InvalidInput):
            parsers.for_document("application/zip")


async def test_office_files_and_text_files() -> None:
    async with _fake() as http:
        markitdown = SidecarParser(
            "markitdown", "http://docparse:8000/v1", http=http, timeout_s=10, mimes=OFFICE_MIMES
        )
        docx = office(extra={"word/media/note.xml": b"<w:t>Quarterly plan</w:t>"})
        doc = await markitdown.parse(StoredDocument("plan.docx", "docx", DOCX, docx), OPTIONS)
        assert doc.pages[0].method == "markitdown"
        assert "Quarterly plan" in doc.markdown
    text = "﻿# Title\nbody".encode()
    plain = await TextParser().parse(StoredDocument("a.md", "text", "text/markdown", text), OPTIONS)
    assert (plain.markdown, plain.pages[0].method) == ("# Title\nbody", "text")
