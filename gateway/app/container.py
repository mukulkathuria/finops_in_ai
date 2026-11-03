"""Composition of long-lived services (constructor injection, no globals)."""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

import httpx2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from control_plane.services.api_keys import ApiKeyService
from control_plane.services.authz import Authorizer
from control_plane.services.identity import IdentityService
from control_plane.services.invitations import InvitationService
from control_plane.services.providers import ProviderKeyService
from control_plane.services.sessions import SessionService
from control_plane.services.teams import TeamService
from gateway.adapters.llm.registry import OrgAdapters, build_adapters
from gateway.adapters.postgres.api_keys import ApiKeyRepository
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.catalog import CatalogCache, CatalogRepository
from gateway.adapters.postgres.counters import PostgresCounterStore
from gateway.adapters.postgres.credentials import CredentialRepository, org_key_lookup
from gateway.adapters.postgres.database import Database
from gateway.adapters.postgres.identity import IdentityRepository, RoleCatalogCache
from gateway.adapters.postgres.sessions import SessionRepository
from gateway.adapters.postgres.teams import InvitationRepository, TeamRepository
from gateway.adapters.postgres.usage import UsageRepository
from gateway.adapters.security.envelope import Envelope, LocalKeyManager, parse_master_keys
from gateway.adapters.security.passwords import Argon2PasswordHasher
from gateway.adapters.security.tokens import TokenService, parse_public_keys, private_key_from_seed
from gateway.app.gateway_config import load_gateway_config
from gateway.app.settings import Settings
from gateway.pipeline.chat import ChatService

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    db: Database
    tokens: TokenService
    hasher: Argon2PasswordHasher
    identity_repo: IdentityRepository
    role_catalog: RoleCatalogCache
    audit: AuditRepository
    api_keys: ApiKeyRepository
    counters: PostgresCounterStore
    session_service: SessionService
    identity_service: IdentityService
    team_service: TeamService
    invitation_service: InvitationService
    api_key_service: ApiKeyService
    http: httpx2.AsyncClient
    owns_http: bool
    chat_service: ChatService
    provider_key_service: ProviderKeyService

    async def aclose(self) -> None:
        await self.chat_service.drain()
        if self.owns_http:
            await self.http.aclose()
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


def build_envelope(settings: Settings) -> Envelope:
    if settings.master_key is None:
        # Settings refuses this outside dev and test; stored keys then die with the process.
        log.warning("MASTER_KEY is not set; using an ephemeral master key")
        keys, active = {"ephemeral": os.urandom(32)}, "ephemeral"
    else:
        keys = parse_master_keys(
            settings.master_key.get_secret_value(),
            settings.master_key_id,
            settings.master_previous_keys.get_secret_value(),
        )
        active = settings.master_key_id
    return Envelope(LocalKeyManager(keys, active))


def build_container(
    settings: Settings,
    *,
    http_client: httpx2.AsyncClient | None = None,
    environ: Mapping[str, str] | None = None,
) -> Container:
    db = Database(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        application_name=settings.service_name,
    )
    identity_repo = IdentityRepository()
    audit = AuditRepository()
    tokens = build_token_service(settings)
    hasher = Argon2PasswordHasher(max_concurrency=settings.password_hash_concurrency)
    http = http_client or httpx2.AsyncClient(
        timeout=httpx2.Timeout(120.0, connect=5.0),
        limits=httpx2.Limits(max_connections=500, max_keepalive_connections=100),
    )
    gateway_config = load_gateway_config(settings.config_path)
    provider_configs = gateway_config.provider_configs(settings.provider_adapter_default)
    adapters = build_adapters(provider_configs, os.environ if environ is None else environ, http)
    envelope = build_envelope(settings)
    credentials = CredentialRepository()
    teams = TeamRepository()
    api_keys = ApiKeyRepository()
    authz = Authorizer(db=db, audit=audit)
    sessions = SessionService(
        db=db,
        sessions=SessionRepository(),
        identity=identity_repo,
        audit=audit,
        hasher=hasher,
        tokens=tokens,
        refresh_ttl=timedelta(seconds=settings.refresh_token_ttl_seconds),
        family_ttl=timedelta(seconds=settings.refresh_family_ttl_seconds),
        max_failed_logins=settings.login_max_failures,
    )
    return Container(
        settings=settings,
        db=db,
        tokens=tokens,
        hasher=hasher,
        identity_repo=identity_repo,
        role_catalog=RoleCatalogCache(identity_repo),
        audit=audit,
        api_keys=api_keys,
        counters=PostgresCounterStore(db),
        session_service=sessions,
        identity_service=IdentityService(
            db=db,
            repo=identity_repo,
            audit=audit,
            hasher=hasher,
            sessions=sessions,
            signup_mode=settings.signup_mode,
        ),
        team_service=TeamService(
            db=db, teams=teams, identity=identity_repo, audit=audit, authz=authz
        ),
        invitation_service=InvitationService(
            db=db,
            invitations=InvitationRepository(),
            teams=teams,
            identity=identity_repo,
            audit=audit,
            authz=authz,
            hasher=hasher,
            sessions=sessions,
            ttl=timedelta(hours=settings.invite_ttl_hours),
            web_base_url=settings.web_base_url,
        ),
        api_key_service=ApiKeyService(db=db, keys=api_keys, teams=teams, audit=audit, authz=authz),
        http=http,
        owns_http=http_client is None,
        chat_service=ChatService(
            db=db,
            catalog=CatalogCache(db, CatalogRepository()),
            adapters=adapters,
            usage=UsageRepository(),
            default_max_tokens=gateway_config.defaults.max_output_tokens,
            retries=gateway_config.defaults.provider_retries,
            org_adapters=OrgAdapters(
                org_key_lookup(db, credentials, envelope), provider_configs, http
            ),
        ),
        provider_key_service=ProviderKeyService(
            db=db,
            repo=credentials,
            envelope=envelope,
            audit=audit,
            authz=authz,
            known_providers=frozenset(provider_configs),
        ),
    )
