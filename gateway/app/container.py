"""Composition of long-lived services (constructor injection, no globals)."""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

import httpx2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from control_plane.services.api_keys import ApiKeyService
from control_plane.services.authz import Authorizer
from control_plane.services.budgets import BudgetService
from control_plane.services.cache import CacheSettingsService
from control_plane.services.guardrails import GuardrailService
from control_plane.services.identity import IdentityService
from control_plane.services.invitations import InvitationService
from control_plane.services.providers import ProviderKeyService
from control_plane.services.relevance import RelevanceService
from control_plane.services.routing import RoutingService
from control_plane.services.savings import SavingsService
from control_plane.services.sessions import SessionService
from control_plane.services.stages import StageAdminService
from control_plane.services.teams import TeamService
from control_plane.services.webhooks import WebhookService
from gateway.adapters.classifiers.registry import build_classifier
from gateway.adapters.code.probe import OutlineProbe
from gateway.adapters.embeddings.openai_compat import OpenAICompatEmbedder
from gateway.adapters.files.clamav import ClamAV
from gateway.adapters.files.pdf import PdfProbe
from gateway.adapters.llm.registry import OrgAdapters, build_adapters
from gateway.adapters.moderation.registry import build_moderator
from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.adapters.observability.telemetry import Telemetry
from gateway.adapters.parsers.registry import build_parsers
from gateway.adapters.postgres.api_keys import ApiKeyRepository
from gateway.adapters.postgres.artifacts import ArtifactRepository
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.budgets import BudgetRepository
from gateway.adapters.postgres.cache import CacheRepository, CacheSettingsCache
from gateway.adapters.postgres.catalog import CatalogCache, CatalogRepository
from gateway.adapters.postgres.counters import PostgresCounterStore
from gateway.adapters.postgres.credentials import CredentialRepository, org_key_lookup
from gateway.adapters.postgres.database import Database
from gateway.adapters.postgres.documents import DocumentRepository
from gateway.adapters.postgres.files import FileRepository
from gateway.adapters.postgres.graph import GraphRepository, PostgresGraphStore
from gateway.adapters.postgres.idempotency import IdempotencyRepository
from gateway.adapters.postgres.identity import IdentityRepository, RoleCatalogCache
from gateway.adapters.postgres.policies import PolicyCache, PolicyRepository
from gateway.adapters.postgres.relevance import RelevanceRepository, RelevanceSettingsCache
from gateway.adapters.postgres.routing import RoutingRepository, RoutingSettingsCache
from gateway.adapters.postgres.savings import BaselineModels, SavingsRepository
from gateway.adapters.postgres.sessions import SessionRepository
from gateway.adapters.postgres.stages import StageModeCache, StageRepository
from gateway.adapters.postgres.teams import InvitationRepository, TeamRepository
from gateway.adapters.postgres.usage import UsageRepository
from gateway.adapters.postgres.webhooks import WebhookRepository
from gateway.adapters.rerank.http import HttpReranker
from gateway.adapters.security.envelope import Envelope, LocalKeyManager, parse_master_keys
from gateway.adapters.security.passwords import Argon2PasswordHasher
from gateway.adapters.security.tokens import TokenService, parse_public_keys, private_key_from_seed
from gateway.adapters.storage.local import LocalObjectStore
from gateway.app.gateway_config import load_gateway_config
from gateway.app.observability import Observability, build_observability
from gateway.app.settings import Settings
from gateway.domain.files import FileLimits
from gateway.domain.savings import OverheadRates
from gateway.pipeline.cache import ResponseCache
from gateway.pipeline.chat import HOLD_TTL, ChatService
from gateway.pipeline.code import CodeService
from gateway.pipeline.files import FileService
from gateway.pipeline.guardrails import Guardrails
from gateway.pipeline.parsing import ParseService
from gateway.pipeline.retrieval import RetrievalService
from gateway.pipeline.routing import RouterStage, Verifier
from gateway.pipeline.stages import CompressionStage, RagRelevance, StageRunner
from gateway.pipeline.usage import UsageReporter
from gateway.ports.storage import ObjectStore

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
    envelope: Envelope
    webhook_service: WebhookService
    guardrail_service: GuardrailService
    stage_admin_service: StageAdminService
    savings_service: SavingsService
    cache_settings_service: CacheSettingsService
    routing_service: RoutingService
    relevance_service: RelevanceService
    budget_service: BudgetService
    usage_reporter: UsageReporter
    object_store: ObjectStore
    file_service: FileService
    parse_service: ParseService
    retrieval_service: RetrievalService
    graph_store: PostgresGraphStore
    code_service: CodeService
    metrics: GatewayMetrics
    telemetry: Telemetry

    async def aclose(self) -> None:
        await self.parse_service.drain()
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


def _secret(value: SecretStr | None) -> str | None:
    """An empty value in .env (for example `JEV_API_KEY=`) means unset."""
    return value.get_secret_value() or None if value is not None else None


def build_container(
    settings: Settings,
    *,
    http_client: httpx2.AsyncClient | None = None,
    environ: Mapping[str, str] | None = None,
    observability: Observability | None = None,
) -> Container:
    observability = observability or build_observability(settings)
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
    object_store = LocalObjectStore(settings.object_store_path)
    artifacts = ArtifactRepository()
    file_service = FileService(
        db=db,
        repo=FileRepository(),
        store=object_store,
        limits=FileLimits(
            max_bytes=settings.max_upload_bytes,
            max_pages=settings.max_file_pages,
            max_image_pixels=settings.max_image_pixels,
        ),
        pdf=PdfProbe(timeout_s=settings.file_check_timeout_s),
        artifacts=artifacts,
        scanner=(
            ClamAV(settings.clamav_address, timeout_s=settings.file_check_timeout_s)
            if settings.clamav_address
            else None
        ),
        metrics=observability.metrics,
    )
    parse_service = ParseService(
        db=db,
        files=file_service,
        artifacts=artifacts,
        store=object_store,
        parsers=build_parsers(
            ocr_backend=settings.ocr_backend,
            ocr_servers={
                "glm_ocr": (settings.glm_ocr_base_url, settings.glm_ocr_model),
                "paddleocr_vl": (settings.paddleocr_vl_base_url, settings.paddleocr_vl_model),
            },
            parser_base_url=settings.parser_base_url,
            http=http,
            timeout_s=settings.parse_timeout_s,
        ),
        min_confidence=settings.ocr_min_confidence,
        metrics=observability.metrics,
    )
    embedder = OpenAICompatEmbedder(
        settings.embedder_base_url,
        settings.embedder_model,
        api_key=_secret(settings.embedder_api_key),
        http=http,
        timeout_s=settings.embedder_timeout_s,
    )
    catalog = CatalogCache(db, CatalogRepository())
    savings_repo = SavingsRepository()
    baselines = BaselineModels(db, savings_repo)
    cache_repo = CacheRepository()
    cache_settings = CacheSettingsCache(db, cache_repo)
    stage_repo = StageRepository()
    stage_modes = StageModeCache(db, stage_repo)
    routing_repo = RoutingRepository()
    routing_settings = RoutingSettingsCache(db, routing_repo)
    stage_runner = StageRunner(
        [CompressionStage(), RouterStage(routing_settings, catalog)],
        stage_modes,
        descriptors=[RagRelevance()],
    )
    relevance_repo = RelevanceRepository()
    relevance_settings = RelevanceSettingsCache(db, relevance_repo)
    reranker = (
        HttpReranker(
            settings.reranker_base_url,
            settings.reranker_model,
            http=http,
            api_key=_secret(settings.reranker_api_key),
            timeout_s=settings.reranker_timeout_s,
        )
        if settings.reranker_base_url
        else None
    )
    retrieval_service = RetrievalService(
        db=db,
        repo=DocumentRepository(),
        embedder=embedder,
        parsing=parse_service,
        metrics=observability.metrics,
        stages=stage_runner,
        reranker=reranker,
        relevance=relevance_settings,
        max_chunks_per_org=settings.max_indexed_chunks_per_org,
    )
    credentials = CredentialRepository()
    teams = TeamRepository()
    api_keys = ApiKeyRepository()
    budgets = BudgetRepository()
    classifier = build_classifier(
        settings.classifier_backend,
        http=http,
        laya_base_url=settings.laya_base_url,
        laya_api_key=_secret(settings.laya_api_key),
        jev_base_url=settings.jev_base_url,
        jev_api_key=_secret(settings.jev_api_key),
        jev_model=settings.jev_model,
        timeout_s=settings.classifier_timeout_ms / 1000,
    )
    policies = PolicyRepository()
    policy_cache = PolicyCache(db, policies)
    usage = UsageRepository()
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
    graph_store = PostgresGraphStore(db)
    moderator = build_moderator(
        settings.moderation_backend,
        http=http,
        base_url=settings.moderation_base_url,
        api_key=_secret(settings.moderation_api_key),
        model=settings.moderation_model,
        timeout_s=settings.moderation_timeout_s,
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
            guardrails=Guardrails(
                classifier=classifier,
                policies=policy_cache,
                telemetry=observability.telemetry,
                max_input_chars=settings.guardrail_max_input_chars,
            ),
            catalog=catalog,
            adapters=adapters,
            usage=usage,
            budgets=budgets,
            idempotency=IdempotencyRepository(stale_after_s=HOLD_TTL.total_seconds()),
            telemetry=observability.telemetry,
            default_max_tokens=gateway_config.defaults.max_output_tokens,
            retries=gateway_config.defaults.provider_retries,
            stages=stage_runner,
            stage_reports=stage_repo,
            overhead=OverheadRates(
                ocr_gpu_second=settings.ocr_gpu_second_micro_usd,
                classifier_call=settings.classifier_call_micro_usd,
                embedding_per_mtok=settings.embedding_micro_usd_per_mtok,
            ),
            baselines=baselines,
            moderator=moderator,
            cache=ResponseCache(
                db=db,
                repo=cache_repo,
                settings=cache_settings,
                envelope=envelope,
                embedder=embedder,
            ),
            verifier=Verifier(db=db, repo=routing_repo, embedder=embedder),
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
        budget_service=BudgetService(db=db, budgets=budgets, teams=teams, audit=audit, authz=authz),
        usage_reporter=UsageReporter(db=db, usage=usage, budgets=budgets),
        object_store=object_store,
        file_service=file_service,
        parse_service=parse_service,
        retrieval_service=retrieval_service,
        graph_store=graph_store,
        code_service=CodeService(
            db=db,
            graph=graph_store,
            repo=GraphRepository(),
            files=file_service,
            probe=OutlineProbe(),
        ),
        metrics=observability.metrics,
        telemetry=observability.telemetry,
        envelope=envelope,
        guardrail_service=GuardrailService(
            db=db, repo=policies, cache=policy_cache, audit=audit, authz=authz
        ),
        stage_admin_service=StageAdminService(
            db=db,
            repo=stage_repo,
            modes=stage_modes,
            runner=stage_runner,
            audit=audit,
            authz=authz,
        ),
        savings_service=SavingsService(
            db=db,
            repo=savings_repo,
            baselines=baselines,
            catalog=catalog,
            audit=audit,
            authz=authz,
        ),
        cache_settings_service=CacheSettingsService(
            db=db, repo=cache_repo, cache=cache_settings, audit=audit, authz=authz
        ),
        routing_service=RoutingService(
            db=db,
            repo=routing_repo,
            cache=routing_settings,
            catalog=catalog,
            audit=audit,
            authz=authz,
        ),
        relevance_service=RelevanceService(
            db=db, repo=relevance_repo, cache=relevance_settings, audit=audit, authz=authz
        ),
        webhook_service=WebhookService(
            db=db,
            repo=WebhookRepository(),
            envelope=envelope,
            audit=audit,
            authz=authz,
            allow_insecure=not settings.is_production_like,
        ),
    )
