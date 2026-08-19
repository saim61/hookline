"""Delivery worker entrypoint. Run with `uv run hookline-worker`.

A separate process from the API, not a background task inside it. The two scale on
completely different signals - the API on request rate, the worker on how slow customer
endpoints are - and a worker blocked on a dozen ten-second timeouts must not be able to
add latency to an ingest call. In Kubernetes they are two Deployments with two replica
counts.
"""

import asyncio
import signal
import sys

from prometheus_client import start_http_server

from hookline.cache.client import close_redis
from hookline.config import get_settings
from hookline.db.session import dispose_engine, get_sessionmaker
from hookline.observability import metrics
from hookline.observability.logging import configure_logging, get_logger
from hookline.observability.tracing import instrument_worker
from hookline.worker.runner import build_worker

log = get_logger("hookline.worker")


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Ask the loop to finish its current batch and exit.

    Kubernetes sends SIGTERM and then waits before SIGKILL. Draining in that window is
    what keeps a rolling deploy from leaving a batch stranded in_flight for the reaper
    to clean up minutes later.

    add_signal_handler is not implemented on Windows, so fall back to signal.signal
    there. The fallback runs the callback off the event loop, hence call_soon_threadsafe.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _run() -> None:
    settings = get_settings()
    configure_logging(debug=settings.debug, service=f"{settings.app_name}-worker")
    instrument_worker(settings)

    if settings.metrics_enabled:
        # The worker serves no HTTP of its own, so Prometheus has nothing to scrape unless
        # it starts a listener. A separate port from the API's, since these are two
        # processes with two sets of numbers.
        start_http_server(settings.worker_metrics_port, registry=metrics.REGISTRY)
        log.info("metrics listening", port=settings.worker_metrics_port)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    worker, http = build_worker(settings, get_sessionmaker())
    try:
        await worker.run_forever(stop)
    finally:
        await http.aclose()
        await dispose_engine()
        await close_redis()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Second Ctrl-C during shutdown. Exiting quietly beats a traceback.
        sys.exit(130)


if __name__ == "__main__":
    main()
