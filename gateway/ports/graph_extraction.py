"""Reading a chunk of a domain's document into relations between named entities (ADR 0011)."""

from typing import Protocol

from gateway.domain.knowledge import DomainSchema, Extraction


class GraphExtractor(Protocol):
    @property
    def model(self) -> str: ...

    async def extract(self, schema: DomainSchema, text: str) -> Extraction:
        """The relations the text states, of the schema's types; ExtractionFailed when the model
        cannot answer readably."""
        ...
