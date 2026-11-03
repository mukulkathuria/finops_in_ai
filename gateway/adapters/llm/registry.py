"""Adapter registry: one line per backend (spec section 3, pattern step 3)."""

from collections.abc import Callable, Mapping

import httpx2

from gateway.adapters.llm.anthropic import AnthropicAdapter
from gateway.adapters.llm.gemini import GeminiAdapter
from gateway.adapters.llm.openai_compat import OpenAICompatAdapter
from gateway.domain.providers import ProviderConfig
from gateway.ports.llm import LLMAdapter

Factory = Callable[[ProviderConfig, str, httpx2.AsyncClient], LLMAdapter]

FACTORIES: dict[str, Factory] = {
    "openai_compat": lambda c, key, http: OpenAICompatAdapter(
        base_url=c.base_url,
        api_key=key,
        http=http,
        name=c.name,
        timeout_s=c.timeout_s,
        max_tokens_field=c.max_tokens_field,
    ),
    "anthropic": lambda c, key, http: AnthropicAdapter(
        api_key=key, http=http, base_url=c.base_url, name=c.name, timeout_s=c.timeout_s
    ),
    "gemini": lambda c, key, http: GeminiAdapter(
        api_key=key, http=http, base_url=c.base_url, name=c.name, timeout_s=c.timeout_s
    ),
}


def build_adapters(
    configs: Mapping[str, ProviderConfig], environ: Mapping[str, str], http: httpx2.AsyncClient
) -> dict[str, LLMAdapter]:
    """Instantiate every configured provider whose API key is present in the environment."""
    adapters: dict[str, LLMAdapter] = {}
    for name, config in configs.items():
        key = environ.get(config.api_key_env, "").strip()
        if key:
            adapters[name] = FACTORIES[config.kind](config, key, http)
    return adapters
