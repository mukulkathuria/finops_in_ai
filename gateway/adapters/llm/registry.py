"""Adapter registry: one line per backend (spec section 3, pattern step 3)."""

import importlib.util
import time
from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

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


def _litellm_installed() -> bool:
    return importlib.util.find_spec("litellm") is not None


def _check(name: str, config: ProviderConfig) -> None:
    """ADR 0008: the litellm adapter is an optional extra and never a silent fallback. A
    provider that selects it fails at startup unless it can be served, key or no key."""
    if config.adapter != "litellm":
        return
    if config.kind != "openai_compat":
        raise RuntimeError(
            f"provider {name!r} selects the litellm adapter, which serves openai_compat providers"
        )
    if not _litellm_installed():
        raise RuntimeError(
            f"provider {name!r} selects the litellm adapter; install finops-gateway[litellm]"
        )


def build_adapter(config: ProviderConfig, key: str, http: httpx2.AsyncClient) -> LLMAdapter:
    """The adapter a provider is configured with: native, or LiteLLM when selected."""
    if config.adapter == "litellm":
        from gateway.adapters.llm.litellm_adapter import LiteLLMAdapter  # the optional extra

        return LiteLLMAdapter(
            base_url=config.base_url,
            api_key=key,
            http=http,
            name=f"litellm:{config.name}",
            timeout_s=config.timeout_s,
            max_tokens_field=config.max_tokens_field,
        )
    return FACTORIES[config.kind](config, key, http)


def build_adapters(
    configs: Mapping[str, ProviderConfig], environ: Mapping[str, str], http: httpx2.AsyncClient
) -> dict[str, LLMAdapter]:
    """Instantiate every configured provider whose API key is present in the environment."""
    adapters: dict[str, LLMAdapter] = {}
    for name, config in configs.items():
        _check(name, config)
        key = environ.get(config.api_key_env, "").strip()
        if key:
            adapters[name] = build_adapter(config, key, http)
    return adapters


class OrgAdapters:
    """Adapters built from an org's own provider keys, cached briefly (key changes apply within
    `ttl_seconds`); the decrypted keys live only inside these adapter objects."""

    def __init__(
        self,
        lookup: Callable[[UUID], Awaitable[dict[str, tuple[UUID, str]]]],
        configs: Mapping[str, ProviderConfig],
        http: httpx2.AsyncClient,
        ttl_seconds: float = 30.0,
    ) -> None:
        self._lookup = lookup
        self._configs = configs
        self._http = http
        self._ttl = ttl_seconds
        self._cache: dict[UUID, tuple[float, dict[str, LLMAdapter]]] = {}

    async def get(self, org_id: UUID) -> dict[str, LLMAdapter]:
        hit = self._cache.get(org_id)
        if hit is not None and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        keys = await self._lookup(org_id)
        adapters = {
            provider: build_adapter(self._configs[provider], key, self._http)
            for provider, (_, key) in keys.items()
            if provider in self._configs
        }
        if len(self._cache) > 10_000:  # bounded memory under many tenants
            self._cache.clear()
        self._cache[org_id] = (time.monotonic(), adapters)
        return adapters
