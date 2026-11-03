"""Structured JSON logging with secret and PII redaction (spec section 10, data protection).

Redaction runs on the rendered message and on string extras, so a secret passed by mistake to a
log call never reaches the sink. Request bodies and prompts are never logged by design.
"""

import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final

import orjson

_REDACTIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bfo_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bfrt_[A-Za-z0-9_-]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    (re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+):[^@\s]+@"), r"\1:[REDACTED]@"),
    (
        re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)(\W{1,3})([^\s,;]{4,})"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[REDACTED_EMAIL]"),
)

_RESERVED: Final = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "msg": redact(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return orjson.dumps(payload, default=str).decode()


def configure_logging(level: str, service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):  # route server logs through the JSON formatter
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # the gateway writes its own access line
