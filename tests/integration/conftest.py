"""Fixtures for tests that need the app, a real HTTP client, or a fake receiver."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from hookline.auth.scopes import Scope
from hookline.config import Settings, get_settings
from hookline.db.session import get_sessionmaker
from hookline.delivery import signing
from hookline.repositories.api_key import ApiKeyRepository
from hookline.worker.runner import DeliveryWorker

RECEIVER_PORT = 8097


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    """A fresh app per test.

    `create_app()` is a factory precisely so this is possible: middleware, routers and
    instrumentation are all rebuilt, so one test cannot leave state behind in another's
    application object.
    """
    from hookline.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Unauthenticated client, over ASGI rather than a real socket.

    No port to bind, no server to wait for, and a failure inside a handler surfaces as a
    Python traceback instead of a 500 with the detail hidden.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_key(name: str, scopes: list[str], **kwargs: Any) -> tuple[str, str, str | None]:
    async with get_sessionmaker()() as session:
        api_key, token = await ApiKeyRepository(session).create(name=name, scopes=scopes, **kwargs)
        await session.commit()
        return str(api_key.id), token, api_key.inbound_signing_secret


@pytest.fixture
async def make_key() -> Callable[..., Any]:
    return _make_key


@pytest.fixture
async def admin_key() -> tuple[str, str]:
    key_id, token, _ = await _make_key("test admin", [Scope.ADMIN.value])
    return key_id, token


@pytest.fixture
async def api(
    client: httpx.AsyncClient, admin_key: tuple[str, str]
) -> AsyncIterator[httpx.AsyncClient]:
    """Client carrying an admin key. The default for tests that are not about auth."""
    client.headers["Authorization"] = f"Bearer {admin_key[1]}"
    yield client


# --------------------------------------------------------------------- fake receiver


@dataclass
class Receiver:
    """A stand-in for a customer's webhook endpoint.

    Programmable per path so one test can make an endpoint fail twice and then succeed,
    and it verifies the HMAC on arrival - which is how the signing code is checked from
    the receiver's side rather than only against itself.
    """

    base_url: str
    secrets: dict[str, str] = field(default_factory=dict)
    plan: dict[str, list[object]] = field(default_factory=dict)
    received: dict[str, list[dict[str, object]]] = field(default_factory=dict)

    def url_for(self, name: str) -> str:
        return f"{self.base_url}/hook/{name}"

    def register(self, name: str, secret: str, responses: list[object] | None = None) -> None:
        self.secrets[name] = secret
        self.plan[name] = responses or [200]

    def hits(self, name: str) -> list[dict[str, object]]:
        return self.received.get(name, [])


@pytest.fixture(scope="session")
async def receiver() -> AsyncIterator[Receiver]:
    state = Receiver(base_url=f"http://127.0.0.1:{RECEIVER_PORT}")
    app = FastAPI()

    @app.post("/hook/{name}")
    async def hook(name: str, request: Request) -> Response:
        body = await request.body()
        h = request.headers
        valid = signing.verify(
            secret=state.secrets.get(name, "whsec_unknown"),
            message_id=h.get("webhook-id", ""),
            timestamp=int(h.get("webhook-timestamp", "0")),
            body=body,
            header_value=h.get("webhook-signature", ""),
            now=int(time.time()),
        )
        state.received.setdefault(name, []).append(
            {
                "signature_valid": valid,
                "webhook_id": h.get("webhook-id"),
                "user_agent": h.get("user-agent"),
                "content_type": h.get("content-type"),
                "body": json.loads(body),
                "raw_body": body,
            }
        )

        responses = state.plan.get(name, [200])
        action = responses[min(len(state.received[name]) - 1, len(responses) - 1)]
        if action == "slow":
            await asyncio.sleep(5)
            return Response(status_code=200, content=b"late")
        return Response(status_code=int(action), content=f"receiver said {action}".encode())

    config = uvicorn.Config(
        app, host="127.0.0.1", port=RECEIVER_PORT, log_level="error", access_log=False
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # uvicorn exposes no "started" awaitable, so polling the flag is the available option.
    # Bounded so a receiver that fails to bind fails the test instead of hanging the suite.
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.02)
    else:
        raise RuntimeError("test receiver failed to start")
    try:
        yield state
    finally:
        server.should_exit = True
        await task


@pytest.fixture(autouse=True)
def _reset_receiver(request: pytest.FixtureRequest) -> None:
    """Clear recorded traffic between tests without restarting the server.

    Only touches the fixture if the test actually asked for it, so tests that do not use a
    receiver do not pay for starting one.
    """
    if "receiver" not in request.fixturenames:
        return
    state = request.getfixturevalue("receiver")
    state.secrets.clear()
    state.plan.clear()
    state.received.clear()


# --------------------------------------------------------------------- worker


@pytest.fixture
async def worker() -> AsyncIterator[DeliveryWorker]:
    """A real worker, driven one batch at a time.

    `run_once()` rather than `run_forever()`: a polling loop in the background makes tests
    race against a timer, and the resulting flakes get "fixed" with sleeps. Driving the
    batch explicitly means each assertion runs at a known point in the lifecycle.
    """
    from hookline.worker.runner import build_worker

    instance, http = build_worker(get_settings(), get_sessionmaker())
    try:
        yield instance
    finally:
        await http.aclose()


@pytest.fixture
def drain() -> Callable[..., Any]:
    """Poll until three consecutive batches claim nothing."""

    async def _drain(worker: DeliveryWorker, rounds: int = 80) -> None:
        idle = 0
        for _ in range(rounds):
            stats = await worker.run_once()
            idle = idle + 1 if stats.claimed == 0 else 0
            if idle >= 3:
                return
            await asyncio.sleep(0.03)

    return _drain
