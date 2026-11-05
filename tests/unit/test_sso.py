"""US-27: the OpenID Connect pieces.

- PKCE matches RFC 7636's example.
- The authorization request carries state, nonce and the S256 challenge.
- An ID token is refused for a wrong issuer, audience, party, time, nonce or subject, and for
  any key or algorithm the provider did not publish (`alg: none`, HS256 with the client secret,
  a foreign key).
- The client reads the provider through its discovery document and exchanges the code with
  PKCE and its credentials.
- A provider on a private address is refused outside dev and test."""

import base64
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
import orjson
import pytest

from gateway.adapters.sso.oidc import OidcClient, OidcError
from gateway.domain.errors import InvalidInput, Unauthenticated
from gateway.domain.sso import (
    SignIn,
    authorization_url,
    challenge_of,
    check_claims,
    clean_domains,
    domain_allowed,
    issuer_url,
    new_pkce,
)
from tests.support import fake_idp

NOW = 1_800_000_000.0
CLAIMS = {
    "iss": "https://idp.example",
    "aud": "client",
    "sub": "user-1",
    "nonce": "n1",
    "iat": NOW,
    "exp": NOW + 300,
    "email": "Ada@Example.com",
    "email_verified": True,
}
CALLBACK = "https://gateway.example/v1/auth/sso/callback"


def _check(claims: dict[str, object]) -> SignIn:
    return check_claims(
        claims, issuer="https://idp.example", client_id="client", nonce="n1", now=NOW
    )


def test_pkce_follows_rfc_7636() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # its Appendix B
    assert challenge_of(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    pkce = new_pkce()
    assert 43 <= len(pkce.verifier) <= 128
    assert pkce.challenge == challenge_of(pkce.verifier)
    assert new_pkce().verifier != pkce.verifier


def test_the_authorization_request_carries_state_nonce_and_the_challenge() -> None:
    url = authorization_url(
        "https://idp.example/authorize?prompt=login",
        client_id="client",
        redirect_uri=CALLBACK,
        state="s1",
        nonce="n1",
        challenge="c1",
    )
    query = parse_qs(urlsplit(url).query)
    assert query == {
        "prompt": ["login"],
        "response_type": ["code"],
        "client_id": ["client"],
        "redirect_uri": [CALLBACK],
        "scope": ["openid email profile"],
        "state": ["s1"],
        "nonce": ["n1"],
        "code_challenge": ["c1"],
        "code_challenge_method": ["S256"],
    }


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"aud": "someone-else"}, "audience"),
        ({"aud": ["client", "other"]}, "authorized party"),
        ({"azp": "other"}, "authorized party"),
        ({"exp": NOW - 600}, "expired"),
        ({"exp": "soon"}, "expired"),
        ({"iat": NOW + 600}, "issued in the future"),
        ({"nbf": NOW + 600}, "not yet valid"),
        ({"nonce": "n2"}, "nonce"),
        ({"sub": ""}, "subject"),
    ],
)
def test_an_id_token_must_say_the_right_things(change: dict[str, object], reason: str) -> None:
    with pytest.raises(Unauthenticated, match=reason) as refused:
        _check(CLAIMS | change)
    assert refused.value.code == "sso_invalid_token"


def test_claims_that_pass_give_the_subject_and_a_normalised_email() -> None:
    both = _check(CLAIMS | {"aud": ["client", "other"], "azp": "client"})
    assert both == SignIn("user-1", "ada@example.com", True)
    assert _check(CLAIMS | {"email_verified": "false"}).email_verified is False
    assert _check({k: v for k, v in CLAIMS.items() if k != "email"}).email is None


def test_domains_and_issuers_are_checked() -> None:
    assert clean_domains([" Example.COM ", "@corp.example", ""]) == ["corp.example", "example.com"]
    with pytest.raises(InvalidInput):
        clean_domains(["not a domain"])
    assert domain_allowed("a@example.com", ["example.com"])
    assert not domain_allowed("a@evil.example", ["example.com"])
    assert domain_allowed("a@anything.example", [])
    issuer = "https://login.example/tenant/"
    assert issuer_url(f" {issuer} ", allow_insecure=False) == issuer  # kept exactly
    for bad in (
        "http://login.example",
        "https://login.example/?a=1",
        "https://login.example/#x",
        "https://user:pw@login.example",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://localhost",
        "https://idp.internal",
        "ftp://login.example",
    ):
        with pytest.raises(InvalidInput):
            issuer_url(bad, allow_insecure=False)
    assert issuer_url("http://idp.test", allow_insecure=True) == "http://idp.test"


def _client() -> OidcClient:
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake_idp.app))
    return OidcClient(http, allow_insecure=True)


def _unsigned(claims: dict[str, object]) -> str:
    def part(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(orjson.dumps(value)).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}."


async def test_the_client_signs_in_through_a_provider_and_refuses_forgeries() -> None:
    oidc = _client()
    provider = await oidc.provider(fake_idp.ISSUER)
    assert provider.token_endpoint == f"{fake_idp.ISSUER}/token"
    pkce = new_pkce()
    fake_idp.arrange(
        "code-1",
        nonce="n1",
        challenge=pkce.challenge,
        redirect_uri=CALLBACK,
        subject="u1",
        email="ada@example.com",
    )
    exchange = {
        "redirect_uri": CALLBACK,
        "client_id": fake_idp.CLIENT_ID,
        "client_secret": fake_idp.CLIENT_SECRET,
    }
    token = await oidc.exchange(provider, code="code-1", verifier=pkce.verifier, **exchange)
    signed_in = await oidc.verify(provider, token, client_id=fake_idp.CLIENT_ID, nonce="n1")
    assert signed_in == SignIn("u1", "ada@example.com", True)

    fake_idp.arrange(
        "code-2", nonce="n1", challenge=pkce.challenge, redirect_uri=CALLBACK, subject="u1"
    )
    with pytest.raises(OidcError, match="400"):  # a verifier that does not match the challenge
        await oidc.exchange(provider, code="code-2", verifier="x" * 64, **exchange)

    now = int(time.time())
    claims = {
        "iss": fake_idp.ISSUER,
        "aud": fake_idp.CLIENT_ID,
        "sub": "u1",
        "nonce": "n1",
        "iat": now,
        "exp": now + 300,
    }
    forged = jwt.encode(
        claims, fake_idp.OTHER_KEY, algorithm="RS256", headers={"kid": fake_idp.KID}
    )
    unknown = jwt.encode(claims, fake_idp.KEY, algorithm="RS256", headers={"kid": "rotated-away"})
    shared = jwt.encode(claims, fake_idp.CLIENT_SECRET, algorithm="HS256")
    for bad in (forged, unknown, shared, _unsigned(claims), "not.a.token"):
        with pytest.raises(Unauthenticated):
            await oidc.verify(provider, bad, client_id=fake_idp.CLIENT_ID, nonce="n1")


async def test_a_provider_on_a_private_address_is_refused_outside_dev() -> None:
    def never(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"no request may reach {request.url}")

    oidc = OidcClient(
        httpx2.AsyncClient(transport=httpx2.MockTransport(never)), allow_insecure=False
    )
    for issuer in ("https://127.0.0.1", "https://localhost"):
        with pytest.raises(OidcError, match="non-public"):
            await oidc.provider(issuer)
