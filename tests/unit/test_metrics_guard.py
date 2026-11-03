"""/metrics access: a matching token when one is set; dev and test only otherwise."""

from secrets import token_urlsafe

import pytest
from pydantic import SecretStr

from gateway.api.metrics import ensure_scrape_allowed
from gateway.app.settings import Settings
from gateway.domain.errors import NotFound, Unauthenticated


def _settings(environment: str, token: str | None = None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=environment,
        database_url=SecretStr("postgresql://app:" + token_urlsafe(8) + "@db/finops"),
        jwt_signing_key=SecretStr(token_urlsafe(32)),
        master_key=SecretStr(token_urlsafe(32)),
        metrics_token=SecretStr(token) if token else None,
    )


def test_dev_and_test_serve_metrics_without_a_token() -> None:
    ensure_scrape_allowed(_settings("dev"), None)
    ensure_scrape_allowed(_settings("test"), None)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_production_hides_metrics_without_a_token(environment: str) -> None:
    with pytest.raises(NotFound):
        ensure_scrape_allowed(_settings(environment), None)


def test_a_configured_token_must_match() -> None:
    token = token_urlsafe(16)
    settings = _settings("production", token)
    ensure_scrape_allowed(settings, f"Bearer {token}")
    for wrong in (None, "Bearer nope", f"Basic {token}"):
        with pytest.raises(Unauthenticated):
            ensure_scrape_allowed(settings, wrong)
