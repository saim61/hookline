"""Delivery worker entrypoint. Run with `uv run hookline-worker`.

A separate process from the API, not a background task inside it. The two scale on
completely different signals - the API on request rate, the worker on how slow customer
endpoints are - and a worker blocked on a dozen ten-second timeouts must not be able to
add latency to an ingest call. In Kubernetes they are two Deployments with two replica
counts.
"""

import asyncio
import logging
import signal
import sys

from hookline.cache.client import close_redis
from hookline.config import get_settings
from hookline.db.session import dispose_engine, get_sessionmaker
from hookline.worker.runner import build_worker


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
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

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
