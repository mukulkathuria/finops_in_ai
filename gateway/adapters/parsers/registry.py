"""Parsers from config (spec section 3A): plain text stays in the gateway, Office files and HTML
go to MarkItDown, PDFs and images to the one OCR backend named by OCR_BACKEND. Switching backends
is a config change and a restart; every artifact records the backend that made it."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import httpx2

from gateway.adapters.parsers.sidecar import SidecarParser
from gateway.adapters.parsers.text import TextParser
from gateway.domain.errors import InvalidInput
from gateway.ports.parsers import DocumentParser

_OOXML: Final = "application/vnd.openxmlformats-officedocument."
OFFICE_MIMES: Final = frozenset(
    {
        _OOXML + "wordprocessingml.document",
        _OOXML + "spreadsheetml.sheet",
        _OOXML + "presentationml.presentation",
        "text/html",
    }
)
OCR_MIMES: Final = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}
)


@dataclass(frozen=True, slots=True)
class Parsers:
    text: DocumentParser
    office: DocumentParser
    ocr: DocumentParser

    def for_document(self, mime: str) -> DocumentParser:
        for parser in (self.text, self.office, self.ocr):
            if parser.supports(mime, None):
                return parser
        raise InvalidInput(f"{mime} files cannot be converted", code="unsupported_file_type")


def build_parsers(
    *,
    ocr_backend: str,
    ocr_servers: Mapping[str, tuple[str, str]],
    parser_base_url: str,
    http: httpx2.AsyncClient,
    timeout_s: float,
) -> Parsers:
    """`ocr_servers` maps each OCR backend to its sidecar URL and the model name it serves."""
    base_url, model = ocr_servers[ocr_backend]
    return Parsers(
        text=TextParser(),
        office=SidecarParser(
            "markitdown", parser_base_url, http=http, timeout_s=timeout_s, mimes=OFFICE_MIMES
        ),
        ocr=SidecarParser(
            ocr_backend, base_url, http=http, timeout_s=timeout_s, mimes=OCR_MIMES, model=model
        ),
    )
