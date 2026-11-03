"""The deterministic `rules` classifier: answers the questions it has rules for."""

from collections.abc import Sequence

from gateway.domain.classifiers import (
    INJECTION_QUESTION,
    ClassifierError,
    ClassifierErrorKind,
    Decision,
    Question,
)
from gateway.domain.detectors import RULES_VERSION, injection_rule


class RulesClassifier:
    name = "rules"
    hosted = False

    def answer(self, state: str, questions: Sequence[Question]) -> list[Decision]:
        """Synchronous, so a large text can be judged in a worker thread."""
        decisions: list[Decision] = []
        for q in questions:
            if q.id != INJECTION_QUESTION.id:
                raise ClassifierError(
                    ClassifierErrorKind.UNSUPPORTED,
                    f"the rules backend has no rule for question {q.id!r}",
                    backend=self.name,
                )
            hit = injection_rule(state) is not None
            decisions.append(
                Decision(q.id, "bool", hit, 0.99 if hit else 0.01, self.name, RULES_VERSION)
            )
        return decisions

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]:
        return self.answer(state, questions)
