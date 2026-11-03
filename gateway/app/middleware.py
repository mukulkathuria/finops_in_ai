"""Request context: a request id on every request and response, plus one access log line.

Pure ASGI (no BaseHTTPMiddleware) so streaming responses pass through without buffering.
"""

import logging
import re
import time
from typing import Final
from uuid import uuid7

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
                },
            )
