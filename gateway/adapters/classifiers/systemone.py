"""SystemOne protocol client: one adapter for Laya (local, `laya-serve`) and Jev (hosted).

`POST {base}/v1/systemone` with `{"state", "questions": {name: {type, instructions, criteria}}}`;
types are `noul` (P(yes)), `choice` and `score`; at most 10 questions and about 128,000
characters of state per call. Vendor shapes stay in this file (anti-corruption layer).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx2
import orjson

from gateway.domain.classifiers import (
    ClassifierError,
    ClassifierErrorKind,
    Decision,
    Question,
)

MAX_QUESTIONS: Final = 10
MAX_STATE_CHARS: Final = 120_000
_TYPES: Final = {"bool": "noul", "choice": "choice", "score": "score"}


def _criteria(q: Question) -> Any:
    if q.criteria is None:
        return None
    if isinstance(q.criteria, Mapping):
        return dict(q.criteria)
    return list(q.criteria)


class SystemOneClassifier:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        http: httpx2.AsyncClient,
        api_key: str | None = None,
        model: str | None = None,
        hosted: bool = False,
        timeout_s: float = 2.0,
    ) -> None:
        self.name = name
        self.hosted = hosted
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._http = http
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]:
        decisions: list[Decision] = []
        for start in range(0, len(questions), MAX_QUESTIONS):
            decisions += await self._call(state, questions[start : start + MAX_QUESTIONS])
        return decisions

    async def _call(self, state: str, questions: Sequence[Question]) -> list[Decision]:
        body: dict[str, Any] = {
            "state": state[:MAX_STATE_CHARS],
            "questions": {
                q.id: {
                    "type": _TYPES[q.kind],
                    "instructions": q.instructions,
                    **({"criteria": c} if (c := _criteria(q)) is not None else {}),
                }
                for q in questions
            },
        }
        if self._model:
            body["model"] = self._model
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._http.post(
                self._url, content=orjson.dumps(body), headers=headers, timeout=self._timeout
            )
        except httpx2.TimeoutException as exc:
            raise ClassifierError(
                ClassifierErrorKind.TIMEOUT, "classifier timed out", backend=self.name
            ) from exc
        except httpx2.HTTPError as exc:
            raise ClassifierError(
                ClassifierErrorKind.UNAVAILABLE, "classifier unreachable", backend=self.name
            ) from exc
        if response.status_code != 200:
            raise self._error(response)
        try:
            payload = orjson.loads(response.content)
            version = str(payload.get("model") or self.name)
            answers = payload["answers"]
            return [self._decision(q, answers[q.id], version) for q in questions]
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ClassifierError(
                ClassifierErrorKind.UNAVAILABLE, "malformed classifier response", backend=self.name
            ) from exc

    def _decision(self, q: Question, answer: Mapping[str, Any], version: str) -> Decision:
        if q.kind == "bool":
            p = float(answer["noul"])
            return Decision(q.id, "bool", p >= 0.5, p, self.name, version)
        if q.kind == "choice":
            return Decision(
                q.id,
                "choice",
                str(answer["choice"]),
                float(answer.get("confidence", 0.0)),
                self.name,
                version,
            )
        return Decision(
            q.id,
            "score",
            float(answer["score"]),
            float(answer.get("confidence", 0.0)),
            self.name,
            version,
        )

    def _error(self, response: httpx2.Response) -> ClassifierError:
        status = response.status_code
        message = f"classifier returned HTTP {status}"
        retry_after = None
        if status == 429:
            try:
                retry_after = float(response.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
            kind = ClassifierErrorKind.RATE_LIMIT
        elif status in (401, 403):
            kind = ClassifierErrorKind.AUTH
        elif status in (400, 404, 413, 422):
            kind = ClassifierErrorKind.BAD_REQUEST
        else:
            kind = ClassifierErrorKind.UNAVAILABLE
        return ClassifierError(kind, message, backend=self.name, retry_after_s=retry_after)
