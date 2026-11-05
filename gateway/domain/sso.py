"""Single sign-on (US-27): the OpenID Connect rules that need no I/O.

- PKCE (RFC 7636): a verifier per sign-in, sent to the provider as its S256 challenge.
- The authorization request (OIDC Core 3.1.2.1): the code flow with `openid email profile`, a
  state, a nonce and the challenge.
- What an ID token must say before anyone is signed in (OIDC Core 3.1.3.7): its issuer,
  audience and authorized party, its times, the nonce and a subject. The adapter checks the
  signature first.
- Which emails a first sign-in may link: verified, in a domain the org allows.
"""

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeGuard
from urllib.parse import urlencode, urlsplit

from gateway.domain.errors import InvalidInput, Unauthenticated
from gateway.domain.identity import normalize_email
from gateway.domain.webhooks import is_public_address

SCOPES: Final = "openid email profile"
STATE_TTL_S: Final = 600  # a sign-in started must come back within ten minutes
CODE_TTL_S: Final = 60  # the web app trades its one-time code at once
MAX_SKEW_S: Final = 120  # clock difference allowed with the provider
MAX_ISSUER_LENGTH: Final = 500
# Asymmetric signatures only: an HMAC token (keyed with a shared secret) or `none` never passes.
ALGORITHMS: Final = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
_DOMAIN: Final = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


@dataclass(frozen=True, slots=True)
class Pkce:
    verifier: str
    challenge: str


def challenge_of(verifier: str) -> str:
    """The S256 code challenge: base64url(sha256(verifier)), unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(64)  # 86 characters: RFC 7636 asks for 43 to 128
    return Pkce(verifier, challenge_of(verifier))


def authorization_url(
    endpoint: str, *, client_id: str, redirect_uri: str, state: str, nonce: str, challenge: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"


@dataclass(frozen=True, slots=True)
class SignIn:
    """Who the provider says signed in."""

    subject: str
    email: str | None
    email_verified: bool


def invalid_token(reason: str) -> Unauthenticated:
    return Unauthenticated(
        f"the provider's ID token is not valid ({reason})", code="sso_invalid_token"
    )


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def check_claims(
    claims: Mapping[str, Any], *, issuer: str, client_id: str, nonce: str, now: float
) -> SignIn:
    """An ID token's claims, once its signature is checked (OIDC Core 3.1.3.7, steps 2-11)."""
    if claims.get("iss") != issuer:
        raise invalid_token("issuer")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or client_id not in audiences:
        raise invalid_token("audience")
    party = claims.get("azp")
    if (len(audiences) > 1 or party is not None) and party != client_id:
        raise invalid_token("authorized party")
    expires, issued = claims.get("exp"), claims.get("iat")
    if not _number(expires) or expires + MAX_SKEW_S < now:
        raise invalid_token("expired")
    if not _number(issued) or issued - MAX_SKEW_S > now:
        raise invalid_token("issued in the future")
    starts = claims.get("nbf")
    if starts is not None and (not _number(starts) or starts - MAX_SKEW_S > now):
        raise invalid_token("not yet valid")
    said = claims.get("nonce")
    if not isinstance(said, str) or not hmac.compare_digest(said.encode(), nonce.encode()):
        raise invalid_token("nonce")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
        raise invalid_token("subject")
    return SignIn(
        subject, _email(claims.get("email")), claims.get("email_verified") in (True, "true")
    )


def _email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return normalize_email(value)
    except InvalidInput:
        return None


def clean_domains(domains: Sequence[str]) -> list[str]:
    """The email domains a first sign-in may link, lower-case and checked."""
    cleaned = sorted({d.strip().lower().lstrip("@") for d in domains if d.strip()})
    for domain in cleaned:
        if not _DOMAIN.fullmatch(domain):
            raise InvalidInput(
                f"{domain!r} is not a domain name", code="invalid_domain", param="allowed_domains"
            )
    return cleaned


def domain_allowed(email: str, domains: Sequence[str]) -> bool:
    """Whether a first sign-in may link by this email: its domain must be one the org named.
    Naming none links nothing, so the provider's word that an email is verified is never
    enough on its own (some providers assert it for addresses their users simply typed)."""
    return email.rpartition("@")[2] in domains


def issuer_url(value: str, *, allow_insecure: bool) -> str:
    """An issuer identifier (OIDC Discovery 2): an https URL with no query or fragment, kept
    exactly as given (the discovery document must repeat it), on a public host unless the
    environment is for development or tests."""
    issuer = value.strip()
    parts = urlsplit(issuer)
    if not issuer or len(issuer) > MAX_ISSUER_LENGTH:
        raise _bad_issuer("the issuer is empty or too long")
    if parts.scheme not in (("https", "http") if allow_insecure else ("https",)):
        raise _bad_issuer("the issuer must be an https URL")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise _bad_issuer("the issuer needs a host, and no credentials, query or fragment")
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    local = parts.hostname == "localhost" or parts.hostname.endswith(
        (".localhost", ".local", ".internal")
    )
    if not allow_insecure and (
        local or (literal is not None and not is_public_address(str(literal)))
    ):
        raise _bad_issuer("the issuer must be on a public host")
    return issuer


def _bad_issuer(message: str) -> InvalidInput:
    return InvalidInput(message, code="invalid_issuer", param="issuer")
