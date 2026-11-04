"""Process configuration from the environment and `.env` (ADR 0005).

Secrets are `SecretStr` so they never render in logs or reprs. The object is frozen: config
changes that must not restart the process go through snapshots, not through Settings.
"""

from enum import StrEnum
from pathlib import Path
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

    # Unset means production: a deployment that forgets ENVIRONMENT fails fast on missing
    # secrets instead of running with ephemeral keys and public docs.
    environment: Environment = Environment.PRODUCTION
    log_level: str = "INFO"
    service_name: str = "finops-gateway"
    config_path: Path = Path("config/gateway.yaml")
    max_request_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)

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

    # Provider keys (US-06): envelope encryption master key and the adapter switch.
    master_key: SecretStr | None = None
    master_key_id: str = Field(default="m1", pattern=r"^[A-Za-z0-9._-]{1,32}$")
    master_previous_keys: SecretStr = SecretStr("")
    provider_adapter_default: Literal["native", "litellm"] = "native"

    # Classifiers (US-11, US-12, US-20): rules | laya | jev. Laya speaks the SystemOne
    # protocol locally; Jev is hosted and needs JEV_API_KEY (selecting it without one fails).
    classifier_backend: Literal["rules", "laya", "jev"] = "laya"
    laya_base_url: str = "http://laya:8000"
    laya_api_key: SecretStr | None = None
    jev_base_url: str = "https://routellm.abacus.ai"
    jev_api_key: SecretStr | None = None
    jev_model: str = "jev-latest"
    classifier_timeout_ms: int = Field(default=2000, ge=50, le=30_000)

    # Files (US-14): validated from their bytes; originals live in the object store.
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    max_file_pages: int = Field(default=500, ge=1, le=100_000)
    max_image_pixels: int = Field(default=50_000_000, ge=1_000_000, le=1_000_000_000)
    file_check_timeout_s: float = Field(default=15.0, gt=0, le=300)
    object_store: Literal["local"] = "local"
    object_store_path: str = "/var/lib/finops/objects"
    clamav_address: str | None = None  # host:port of clamd; unset or empty disables it

    # Document parsing (US-15, US-16): one OCR backend at a time, switched by OCR_BACKEND and a
    # restart. Every URL is a sidecar speaking the parser contract (POST {url}/parse).
    ocr_backend: Literal["glm_ocr", "paddleocr_vl"] = "glm_ocr"
    ocr_min_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    glm_ocr_base_url: str = "http://glm-ocr:8000/v1"
    glm_ocr_model: str = "zai-org/GLM-OCR"
    paddleocr_vl_base_url: str = "http://paddleocr-vl:8080/v1"
    paddleocr_vl_model: str = "PaddleOCR-VL-1.5-0.9B"
    parser_base_url: str = "http://docparse:8000/v1"  # MarkItDown: Office files and HTML
    parse_timeout_s: float = Field(default=600.0, gt=0, le=3600)

    # Invitations (US-02). Links point at the dashboard; the token travels in the URL fragment.
    invite_ttl_hours: int = Field(default=72, ge=1, le=720)
    web_base_url: str = "http://127.0.0.1:5173"

    # Worker (US-10): how often the budget janitor returns the holds of requests that died,
    # and how often it rebuilds the current budget counters from the ledger.
    janitor_interval_seconds: float = Field(default=30, ge=1, le=3600)
    reconcile_interval_seconds: float = Field(default=300, ge=10, le=86_400)
    outbox_interval_seconds: float = Field(default=5, ge=0.5, le=300)  # alert delivery

    # Observability (US-29). Traces export over OTLP/HTTP when the endpoint is set (empty: no
    # export). /metrics needs METRICS_TOKEN as a bearer token when it is set; without one it
    # is served in dev and test only.
    otel_exporter_otlp_endpoint: str = ""
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    metrics_token: SecretStr | None = None

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
        master = self.master_key.get_secret_value() if self.master_key else ""
        if not master or "CHANGE_ME" in master:
            raise ValueError("MASTER_KEY is required outside dev and test")
        return self
