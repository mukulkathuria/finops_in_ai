"""Envelope encryption (US-06): AES-256-GCM, per-secret data keys, context binding, rotation."""

import dataclasses
import os

import httpx2
import pytest

from gateway.adapters.llm import registry
from gateway.adapters.llm.registry import build_adapters
from gateway.adapters.security.envelope import (
    DecryptionError,
    Envelope,
    LocalKeyManager,
    parse_master_keys,
)
from gateway.adapters.security.tokens import b64url_encode
from gateway.app.gateway_config import GatewayConfig
from gateway.domain.providers import ProviderConfig

AAD = b"finops:provider-key:org-a:openai:row-1"


def _envelope(keys: dict[str, bytes] | None = None, active: str = "m1") -> Envelope:
    return Envelope(LocalKeyManager(keys or {"m1": os.urandom(32)}, active))


def test_round_trip_and_fresh_data_key_per_secret() -> None:
    env = _envelope()
    a, b = env.seal("sk-secret-value", AAD), env.seal("sk-secret-value", AAD)
    assert env.open(a, AAD) == "sk-secret-value"
    assert a.ciphertext != b.ciphertext
    assert a.wrapped_key != b.wrapped_key
    assert b"sk-secret-value" not in a.ciphertext + a.wrapped_key


@pytest.mark.parametrize("part", ["ciphertext", "wrapped_key"])
def test_tampering_is_detected(part: str) -> None:
    env = _envelope()
    sealed = env.seal("sk-secret-value", AAD)
    raw = bytearray(getattr(sealed, part))
    raw[-1] ^= 1
    broken = dataclasses.replace(sealed, **{part: bytes(raw)})
    with pytest.raises(DecryptionError):
        env.open(broken, AAD)


def test_ciphertext_is_bound_to_its_org_provider_and_row() -> None:
    env = _envelope()
    sealed = env.seal("sk-secret-value", AAD)
    with pytest.raises(DecryptionError):
        env.open(sealed, b"finops:provider-key:org-b:openai:row-1")


def test_rotation_keeps_old_secrets_readable() -> None:
    old, new = os.urandom(32), os.urandom(32)
    sealed = _envelope({"m1": old}, "m1").seal("sk-old", AAD)
    rotated = _envelope({"m1": old, "m2": new}, "m2")
    assert rotated.open(sealed, AAD) == "sk-old"
    assert rotated.seal("sk-new", AAD).key_id == "m2"
    with pytest.raises(DecryptionError, match="not configured"):
        _envelope({"m2": new}, "m2").open(sealed, AAD)


def test_master_key_parsing_validates_lengths() -> None:
    good = b64url_encode(os.urandom(32))
    assert set(parse_master_keys(good, "m2", f"m1:{good}")) == {"m1", "m2"}
    with pytest.raises(ValueError, match="32-byte"):
        parse_master_keys(b64url_encode(b"short"), "m1", "")
    with pytest.raises(ValueError, match="kid:base64url"):
        parse_master_keys(good, "m1", "nocolon")


LITELLM_OPENAI = ProviderConfig(
    "openai", "openai_compat", "https://api.openai.com/v1", "OPENAI_API_KEY", adapter="litellm"
)


def test_selecting_litellm_fails_closed_where_it_cannot_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = ProviderConfig(
        "anthropic",
        "anthropic",
        "https://api.anthropic.com",
        "ANTHROPIC_API_KEY",
        adapter="litellm",
    )
    with pytest.raises(RuntimeError, match="openai_compat"):
        build_adapters({"anthropic": claude}, {}, httpx2.AsyncClient())  # even without a key
    monkeypatch.setattr(registry, "_litellm_installed", lambda: False)  # a build without the extra
    with pytest.raises(RuntimeError, match=r"finops-gateway\[litellm\]"):
        build_adapters({"openai": LITELLM_OPENAI}, {}, httpx2.AsyncClient())


def test_selecting_litellm_builds_it_for_an_openai_compatible_provider() -> None:
    pytest.importorskip("litellm")
    keys = {"OPENAI_API_KEY": "k"}
    built = build_adapters({"openai": LITELLM_OPENAI}, keys, httpx2.AsyncClient())
    assert built["openai"].name == "litellm:openai"


def test_the_litellm_default_reaches_only_the_providers_it_can_serve() -> None:
    config = GatewayConfig.model_validate(
        {
            "providers": {
                "openai": {
                    "kind": "openai_compat",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
                "anthropic": {
                    "kind": "anthropic",
                    "base_url": "https://api.anthropic.com",
                    "api_key_env": "ANTHROPIC_API_KEY",
                },
                "pinned": {
                    "kind": "openai_compat",
                    "base_url": "https://llm.example.test/v1",
                    "api_key_env": "PINNED_KEY",
                    "adapter": "native",
                },
            }
        }
    )
    chosen = {name: c.adapter for name, c in config.provider_configs("litellm").items()}
    assert chosen == {"openai": "litellm", "anthropic": "native", "pinned": "native"}
