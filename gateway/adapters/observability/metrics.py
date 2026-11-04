"""Prometheus metrics (US-29).

One registry per app instance (no process-global state), and only bounded label values: route
templates, providers, catalog models, outcomes and budget scopes; never ids, paths or text.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)


class GatewayMetrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        ProcessCollector(registry=r)
        PlatformCollector(registry=r)
        GCCollector(registry=r)
        self.http_requests = Counter(
            "finops_http_requests",
            "HTTP requests by method, route template and status.",
            ("method", "route", "status"),
            registry=r,
        )
        self.http_duration = Histogram(
            "finops_http_request_duration_seconds",
            "HTTP request duration; streams count until their last byte.",
            ("method", "route"),
            buckets=_SECONDS,
            registry=r,
        )
        self.http_in_flight = Gauge(
            "finops_http_requests_in_flight", "HTTP requests being served.", registry=r
        )
        self.stage_duration = Histogram(
            "finops_stage_duration_seconds",
            "Pipeline stage duration: auth, budget.reserve, provider (streams: to the first"
            " chunk) and budget.settle.",
            ("stage",),
            buckets=_SECONDS,
            registry=r,
        )
        self.llm_requests = Counter(
            "finops_llm_requests",
            "Model calls by provider, model, kind and outcome.",
            ("provider", "model", "kind", "status"),
            registry=r,
        )
        self.llm_tokens = Counter(
            "finops_llm_tokens",
            "Billed tokens by provider, model and direction.",
            ("provider", "model", "direction"),
            registry=r,
        )
        self.llm_cost = Counter(
            "finops_llm_cost_micro_usd",
            "Spend in micro-USD by provider and model.",
            ("provider", "model"),
            registry=r,
        )
        self.budget_rejections = Counter(
            "finops_budget_rejections",
            "Requests refused with budget_exceeded, by the scope that refused them.",
            ("scope",),
            registry=r,
        )
        self.budget_alerts = Counter(
            "finops_budget_soft_alerts", "Soft-threshold budget alerts fired.", registry=r
        )
        self.guardrail_decisions = Counter(
            "finops_guardrail_decisions",
            "Guardrail decisions by stage, check, action and backend (never the matched text).",
            ("stage", "check", "action", "backend"),
            registry=r,
        )
        self.retrievals = Counter(
            "finops_retrievals",
            "Retrieval operations by source (api search, chat, ingest).",
            ("source",),
            registry=r,
        )
        self.document_parses = Counter(
            "finops_document_parses",
            "Document conversions by backend and outcome (parsed, cached, failed).",
            ("backend", "outcome"),
            registry=r,
        )
        self.file_uploads = Counter(
            "finops_file_uploads",
            "File uploads by result (accepted or rejected) and reason (file kind or refusal).",
            ("result", "reason"),
            registry=r,
        )
        self.idempotent_replays = Counter(
            "finops_idempotent_replays",
            "Responses replayed for a repeated Idempotency-Key.",
            ("kind",),
            registry=r,
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
