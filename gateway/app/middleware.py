"""Request context: a request id on every request and response, one access log line, and
(US-29) a server span plus RED metrics per request.

Pure ASGI (no BaseHTTPMiddleware) so streaming responses pass through without buffering.
"""

import logging
import re
import time
from typing import Final
from uuid import uuid7

from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, StatusCode, Tracer
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.domain.errors import PayloadTooLarge

_VALID_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
log = logging.getLogger("finops.access")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), ""
        )
        request_id = incoming if _VALID_ID.fullmatch(incoming) else str(uuid7())
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "trace_id": scope.get("state", {}).get("trace_id"),
                },
            )


class TelemetryMiddleware:
    """A server span (continuing the caller's W3C trace) and RED metrics for every request.
    Labels use the route template, never the raw path, so cardinality stays bounded. Probes and
    scrapes are neither traced nor counted. The trace id is returned as `x-trace-id`."""

    _QUIET: Final = frozenset({"/healthz", "/readyz", "/metrics"})

    def __init__(self, app: ASGIApp, *, metrics: GatewayMetrics, tracer: Tracer) -> None:
        self.app = app
        self._metrics = metrics
        self._tracer = tracer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._QUIET:
            await self.app(scope, receive, send)
            return
        method: str = scope["method"]
        state = scope.setdefault("state", {})
        carrier = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        started = time.perf_counter()
        status = 500
        with self._tracer.start_as_current_span(
            method,
            context=extract(carrier),
            kind=SpanKind.SERVER,
            attributes={
                "http.request.method": method,
                "url.path": scope["path"],
                "finops.request_id": str(state.get("request_id", "")),
            },
        ) as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            state["trace_id"] = trace_id

            async def send_traced(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    MutableHeaders(scope=message)["x-trace-id"] = trace_id
                await send(message)

            self._metrics.http_in_flight.inc()
            try:
                await self.app(scope, receive, send_traced)
            finally:
                self._metrics.http_in_flight.dec()
                route = getattr(scope.get("route"), "path", None) or "unmatched"
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(StatusCode.ERROR)
                self._metrics.http_requests.labels(method, route, str(status)).inc()
                self._metrics.http_duration.labels(method, route).observe(
                    time.perf_counter() - started
                )


class BodyLimitMiddleware:
    """Reject request bodies above `max_bytes` (spec section 4, ingress) without buffering them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = next((v for k, v in scope["headers"] if k == b"content-length"), b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            body = b'{"error":{"message":"request body too large","type":"invalid_request_error",'
            body += b'"code":"request_too_large","param":null}}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise PayloadTooLarge("request body too large")
            return message

        await self.app(scope, limited, send)
