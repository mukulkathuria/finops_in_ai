"""Pipeline telemetry (US-29): one span per stage with its duration in a histogram, and GenAI
semantic-convention client spans for provider calls.

Prompt and completion text never enter a span, a metric label or a log line (spec section 12,
privacy): spans carry model names, token counts, cost and outcome only.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Tracer
from opentelemetry.util.types import AttributeValue

from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.domain.budgets import BudgetExceeded
from gateway.domain.llm import Usage


class Telemetry:
    def __init__(self, metrics: GatewayMetrics, tracer: Tracer) -> None:
        self.metrics = metrics
        self.tracer = tracer

    @contextmanager
    def stage(self, name: str) -> Iterator[Span]:
        """A child span of the current request, timed into the stage histogram."""
        started = time.perf_counter()
        with self.tracer.start_as_current_span(name) as span:
            try:
                yield span
            finally:
                self.metrics.stage_duration.labels(name).observe(time.perf_counter() - started)

    @staticmethod
    def _gen_ai(
        operation: str, provider: str, model: str, max_tokens: int | None
    ) -> dict[str, AttributeValue]:
        attributes: dict[str, AttributeValue] = {
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
        }
        if max_tokens is not None:
            attributes["gen_ai.request.max_tokens"] = max_tokens
        return attributes

    @contextmanager
    def provider_call(
        self, operation: str, provider: str, model: str, max_tokens: int | None = None
    ) -> Iterator[Span]:
        """A GenAI client span around one provider call, retries included."""
        started = time.perf_counter()
        with self.tracer.start_as_current_span(
            f"{operation} {model}",
            kind=SpanKind.CLIENT,
            attributes=self._gen_ai(operation, provider, model, max_tokens),
        ) as span:
            try:
                yield span
            finally:
                self.metrics.stage_duration.labels("provider").observe(
                    time.perf_counter() - started
                )

    def stream_span(self, provider: str, model: str, max_tokens: int) -> Span:
        """A stream outlives its handler, so its span is ended explicitly when it settles."""
        return self.tracer.start_span(
            f"chat {model}",
            kind=SpanKind.CLIENT,
            attributes=self._gen_ai("chat", provider, model, max_tokens),
        )

    @contextmanager
    def opening(self, span: Span) -> Iterator[None]:
        """Make a stream's span current while the stream opens; time to the first chunk."""
        started = time.perf_counter()
        try:
            with trace.use_span(span, end_on_exit=False):
                yield
        finally:
            self.metrics.stage_duration.labels("provider").observe(time.perf_counter() - started)

    @staticmethod
    def annotate(
        span: Span, usage: Usage, cost: int, finish: str | None, *, failed: bool = False
    ) -> None:
        if not span.is_recording():
            return
        span.set_attributes(
            {
                "gen_ai.usage.input_tokens": usage.input_tokens,
                "gen_ai.usage.output_tokens": usage.output_tokens,
                "finops.cost_micro_usd": cost,
                "finops.usage_estimated": usage.estimated,
            }
        )
        if finish:
            span.set_attribute("gen_ai.response.finish_reasons", (finish,))
        if failed:
            span.set_status(trace.StatusCode.ERROR)

    def count(
        self, *, provider: str, model: str, kind: str, status: str, usage: Usage, cost: int
    ) -> None:
        m = self.metrics
        m.llm_requests.labels(provider, model, kind, status).inc()
        if usage.input_tokens:
            m.llm_tokens.labels(provider, model, "input").inc(usage.input_tokens)
        if usage.output_tokens:
            m.llm_tokens.labels(provider, model, "output").inc(usage.output_tokens)
        if cost:
            m.llm_cost.labels(provider, model).inc(cost)

    def budget_refused(self, span: Span, exc: BudgetExceeded) -> None:
        scope = exc.details.get("scope")
        name = str(scope.get("type", "unknown")) if isinstance(scope, dict) else "unknown"
        self.metrics.budget_rejections.labels(name).inc()
        span.set_attribute("finops.budget.refused_scope", name)
