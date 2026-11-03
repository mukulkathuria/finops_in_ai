"""Composition of long-lived services (constructor injection, no globals)."""

import logging
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from control_plane.services.identity import IdentityService
from gateway.adapters.postgres.database import Database
from gateway.adapters.postgres.identity import IdentityRepository, RoleCatalogCache
from gateway.adapters.security.passwords import Argon2PasswordHasher
from gateway.adapters.security.tokens import TokenService, parse_public_keys, private_key_from_seed
from gateway.app.settings import Settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    db: Database
    tokens: TokenService
    hasher: Argon2PasswordHasher
    identity_repo: IdentityRepository
    role_catalog: RoleCatalogCache
    identity_service: IdentityService

    async def aclose(self) -> None:
        await self.db.dispose()


def build_token_service(settings: Settings) -> TokenService:
    if settings.jwt_signing_key is None:
        # Settings refuses this outside dev and test; tokens then die with the process.
        log.warning("JWT_SIGNING_KEY is not set; using an ephemeral signing key")
        signing_key = Ed25519PrivateKey.generate()
    else:
        signing_key = private_key_from_seed(settings.jwt_signing_key.get_secret_value())
    return TokenService(
        signing_key=signing_key,
        key_id=settings.jwt_key_id,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        access_ttl_seconds=settings.access_token_ttl_seconds,
        previous_keys=parse_public_keys(settings.jwt_previous_public_keys),
    )


def build_container(settings: Settings) -> Container:
    db = Database(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        application_name=settings.service_name,
    )
    identity_repo = IdentityRepository()
    tokens = build_token_service(settings)
    hasher = Argon2PasswordHasher(max_concurrency=settings.password_hash_concurrency)
    return Container(
        settings=settings,
        db=db,
        tokens=tokens,
        hasher=hasher,
        identity_repo=identity_repo,
        role_catalog=RoleCatalogCache(identity_repo),
        identity_service=IdentityService(
            db=db,
            repo=identity_repo,
            hasher=hasher,
            tokens=tokens,
            signup_mode=settings.signup_mode,
        ),
    )
