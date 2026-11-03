"""Provider configuration (non-secret structure from config/gateway.yaml, ADR 0005)."""

from dataclasses import dataclass
from typing import Literal

AdapterKind = Literal["openai_compat", "anthropic", "gemini"]


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    kind: AdapterKind
    base_url: str
    api_key_env: str
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    timeout_s: float = 120.0
