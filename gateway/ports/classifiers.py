"""Classifier port (spec section 3, family C). Adapters raise only `ClassifierError`."""

from collections.abc import Sequence
from typing import Protocol

from gateway.domain.classifiers import Decision, Question


class Classifier(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def hosted(self) -> bool:
        """True when text leaves the organisation's infrastructure (Jev)."""
        ...

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]: ...
