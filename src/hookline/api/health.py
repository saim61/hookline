from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from hookline.api.deps import SessionDep
from hookline.cache import client as cache_client

router = APIRouter(tags=["meta"])


@router.get("/health", summary="Liveness: the process is running")
async def health() -> dict[str, str]:
    """Deliberately checks nothing.

    Kubernetes restarts the pod when liveness fails. If this touched Postgres, a brief
    database blip would restart every replica at once and turn a recoverable outage into
    a much worse one.
    """
    return {"status": "ok"}


@router.get("/ready", summary="Readiness: the process can serve traffic")
async def ready(session: SessionDep) -> dict[str, str]:
    """503 when Postgres is unreachable. Redis is reported but never fatal.

    Postgres is the source of truth - without it nothing can be ingested, so the pod
    should leave the load balancer. Redis only holds derived state, and every caller
    fails open, so a Redis outage degrades rate limiting and caching while ingest keeps
    working. Failing readiness on it would take the whole service down to protect a
    feature that is already designed to be optional.
    """
    try:
        await session.execute(text("select 1"))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        ) from exc

    return {
        "status": "ok",
        "database": "ok",
        "redis": "ok" if await cache_client.ping() else "degraded",
    }
