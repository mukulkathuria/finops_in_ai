"""Liveness and readiness probes (US-29)."""

from fastapi import APIRouter, Request, Response

from gateway.api.markers import public

router = APIRouter(tags=["ops"])


@router.get("/healthz")
@public
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving the event loop."""
    return {"status": "ok"}


@router.get("/readyz")
@public
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness: every hard dependency answers within its deadline (503 otherwise)."""
    container = request.app.state.container
    checks = {
        "database": "ok" if await container.db.is_ready() else "unavailable",
        "objects": "ok" if await container.object_store.is_ready() else "unavailable",
    }
    ready = all(v == "ok" for v in checks.values())
    response.status_code = 200 if ready else 503
    body: dict[str, object] = {"status": "ok" if ready else "unavailable", "checks": checks}
    if checks["database"] == "ok":  # informational (ADR 0004): it never decides readiness
        present = await container.graph_store.sql_pgq()
        if present is not None:
            body["info"] = {"sql_pgq": "present" if present else "absent"}
    return body
