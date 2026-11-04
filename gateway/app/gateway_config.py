"""Load config/gateway.yaml into typed, frozen configuration (validated at startup)."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from gateway.domain.providers import ProviderConfig


class _Provider(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["openai_compat", "anthropic", "gemini"]
    base_url: str = Field(pattern=r"^https?://")
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    timeout_s: float = Field(default=120.0, gt=0, le=600)
    adapter: Literal["native", "litellm"] | None = None


class _Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    provider_retries: int = Field(default=1, ge=0, le=5)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    providers: dict[str, _Provider] = Field(default_factory=dict)
    defaults: _Defaults = _Defaults()

    def provider_configs(
        self, default_adapter: Literal["native", "litellm"] = "native"
    ) -> dict[str, ProviderConfig]:
        return {
            name: ProviderConfig(
                name=name,
                kind=p.kind,
                base_url=p.base_url,
                api_key_env=p.api_key_env,
                max_tokens_field=p.max_tokens_field,
                timeout_s=p.timeout_s,
                # The default reaches only the providers LiteLLM can serve; an explicit
                # choice is kept and fails at startup if it cannot be served (ADR 0008).
                adapter=p.adapter or (default_adapter if p.kind == "openai_compat" else "native"),
            )
            for name, p in self.providers.items()
        }


def load_gateway_config(path: Path) -> GatewayConfig:
    if not path.exists():
        return GatewayConfig()
    return GatewayConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
