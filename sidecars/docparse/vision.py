"""OCR through an OpenAI-compatible vision server (vLLM serving GLM-OCR or PaddleOCR-VL): one page
image in, Markdown out, with a confidence taken from the token log-probabilities."""

import base64
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx2

# Task prompts from the model cards: GLM-OCR "Text Recognition:", PaddleOCR-VL "OCR:".
PROMPTS: Final = {"glm_ocr": "Text Recognition:", "paddleocr_vl": "OCR:"}
_FENCE: Final = re.compile(r"\A```[a-z]*\n(.*)\n```\Z", re.DOTALL)


class VisionError(Exception):
    """The vision server could not answer."""


@dataclass(frozen=True, slots=True)
class Recognised:
    markdown: str
    confidence: float
    seconds: float
    warning: str | None = None


def _unfence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def _confidence(choice: dict[str, Any]) -> tuple[float, str | None]:
    """The geometric mean of the token probabilities."""
    tokens = (choice.get("logprobs") or {}).get("content") or []
    logprobs = [float(t["logprob"]) for t in tokens]
    if not logprobs:
        return 1.0, "the vision server reported no token probabilities"
    return min(1.0, math.exp(sum(logprobs) / len(logprobs))), None


class VisionClient:
    def __init__(
        self, base_url: str, *, api_key: str | None, timeout_s: float, http: httpx2.AsyncClient
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout_s
        self._http = http

    async def recognise(
        self, image: bytes, mime: str, *, backend: str, model: str, max_tokens: int
    ) -> Recognised:
        started = time.perf_counter()
        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        content = [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": PROMPTS[backend]},
        ]
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "logprobs": True,
        }
        try:
            resp = await self._http.post(
                f"{self._base}/chat/completions",
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            raise VisionError("the OCR model server is unreachable") from exc
        if resp.status_code != 200:
            raise VisionError(f"the OCR model server answered {resp.status_code}")
        try:
            choice = resp.json()["choices"][0]
            text = str(choice["message"]["content"] or "")
            confidence, warning = _confidence(choice)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VisionError("the OCR model server answered outside the OpenAI shape") from exc
        markdown = _unfence(text.strip())
        if choice.get("finish_reason") == "length":
            confidence, warning = min(confidence, 0.5), "the page was cut off at the output limit"
        if not markdown:
            confidence, warning = 0.0, "the OCR model returned no text"
        return Recognised(markdown, confidence, time.perf_counter() - started, warning)
