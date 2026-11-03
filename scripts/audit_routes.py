"""Audit: every API route is `@public` or has an authorization dependency (spec section 12).

Builds the app without starting it (no database needed) and walks every route.
Usage: python scripts/audit_routes.py   (exit 1 on violations)
"""

import sys

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import SecretStr

from gateway.api.markers import route_is_marked
from gateway.app.main import create_app
from gateway.app.settings import Settings

# Framework-generated documentation routes, disabled in staging and production.
_FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def find_unmarked(app: FastAPI) -> list[str]:
    problems = []
    # iter_route_contexts resolves included routers, so router-level dependencies count.
    for rc in iter_route_contexts(app.routes):
        if isinstance(rc.original_route, APIRoute):
            if not route_is_marked(rc.endpoint, getattr(rc, "dependant", None)):
                problems.append(f"{sorted(rc.methods or ())} {rc.path}")
        elif rc.path not in _FRAMEWORK_PATHS:
            problems.append(f"unaudited non-API route: {rc.path!r}")
    return problems


def main() -> int:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="test",
        database_url=SecretStr("postgresql://audit:audit@localhost/audit"),
    )
    problems = find_unmarked(create_app(settings))
    for p in problems:
        print(f"UNPROTECTED ROUTE: {p}")
    print(f"route audit: {len(problems)} violation(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
