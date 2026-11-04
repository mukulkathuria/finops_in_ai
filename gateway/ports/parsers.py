"""Document parsers (spec section 3, family B). One per backend; the registry picks one per
document type, and the OCR backend comes from OCR_BACKEND."""

from typing import Protocol

from gateway.domain.parsing import ParsedDocument, ParseOptions, StoredDocument


class DocumentParser(Protocol):
    @property
    def name(self) -> str: ...

    def supports(self, mime: str, lang_hint: str | None) -> bool: ...

    async def version(self) -> str:
        """The backend version as the backend reports it: part of the parse-cache key."""
        ...

    async def parse(self, document: StoredDocument, options: ParseOptions) -> ParsedDocument: ...
