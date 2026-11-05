"""A reranker behind the `POST /rerank` API that vLLM, Infinity and most model servers share with
Cohere and Jina: `{"model", "query", "documents"}` in, `{"results": [{"index",
"relevance_score"}]}` out. The mock provider serves it too."""

from collections.abc import Sequence
from typing import Final

import httpx2
import orjson

_MAX_PASSAGE_CHARS: Final = 8_000  # of each passage: a cross-encoder reads about 512 tokens


class RerankError(Exception):
    """The reranker could not score the passages; callers keep the vector order."""


def scores_from(raw: bytes, count: int) -> list[float]:
    """One score per passage, in input order; anything else is a malformed answer."""
    scores: list[float | None] = [None] * count
    try:
        for item in orjson.loads(raw)["results"]:
            index, score = int(item["index"]), float(item["relevance_score"])
            if not 0 <= index < count or scores[index] is not None or not 0.0 <= score <= 1.0:
                raise ValueError(
                    "a bad or repeated index, or a score outside 0 to 1"
                    " (normalise the reranker's scores)"
                )
            scores[index] = score
    except (KeyError, TypeError, ValueError) as exc:
        raise RerankError("malformed reranker answer") from exc
    complete = [s for s in scores if s is not None]
    if len(complete) != count:
        raise RerankError("the reranker left passages unscored")
    return complete


class HttpReranker:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        http: httpx2.AsyncClient,
        api_key: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._url = base_url.rstrip("/") + "/rerank"
        self._model = model
        self._http = http
        self._headers = {"content-type": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"
        self._timeout = timeout_s

    @property
    def model(self) -> str:
        return self._model

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        body = {
            "model": self._model,
            "query": query,
            "documents": [p[:_MAX_PASSAGE_CHARS] for p in passages],
            "top_n": len(passages),
            "return_documents": False,
        }
        try:
            resp = await self._http.post(
                self._url, content=orjson.dumps(body), headers=self._headers, timeout=self._timeout
            )
        except httpx2.HTTPError as exc:
            raise RerankError(f"reranker unreachable ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise RerankError(f"reranker answered {resp.status_code}")
        return scores_from(resp.content, len(passages))
