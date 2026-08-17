from fastapi import APIRouter

router = APIRouter(tags=["meta"])

@router.get("/health", summary="Health check endpoint")
async def health() -> dict[str, str]:
    return {"status": "ok"}

@router.get("/ready", summary="Readiness check endpoint")
async def ready() -> dict[str, str]:
    return {"status": "ok"}