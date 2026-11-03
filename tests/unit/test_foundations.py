"""Phase 0 foundations: settings safety, URL handling, log redaction, env bootstrap."""

import logging

import orjson
import pytest
from pydantic import SecretStr, ValidationError

from gateway.adapters.postgres.database import TenantContext, to_sqlalchemy_url
from gateway.app.logging import JsonFormatter, redact
from gateway.app.settings import Environment, Settings
from scripts.init_env import ROLE_PASSWORD_KEYS, render


def _settings(**kw: object) -> Settings:
    kw.setdefault("database_url", SecretStr("postgresql://app:pw@db/finops"))
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_production_refuses_placeholder_database_url() -> None:
    with pytest.raises(ValidationError, match="CHANGE_ME"):
        _settings(
            environment=Environment.PRODUCTION,
            database_url=SecretStr("postgresql://a:CHANGE_ME@h/d"),
        )


def test_dev_accepts_placeholders_and_hides_secret_in_repr() -> None:
    s = _settings(
        environment=Environment.DEV, database_url=SecretStr("postgresql://a:CHANGE_ME@h/d")
    )
    assert "CHANGE_ME" not in repr(s)
    assert not s.is_production_like


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ],
)
def test_database_url_selects_psycopg(url: str, expected: str) -> None:
    assert to_sqlalchemy_url(url) == expected


@pytest.mark.parametrize("url", ["mysql://u:p@h/d", "h:5432/d", "sqlite:///x.db"])
def test_database_url_rejects_other_schemes(url: str) -> None:
    with pytest.raises(ValueError, match="postgresql"):
        to_sqlalchemy_url(url)


def test_tenant_context_params_are_strings() -> None:
    from uuid import uuid7

    org, user = uuid7(), uuid7()
    assert TenantContext(org, user, ("hr", "eng")).as_params() == {
        "org_id": str(org),
        "user_id": str(user),
        "groups": "hr,eng",
    }
    assert TenantContext(org).as_params()["user_id"] == ""


# Key-shaped values are assembled at runtime so no literal in the repo matches a secret scanner.
_FAKE_SECRETS = [
    "sk-" + "proj-" + "abcdefghijklmnopqrstuvwx",
    "sk-" + "ant-api03-" + "abcdefghijklmnopqrstuv",
    "fo_" + "AbCd1234_" + "abcdefghijklmnopqrstuvwxyz012345",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJ" + "hbGciOiJFZERTQSJ9." + "eyJzdWIiOiIxIn0." + "c2lnbmF0dXJl",
    "jane.doe" + "@example.com",
    "frt_" + "abcdefghijklmnopqrstuvwxyz0123",
]


@pytest.mark.parametrize("secret", _FAKE_SECRETS)
def test_redact_removes_secrets_and_pii(secret: str) -> None:
    assert secret not in redact(f"value={secret} trailing")


def test_redact_keeps_db_user_but_drops_password() -> None:
    out = redact("connect postgresql+psycopg://app:s3cretPassw0rd@db:5432/finops")
    assert "s3cretPassw0rd" not in out
    assert "app:" in out


def test_json_formatter_redacts_extras() -> None:
    record = logging.makeLogRecord(
        {
            "msg": "login %s",
            "args": ("user@example.com",),
            "levelname": "INFO",
            "token_hint": "sk-" + "abcdefghijklmnopqrstu",
        }
    )
    payload = orjson.loads(JsonFormatter("svc").format(record))
    assert payload["msg"] == "login [REDACTED_EMAIL]"
    assert payload["token_hint"] == "[REDACTED_KEY]"
    assert payload["service"] == "svc"


EXAMPLE = """# comment line
POSTGRES_PASSWORD=CHANGE_ME
POSTGRES_PORT=5432                    # host port
FINOPS_APP_PASSWORD=CHANGE_ME
FINOPS_WORKER_PASSWORD=CHANGE_ME
FINOPS_MIGRATOR_PASSWORD=CHANGE_ME
FINOPS_READONLY_PASSWORD=CHANGE_ME
DATABASE_URL=postgresql://app:CHANGE_ME@localhost:5432/finops
WORKER_DATABASE_URL=postgresql://worker:CHANGE_ME@localhost:5432/finops
JEV_API_KEY=                          # empty means unavailable
"""


def _parse(text: str) -> dict[str, str]:
    return {
        k: v.split("#")[0].strip()
        for k, _, v in (ln.partition("=") for ln in text.splitlines() if "=" in ln)
    }


def test_init_env_fills_placeholders_and_keeps_urls_consistent() -> None:
    env = _parse(render(EXAMPLE, port="55432"))
    assert "CHANGE_ME" not in "".join(env.values())
    assert env["POSTGRES_PORT"] == "55432"
    assert (
        env["DATABASE_URL"]
        == f"postgresql://app:{env[ROLE_PASSWORD_KEYS['app']]}@localhost:55432/finops"
    )
    assert env["WORKER_DATABASE_URL"].startswith(
        f"postgresql://worker:{env['FINOPS_WORKER_PASSWORD']}@"
    )
    assert env["JEV_API_KEY"] == ""
    assert (
        len({env["FINOPS_APP_PASSWORD"], env["FINOPS_WORKER_PASSWORD"], env["POSTGRES_PASSWORD"]})
        == 3
    )


def test_init_env_preserves_comments() -> None:
    out = render(EXAMPLE)
    assert "# comment line" in out
    assert "# host port" in out
    assert "5432/finops" in out


def test_an_unset_environment_means_production_and_refuses_missing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ENVIRONMENT", "JWT_SIGNING_KEY", "MASTER_KEY"):  # conftest loads .env
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="JWT_SIGNING_KEY"):
        Settings(_env_file=None, database_url=SecretStr("postgresql://a:b@h/d"))  # type: ignore[call-arg]
