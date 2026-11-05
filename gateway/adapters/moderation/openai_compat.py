"""An OpenAI-compatible moderation API (`POST {base}/moderations`) for image prompts (US-24).
Anything but a readable verdict is ModerationUnavailable: image requests fail closed."""

from typing import Any

import httpx2
import orjson

from gateway.domain.images import Moderation, ModerationUnavailable


class OpenAICompatModerator:
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        *,
        http: httpx2.AsyncClient,
        model: str,
        api_key: str | None,
        timeout_s: float,
    ) -> None:
        self._url = base_url.rstrip("/") + "/moderations"
        self._http = http
        self._model = model
        self._headers = {"content-type": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"
        self._timeout = timeout_s

    async def check(self, text: str) -> Moderation:
        body = orjson.dumps({"model": self._model, "input": text})
        try:
            resp = await self._http.post(
                self._url, content=body, headers=self._headers, timeout=self._timeout
            )
        except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
            raise ModerationUnavailable("the moderation service could not be reached") from exc
        if resp.status_code >= 400:
            raise ModerationUnavailable(f"the moderation service answered {resp.status_code}")
        try:
            result: dict[str, Any] = orjson.loads(resp.content)["results"][0]
            flagged = bool(result["flagged"])
            categories = result.get("categories") or {}
            named = tuple(sorted(str(k) for k, v in categories.items() if v is True))
        except (orjson.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModerationUnavailable("the moderation service's answer is unreadable") from exc
        return Moderation(flagged, named if flagged else ())
