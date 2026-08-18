from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from hookline.api.deps import SessionDep

router = APIRouter(tags=["meta"])


@router.get("/health", summary="Health check endpoint")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness check endpoint")
async def ready(session: SessionDep) -> dict[str, str]:
    try:
        await session.execute(text("select 1"))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        ) from exc
    return {"status": "ok"}
