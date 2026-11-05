"""A fake OpenID Connect provider for tests (US-27): discovery, a key set, and a token endpoint
that answers a code a test arranged with the ID token it asked for. It checks the client's
credentials (basic) and PKCE as a real provider does. Mounted at https://idp.test on the app's
HTTP client (tests/support/app.py)."""

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote_plus

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ISSUER = "https://idp.test"
CLIENT_ID = "finops-gateway"
CLIENT_SECRET = "-".join(("idp", "client", "credential"))  # a test value, assembled at runtime
KID = "idp-key-1"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # never published


@dataclass
class _Grant:
    challenge: str
    redirect_uri: str
    claims: dict[str, Any]
    key: Any


_grants: dict[str, _Grant] = {}
app = FastAPI()


def arrange(
    code: str,
    *,
    nonce: str,
    challenge: str,
    redirect_uri: str,
    subject: str,
    email: str | None = None,
    email_verified: bool = True,
    key: Any = None,
    **claims: Any,
) -> None:
    """What the token endpoint answers for `code`: an ID token with these claims (issuer,
    audience and times filled in unless given), signed with KEY unless another is given."""
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": subject,
        "nonce": nonce,
        "iat": now,
        "exp": now + 300,
    }
    if email is not None:
        base |= {"email": email, "email_verified": email_verified}
    _grants[code] = _Grant(challenge, redirect_uri, base | claims, key or KEY)


@app.get("/.well-known/openid-configuration")
async def discovery() -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }


@app.get("/jwks")
async def jwks() -> dict[str, Any]:
    public = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    return {"keys": [{**public, "kid": KID, "alg": "RS256", "use": "sig"}]}


@app.post("/token")
async def token(request: Request) -> JSONResponse:
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if form.get("grant_type") != "authorization_code":
        return _error("unsupported_grant_type")
    if not _client_ok(request.headers.get("authorization")):
        return _error("invalid_client", 401)
    grant = _grants.pop(form.get("code", ""), None)
    if grant is None or form.get("redirect_uri") != grant.redirect_uri:
        return _error("invalid_grant")
    verifier = form.get("code_verifier", "")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    if challenge.rstrip(b"=").decode() != grant.challenge:
        return _error("invalid_grant")
    id_token = jwt.encode(grant.claims, grant.key, algorithm="RS256", headers={"kid": KID})
    return JSONResponse(
        {"access_token": "opaque", "token_type": "Bearer", "expires_in": 300, "id_token": id_token}
    )


def _client_ok(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False
    client_id, _, secret = base64.b64decode(authorization[6:]).decode().partition(":")
    return unquote_plus(client_id) == CLIENT_ID and unquote_plus(secret) == CLIENT_SECRET


def _error(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)
