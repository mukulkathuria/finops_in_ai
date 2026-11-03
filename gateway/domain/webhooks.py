"""Outgoing webhooks (spec sections 4 and 10): which URLs may receive events, and how a receiver
verifies that an event came from this gateway.

Signature: `X-FinOps-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>` with the
Unix timestamp in `X-FinOps-Timestamp`; receivers reject old timestamps to stop replays.
"""

import hashlib
import hmac
import ipaddress
import secrets
from typing import Final
from urllib.parse import urlsplit

from gateway.domain.errors import InvalidInput

MAX_URL_LENGTH: Final = 2048
_SECRET_PREFIX: Final = "whsec_"  # noqa: S105  # nosec B105 - a prefix, not a secret


def new_webhook_secret() -> str:
    return _SECRET_PREFIX + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def is_public_address(address: str) -> bool:
    """True for globally routable unicast addresses only: never loopback, private, link-local
    (cloud metadata), shared, multicast or reserved, including IPv4 mapped into IPv6."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_webhook_url(url: str, *, allow_insecure: bool) -> str:
    """Reject URLs that could reach internal services (SSRF) or leak events in clear text.
    `allow_insecure` (dev and test only) permits plain http and private addresses."""
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise InvalidInput(
            "the webhook URL is empty or too long", code="invalid_webhook_url", param="url"
        )
    parts = urlsplit(url)
    if parts.scheme not in (("https", "http") if allow_insecure else ("https",)):
        raise InvalidInput(
            "the webhook URL must use https", code="invalid_webhook_url", param="url"
        )
    if not parts.hostname or parts.username or parts.password:
        raise InvalidInput(
            "the webhook URL needs a host and must not carry credentials",
            code="invalid_webhook_url",
            param="url",
        )
    if parts.fragment:
        raise InvalidInput(
            "the webhook URL must not have a fragment", code="invalid_webhook_url", param="url"
        )
    host = parts.hostname
    literal = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        literal = False
    if not allow_insecure and (
        (literal and not is_public_address(host))
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        raise InvalidInput(
            "the webhook URL must point to a public host", code="invalid_webhook_url", param="url"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidInput(
            "the webhook URL has an invalid port", code="invalid_webhook_url", param="url"
        ) from exc
    del port
    return url


def url_hint(url: str) -> str:
    """What may be shown or logged: scheme and host only (paths and queries can hold tokens)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}/…"
