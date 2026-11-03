"""Webhook URL policy (SSRF) and signatures."""

import hashlib
import hmac

import pytest

from gateway.domain.errors import InvalidInput
from gateway.domain.webhooks import (
    is_public_address,
    new_webhook_secret,
    sign,
    url_hint,
    validate_webhook_url,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # shared address space
        "0.0.0.0",  # noqa: S104 - an input under test
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "not-an-ip",
    ],
)
def test_internal_addresses_are_never_public(address: str) -> None:
    assert not is_public_address(address)


def test_public_addresses_are_public() -> None:
    assert is_public_address("93.184.215.14")
    assert is_public_address("2606:4700:4700::1111")


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",  # clear text
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/hook",
        "https://intranet.internal/hook",
        "https://user:pw@hooks.example.com/x",
        "https:///nohost",
        "ftp://hooks.example.com/x",
        "https://hooks.example.com/x#frag",
        "https://hooks.example.com:99999/x",
    ],
)
def test_production_rejects_unsafe_webhook_urls(url: str) -> None:
    with pytest.raises(InvalidInput):
        validate_webhook_url(url, allow_insecure=False)


def test_safe_urls_pass_and_dev_may_use_local_receivers() -> None:
    assert validate_webhook_url(" https://hooks.example.com/T/B/x?y=1 ", allow_insecure=False) == (
        "https://hooks.example.com/T/B/x?y=1"
    )
    assert validate_webhook_url("http://127.0.0.1:9000/hook", allow_insecure=True)


def test_hints_never_show_paths_or_queries() -> None:
    assert url_hint("https://hooks.example.com/services/T00/B00/secret?x=1") == (
        "https://hooks.example.com/…"
    )


def test_signatures_verify_with_the_secret_only() -> None:
    secret = new_webhook_secret()
    assert secret.startswith("whsec_")
    body = b'{"type":"budget.soft_threshold"}'
    expected = hmac.new(secret.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert sign(secret, 1_700_000_000, body) == f"v1={expected}"
    assert sign(new_webhook_secret(), 1_700_000_000, body) != sign(secret, 1_700_000_000, body)
