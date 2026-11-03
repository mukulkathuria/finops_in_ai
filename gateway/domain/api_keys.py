"""API key format (US-04): `fo_<8-char visible prefix>_<secret>`; only a SHA-256 digest is kept."""

import re
import secrets
import string
from typing import Final

from gateway.domain.secrets import digest

KEY_PREFIX: Final = "fo_"
_ALPHANUM: Final = string.ascii_letters + string.digits
_FORMAT: Final = re.compile(r"^fo_([A-Za-z0-9]{8})_([A-Za-z0-9_-]{40,64})$")


def new_api_key() -> tuple[str, str]:
    """Return (full key shown once, visible prefix)."""
    prefix = "".join(secrets.choice(_ALPHANUM) for _ in range(8))
    return f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}", prefix


def looks_like_api_key(raw: str) -> bool:
    return raw.startswith(KEY_PREFIX)


def api_key_digest(raw: str) -> str | None:
    """Digest of a well-formed key, or None so malformed input never reaches the database."""
    return digest(raw) if _FORMAT.fullmatch(raw) else None
