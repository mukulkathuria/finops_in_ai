"""Metrics and trace wiring (US-29): one Prometheus registry and one tracer provider per app,
so several apps in one process (tests) never share or collide on global state."""

from dataclasses import dataclass

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from gateway import __version__
from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.adapters.observability.telemetry import Telemetry
from gateway.app.settings import Settings


@dataclass(frozen=True, slots=True)
class Observability:
    metrics: GatewayMetrics
    telemetry: Telemetry
    tracer_provider: TracerProvider

    def shutdown(self) -> None:
        """Flush queued spans to the exporter (bounded by its timeout)."""
        self.tracer_provider.shutdown()


def build_observability(
    settings: Settings, *, span_exporter: SpanExporter | None = None
) -> Observability:
    """Spans export over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set; tests pass an
    in-memory exporter. Sampling honours the caller's decision and samples new traces at
    OTEL_SAMPLE_RATIO."""
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": __version__,
                "deployment.environment.name": settings.environment.value,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
    )
    if span_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif settings.otel_exporter_otlp_endpoint:
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5))
        )
    metrics = GatewayMetrics()
    tracer = provider.get_tracer("finops-gateway", __version__)
    return Observability(metrics, Telemetry(metrics, tracer), provider)
