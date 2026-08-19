from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from hookline.api import health, metrics
from hookline.api.v1.router import api_router
from hookline.cache.client import close_redis
from hookline.config import get_settings
from hookline.dashboard import routes as dashboard_routes
from hookline.dashboard.deps import NotLoggedInError, login_redirect
from hookline.db.session import dispose_engine
from hookline.observability.logging import configure_logging, get_logger
from hookline.observability.middleware import ObservabilityMiddleware
from hookline.observability.tracing import instrument_app
from hookline.paths import STATIC_DIR

log = get_logger("hookline.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info(
        "starting",
        app=settings.app_name,
        debug=settings.debug,
        auth_enabled=settings.auth_enabled,
        metrics_enabled=settings.metrics_enabled,
        dashboard_enabled=settings.dashboard_enabled,
    )
    yield
    # Neither pool is opened until first use, so closing an unused one is a no-op. Both
    # are closed rather than left to the interpreter so a reload or a test that builds
    # several apps does not leak sockets.
    await dispose_engine()
    await close_redis()
    log.info("stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    # Before the app exists, so the very first log line - including anything emitted
    # during import or startup - is already structured.
    configure_logging(debug=settings.debug, service=settings.app_name)

    app = FastAPI(
        title="Hookline",
        description="Reliable webhook delivery service",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Outermost middleware, so its timing covers everything below it including the auth
    # dependency and any error handling.
    app.add_middleware(ObservabilityMiddleware)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(api_router)

    if settings.dashboard_enabled:
        app.include_router(dashboard_routes.router)
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.exception_handler(NotLoggedInError)
        async def _redirect_to_login(request: Request, exc: NotLoggedInError) -> Response:
            """A dependency cannot return a redirect - FastAPI would inject the response
            object as the parameter - so the redirect happens here instead."""
            return login_redirect(request)

    # After the routes are registered: the instrumentor walks them to name its spans.
    instrument_app(app, settings)
    return app


app = create_app()
