"""Requests to hosts a tenant chose (webhook receivers, single sign-on providers) go to public
addresses only (spec section 10, SSRF): the host is resolved and refused when any address is
loopback, private, link-local (cloud metadata), shared or reserved. Production-like
environments always check; development and tests may point at local services."""

import asyncio
import socket
from urllib.parse import urlsplit

from gateway.domain.webhooks import is_public_address


class NotPublic(Exception):
    """The host resolves to no address, or to one that is not public."""


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def ensure_public(url: str) -> None:
    """Raise NotPublic unless every address the URL's host resolves to is public. A failed
    lookup is a ConnectionError: it may succeed later."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await resolve(host, port)
    except OSError as exc:
        raise ConnectionError(f"cannot resolve {host}") from exc
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise NotPublic(f"{host} resolves to a non-public address")
