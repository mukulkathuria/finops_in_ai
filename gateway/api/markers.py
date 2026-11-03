"""Route authorization markers, checked by `scripts/audit_routes.py` (spec section 12).

Every API route must either be explicitly `@public` or depend on a dependency created with
`authz_dependency`. The audit fails CI when a route has neither, which catches the classic
"generated endpoint works but forgot the access check" bug.
"""

from collections.abc import Callable, Iterator
from typing import Any

from fastapi.dependencies.models import Dependant

_PUBLIC = "__finops_public__"
_AUTHZ = "__finops_authz__"


def public[F: Callable[..., Any]](func: F) -> F:
    setattr(func, _PUBLIC, True)
    return func


def authz_dependency[F: Callable[..., Any]](func: F) -> F:
    setattr(func, _AUTHZ, True)
    return func


def _walk(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _walk(sub)


def route_is_marked(endpoint: Callable[..., Any] | None, dependant: Dependant | None) -> bool:
    """True when the endpoint is @public or any (router-inherited) dependency is an authz guard."""
    if endpoint is not None and getattr(endpoint, _PUBLIC, False):
        return True
    if dependant is None:
        return False
    return any(getattr(dep.call, _AUTHZ, False) for dep in _walk(dependant) if dep.call)
