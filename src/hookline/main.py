from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hookline.api import health
from hookline.api.v1.router import api_router
from hookline.config import get_settings
from hookline.db.session import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    print(f"starting {settings.app_name} (debug={settings.debug})")
    yield
    await dispose_engine()
    print("shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Hookline",
        description="Reliable webhook delivery service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(api_router)
    return app


app = create_app()
