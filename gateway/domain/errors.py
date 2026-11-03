"""Expected, typed failures (spec section 3A: typed results for expected failures).

Use cases raise these; the API layer maps each kind to an HTTP status and an OpenAI-shaped error
body. Anything else is a bug and becomes a 500 without internal details.
"""

from collections.abc import Mapping
from typing import Any, ClassVar


class DomainError(Exception):
    kind: ClassVar[str] = "invalid_request"
    default_code: ClassVar[str] = "invalid_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.param = param
        self.details = dict(details or {})


class InvalidInput(DomainError):
    kind = "invalid_request"
    default_code = "invalid_request"


class Unauthenticated(DomainError):
    kind = "authentication"
    default_code = "invalid_credentials"


class Forbidden(DomainError):
    kind = "permission"
    default_code = "forbidden"


class NotFound(DomainError):
    kind = "not_found"
    default_code = "not_found"


class Conflict(DomainError):
    kind = "conflict"
    default_code = "conflict"


class Gone(DomainError):
    kind = "gone"
    default_code = "gone"


class RateLimited(DomainError):
    kind = "rate_limit"
    default_code = "rate_limited"

    def __init__(self, message: str, *, retry_after_s: int = 1, code: str | None = None) -> None:
        super().__init__(message, code=code)
        self.retry_after_s = retry_after_s


class Unavailable(DomainError):
    kind = "unavailable"
    default_code = "service_unavailable"
