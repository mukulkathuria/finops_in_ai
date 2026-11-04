"""The in-gateway parser for plain text, Markdown, CSV and JSON: already text, used as it is."""

from typing import Final

from gateway.domain.parsing import ParsedDocument, ParsedPage, ParseOptions, StoredDocument

_MIMES: Final = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})
_VERSION: Final = "text-1"


class TextParser:
    name = "text"

    def supports(self, mime: str, lang_hint: str | None) -> bool:
        return mime in _MIMES

    async def version(self) -> str:
        return _VERSION

    async def parse(self, document: StoredDocument, options: ParseOptions) -> ParsedDocument:
        text = document.data.decode("utf-8-sig")  # validated as UTF-8 when it was uploaded
        page = ParsedPage(1, "text", 1.0, text)
        return ParsedDocument(self.name, _VERSION, text, (page,))
