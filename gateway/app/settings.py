"""Process configuration from the environment and `.env` (ADR 0005).

Secrets are `SecretStr` so they never render in logs or reprs. The object is frozen: config
changes that must not restart the process go through snapshots, not through Settings.
"""

from enum import StrEnum

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

    @property
    def is_production_like(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)

    @model_validator(mode="after")
    def _refuse_placeholders_outside_dev(self) -> Settings:
        if self.is_production_like and "CHANGE_ME" in self.database_url.get_secret_value():
            raise ValueError("DATABASE_URL still contains the CHANGE_ME placeholder")
        return self
