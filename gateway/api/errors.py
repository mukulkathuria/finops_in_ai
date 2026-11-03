"""OpenAI-shaped error responses for every failure (spec section 3, US-05 compatibility).

`{"error": {"message", "type", "code", "param"}}` plus optional extra fields (for example the
limiting `scope` on `budget_exceeded`). Unexpected exceptions become a generic 500: the details
go to the log with the request id, never to the client.
"""

import logging
from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gateway.domain.errors import DomainError, RateLimited
from gateway.domain.llm import ProviderError, ProviderErrorKind

log = logging.getLogger(__name__)

_STATUS_BY_KIND = {
    "invalid_request": 422,
    "malformed": 400,
    "authentication": 401,
    "permission": 403,
    "not_found": 404,
    "conflict": 409,
    "gone": 410,
    "rate_limit": 429,
    "too_large": 413,
    "budget_exceeded": 429,
    "blocked": 400,
    "unavailable": 503,
}
_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    405: "invalid_request_error",
    409: "conflict_error",
    410: "gone_error",
    413: "invalid_request_error",
    415: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    502: "upstream_error",
    503: "service_unavailable_error",
    504: "upstream_timeout_error",
}
_PROVIDER = {
    ProviderErrorKind.RATE_LIMIT: (429, "upstream_rate_limited"),
    ProviderErrorKind.TIMEOUT: (504, "upstream_timeout"),
    ProviderErrorKind.CONTENT_FILTER: (400, "content_filter"),
    ProviderErrorKind.CTX_OVERFLOW: (400, "context_length_exceeded"),
    ProviderErrorKind.AUTH: (502, "upstream_auth_failed"),
    ProviderErrorKind.BAD_REQUEST: (400, "upstream_bad_request"),
    ProviderErrorKind.UNAVAILABLE: (503, "upstream_unavailable"),
    ProviderErrorKind.OTHER: (502, "upstream_error"),
}


def error_payload(
    status: int,
    message: str,
    *,
    code: str,
    param: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message": message,
        "type": _TYPE_BY_STATUS.get(status, "api_error"),
        "code": code,
        "param": param,
    }
    body.update(extra or {})
    return {"error": body}


def error_response(
    status: int,
    message: str,
    *,
    code: str,
    param: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        error_payload(status, message, code=code, param=param, extra=extra), status, headers
    )


async def _domain_error(_: Request, raw: Exception) -> JSONResponse:
    exc = cast(DomainError, raw)  # registered for DomainError only
    status = _STATUS_BY_KIND.get(exc.kind, 400)
    headers = {"retry-after": str(exc.retry_after_s)} if isinstance(exc, RateLimited) else None
    return error_response(
        status, exc.message, code=exc.code, param=exc.param, extra=exc.details, headers=headers
    )


async def _provider_error(_: Request, raw: Exception) -> JSONResponse:
    exc = cast(ProviderError, raw)  # registered for ProviderError only
    status, code = _PROVIDER[exc.kind]
    # Upstream auth messages can quote (masked) credentials: never relay them to callers.
    message = (
        f"provider {exc.provider} rejected the gateway credentials"
        if exc.kind is ProviderErrorKind.AUTH
        else f"provider {exc.provider}: {exc.message}"
    )
    headers = {"retry-after": str(max(1, int(exc.retry_after_s)))} if exc.retry_after_s else None
    return error_response(status, message, code=code, headers=headers)


async def _validation_error(_: Request, raw: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, raw)  # registered for RequestValidationError only
    first = exc.errors()[0] if exc.errors() else {}
    loc = [str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path", "header")]
    message = str(first.get("msg", "request is not valid"))
    return error_response(422, message, code="invalid_request", param=".".join(loc) or None)


async def _http_error(_: Request, raw: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, raw)  # registered for HTTPException only
    return error_response(
        exc.status_code, str(exc.detail), code=f"http_{exc.status_code}", headers=exc.headers
    )


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.error(
        "unhandled error",
        exc_info=exc,
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    return error_response(500, "internal error", code="internal_error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(ProviderError, _provider_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)
