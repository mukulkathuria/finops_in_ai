"""OpenID Connect (US-27): a provider's discovery document and signing keys, the code exchange
and the ID token's signature. The issuer is an org admin's configuration, not the operator's,
so outside development and tests every request here goes to public addresses only
(`ensure_public`); no redirect is followed, and an answer over 1 MiB is refused."""

import base64
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote_plus

import httpx2
import jwt
import orjson

from gateway.adapters.outbound import NotPublic, ensure_public
from gateway.domain.sso import ALGORITHMS, SignIn, check_claims, invalid_token

_MAX_BODY: Final = 1024 * 1024
_REFETCH_S: Final = 60.0  # an unknown key id fetches the key set again, at most once a minute
_FAMILIES: Final = {"RSA": ("RS", "PS"), "EC": ("ES",), "OKP": ("EdDSA",)}


class OidcError(Exception):
    """The provider could not be reached, or answered something unusable."""


@dataclass(frozen=True, slots=True)
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    auth_methods: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Key:
    kid: str | None
    kty: str
    alg: str | None
    use: str | None
    key: Any  # the public key PyJWT verifies with

    def fits(self, kid: str | None, alg: str) -> bool:
        if (kid is not None and self.kid != kid) or self.use not in (None, "sig"):
            return False
        if self.alg is not None:
            return self.alg == alg
        return alg.startswith(_FAMILIES.get(self.kty, ()))


def _keys_of(document: Mapping[str, Any]) -> list[_Key]:
    keys = []
    for raw in document.get("keys") or ():
        if not isinstance(raw, dict):
            continue
        try:
            parsed = jwt.PyJWK(raw)
        except jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError, KeyError:
            continue  # a key this library cannot use is no key for us
        keys.append(
            _Key(raw.get("kid"), str(raw.get("kty")), raw.get("alg"), raw.get("use"), parsed.key)
        )
    return keys


class OidcClient:
    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        allow_insecure: bool,
        timeout_s: float = 10.0,
        ttl_s: float = 3600.0,
    ) -> None:
        self._http = http
        self._insecure = allow_insecure
        self._timeout = timeout_s
        self._ttl = ttl_s
        self._providers: dict[str, tuple[float, Provider]] = {}
        self._keys: dict[str, tuple[float, list[_Key]]] = {}

    async def provider(self, issuer: str) -> Provider:
        """The provider's endpoints from its discovery document (cached for an hour), which must
        name the issuer exactly as configured."""
        cached = self._providers.get(issuer)
        if cached is not None and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        document = await self._json("GET", issuer.rstrip("/") + "/.well-known/openid-configuration")
        if document.get("issuer") != issuer:
            raise OidcError("the discovery document names another issuer")
        methods = document.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        provider = Provider(
            issuer,
            self._endpoint(document, "authorization_endpoint"),
            self._endpoint(document, "token_endpoint"),
            self._endpoint(document, "jwks_uri"),
            tuple(str(m) for m in methods) if isinstance(methods, list) else (),
        )
        self._providers[issuer] = (time.monotonic(), provider)
        return provider

    def _endpoint(self, document: Mapping[str, Any], name: str) -> str:
        url = document.get(name)
        schemes = ("https://", "http://") if self._insecure else ("https://",)
        if not isinstance(url, str) or not url.startswith(schemes) or len(url) > 2048:
            raise OidcError(f"the discovery document has no usable {name}")
        return url

    async def exchange(
        self,
        provider: Provider,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str | None,
        verifier: str,
    ) -> str:
        """Trade the authorization code, with the PKCE verifier, for the ID token. A confidential
        client authenticates as the provider prefers (post or basic); a public one sends its id."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": client_id,
        }
        headers: dict[str, str] = {}
        if client_secret is not None:
            if "client_secret_post" in provider.auth_methods:
                form["client_secret"] = client_secret
            else:  # client_secret_basic, the default: both parts form-encoded first (RFC 6749)
                pair = f"{quote_plus(client_id)}:{quote_plus(client_secret)}".encode()
                headers["authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
        answer = await self._json("POST", provider.token_endpoint, data=form, headers=headers)
        id_token = answer.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcError("the provider answered without an ID token")
        return id_token

    async def verify(
        self, provider: Provider, id_token: str, *, client_id: str, nonce: str
    ) -> SignIn:
        """The ID token's signature against the provider's published keys, then its claims."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as exc:
            raise invalid_token("malformed") from exc
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALGORITHMS:
            raise invalid_token("algorithm")
        key = await self._signing_key(provider, kid if isinstance(kid, str) else None, alg)
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[alg],
                options={
                    "verify_signature": True,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidTokenError as exc:
            raise invalid_token("signature") from exc
        return check_claims(
            claims, issuer=provider.issuer, client_id=client_id, nonce=nonce, now=time.time()
        )

    async def _signing_key(self, provider: Provider, kid: str | None, alg: str) -> Any:
        for refresh in (False, True):  # a rotated key: fetch the set again, then give up
            for key in await self._key_set(provider, refresh=refresh):
                if key.fits(kid, alg):
                    return key.key
        raise invalid_token("unknown signing key")

    async def _key_set(self, provider: Provider, *, refresh: bool) -> list[_Key]:
        cached = self._keys.get(provider.jwks_uri)
        now = time.monotonic()
        if cached is not None:
            age = now - cached[0]
            if age < self._ttl and not (refresh and age >= _REFETCH_S):
                return cached[1]
        keys = _keys_of(await self._json("GET", provider.jwks_uri))
        self._keys[provider.jwks_uri] = (now, keys)
        return keys

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if not self._insecure:
            try:
                await ensure_public(url)
            except (NotPublic, ConnectionError) as exc:
                raise OidcError(str(exc)) from exc
        try:
            response = await self._http.request(
                method, url, timeout=self._timeout, follow_redirects=False, **kwargs
            )
        except httpx2.HTTPError as exc:
            raise OidcError(f"the provider cannot be reached ({type(exc).__name__})") from exc
        if len(response.content) > _MAX_BODY:
            raise OidcError("the provider's answer is too large")
        if response.status_code != 200:
            raise OidcError(f"the provider answered {response.status_code}")
        try:
            data = orjson.loads(response.content)
        except orjson.JSONDecodeError as exc:
            raise OidcError("the provider's answer is not JSON") from exc
        if not isinstance(data, dict):
            raise OidcError("the provider's answer is not a JSON object")
        return data
