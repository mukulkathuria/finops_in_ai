"""Process configuration from the environment and `.env` (ADR 0005).

Secrets are `SecretStr` so they never render in logs or reprs. The object is frozen: config
changes that must not restart the process go through snapshots, not through Settings.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    environment: Environment = Environment.DEV
    log_level: str = "INFO"
    service_name: str = "finops-gateway"

    # PostgreSQL 19 (app role, always under RLS). The worker uses its own role.
    database_url: SecretStr
    worker_database_url: SecretStr | None = None
    db_pool_size: int = Field(default=20, ge=1, le=500)
    db_max_overflow: int = Field(default=10, ge=0, le=500)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=600_000)

    # Authentication (US-01, US-03; ADR 0006)
    jwt_signing_key: SecretStr | None = None
    jwt_key_id: str = Field(default="k1", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    jwt_previous_public_keys: str = ""
    jwt_issuer: str = "finops-gateway"
    jwt_audience: str = "finops"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    signup_mode: Literal["bootstrap", "open", "disabled"] = "bootstrap"
    password_hash_concurrency: int = Field(default=4, ge=1, le=64)
    refresh_token_ttl_seconds: int = Field(default=14 * 86_400, ge=3_600, le=90 * 86_400)
    refresh_family_ttl_seconds: int = Field(default=30 * 86_400, ge=3_600, le=365 * 86_400)
    login_max_failures: int = Field(default=5, ge=3, le=20)
    auth_rate_limit_per_minute: int = Field(default=30, ge=1, le=100_000)

    # Invitations (US-02). Links point at the dashboard; the token travels in the URL fragment.
    invite_ttl_hours: int = Field(default=72, ge=1, le=720)
    web_base_url: str = "http://127.0.0.1:5173"

    @property
    def is_production_like(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)

    @model_validator(mode="after")
    def _refuse_unsafe_production(self) -> Settings:
        if not self.is_production_like:
            return self
        if "CHANGE_ME" in self.database_url.get_secret_value():
            raise ValueError("DATABASE_URL still contains the CHANGE_ME placeholder")
        key = self.jwt_signing_key.get_secret_value() if self.jwt_signing_key else ""
        if not key or "CHANGE_ME" in key:
            raise ValueError("JWT_SIGNING_KEY is required outside dev and test")
        return self
