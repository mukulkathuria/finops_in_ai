"""The moderation backend MODERATION_BACKEND names (US-24)."""

import httpx2

from gateway.adapters.moderation.openai_compat import OpenAICompatModerator
from gateway.adapters.moderation.rules import RulesModerator
from gateway.ports.moderation import Moderator


def build_moderator(
    backend: str,
    *,
    http: httpx2.AsyncClient,
    base_url: str | None,
    api_key: str | None,
    model: str,
    timeout_s: float,
) -> Moderator:
    if backend == "openai_compat":
        if not base_url:
            raise ValueError("MODERATION_BACKEND=openai_compat needs MODERATION_BASE_URL")
        return OpenAICompatModerator(
            base_url, http=http, model=model, api_key=api_key, timeout_s=timeout_s
        )
    return RulesModerator()
