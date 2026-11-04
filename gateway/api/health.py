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
    return {"status": "ok" if ready else "unavailable", "checks": checks}
