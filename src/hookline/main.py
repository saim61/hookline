from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hookline.api import health
from hookline.api.v1.router import api_router
from hookline.cache.client import close_redis
from hookline.config import get_settings
from hookline.db.session import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    print(f"starting {settings.app_name} (debug={settings.debug})")
    yield
    # Neither pool is opened until first use, so closing an unused one is a no-op. Both
    # are closed rather than left to the interpreter so a reload or a test that builds
    # several apps does not leak sockets.
    await dispose_engine()
    await close_redis()
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
