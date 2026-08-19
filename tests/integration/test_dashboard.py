"""The operator dashboard: sessions, rendering, scope gating, and replay."""

import re
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from hookline.auth.scopes import Scope
from hookline.dashboard import session as session_store

from .conftest import Receiver


async def login(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/dashboard/login", data={"api_key": token}, follow_redirects=False)


@pytest.fixture
async def viewer(client: httpx.AsyncClient, admin_key: tuple[str, str]) -> httpx.AsyncClient:
    """A client with a dashboard session cookie for an admin key."""
    response = await login(client, admin_key[1])
    assert response.status_code == 303
    return client


class TestSessions:
    async def test_unauthenticated_pages_redirect_to_login(self, client: httpx.AsyncClient) -> None:
        for path in ("/dashboard", "/dashboard/events", "/dashboard/deliveries"):
            response = await client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/dashboard/login")

    async def test_the_redirect_remembers_where_you_were_going(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/dashboard/deliveries?status=dead", follow_redirects=False)
        assert "next=/dashboard/deliveries" in response.headers["location"]

    async def test_login_form_renders(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/dashboard/login")
        assert response.status_code == 200
        assert "<form" in response.text
        assert 'name="api_key"' in response.text

    async def test_bad_key_is_rejected_without_a_cookie(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/dashboard/login", data={"api_key": "hl_nonsense"})
        assert response.status_code == 401
        assert session_store.COOKIE_NAME not in response.cookies

    async def test_good_key_sets_a_hardened_cookie(
        self, client: httpx.AsyncClient, admin_key: tuple[str, str]
    ) -> None:
        response = await login(client, admin_key[1])
        assert response.status_code == 303

        header = response.headers["set-cookie"]
        # httponly stops an XSS bug reading it; strict stops the browser sending it on
        # cross-site requests at all, which is the first line of CSRF defence.
        assert "httponly" in header.lower()
        assert "samesite=strict" in header.lower()

    async def test_the_key_itself_never_reaches_the_browser(
        self, client: httpx.AsyncClient, admin_key: tuple[str, str]
    ) -> None:
        """Redis holds session -> key id, so a stolen cookie is a revocable session rather
        than the credential."""
        _, token = admin_key
        response = await login(client, token)
        assert token not in response.headers["set-cookie"]
        assert token not in response.text

    async def test_login_survives_to_the_next_request(self, viewer: httpx.AsyncClient) -> None:
        assert (await viewer.get("/dashboard")).status_code == 200

    async def test_logout_invalidates_the_session(self, viewer: httpx.AsyncClient) -> None:
        assert (await viewer.post("/dashboard/logout", follow_redirects=False)).status_code == 303
        assert (await viewer.get("/dashboard", follow_redirects=False)).status_code == 303

    async def test_revoking_the_key_ends_the_session_immediately(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient, admin_key: tuple[str, str]
    ) -> None:
        """The key is re-read on every request rather than trusted from the session, so a
        revoked key does not keep working until the session expires eight hours later."""
        key_id, _ = admin_key
        assert (await viewer.get("/dashboard")).status_code == 200

        await api.post(f"/api/v1/api-keys/{key_id}/revoke")

        assert (await viewer.get("/dashboard", follow_redirects=False)).status_code == 303

    async def test_open_redirect_is_refused(
        self, client: httpx.AsyncClient, admin_key: tuple[str, str]
    ) -> None:
        """A `next` pointing off-site would let a phishing link bounce someone away the
        instant they authenticated."""
        response = await client.post(
            "/dashboard/login",
            data={"api_key": admin_key[1], "next": "https://evil.example.com/"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/dashboard"


class TestPages:
    async def test_overview_shows_counts(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        event_type = f"dash.{uuid4().hex[:6]}"
        await api.post(
            "/api/v1/endpoints",
            json={"url": "https://d.example.com/h", "event_types": [event_type]},
        )
        await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})

        response = await viewer.get("/dashboard")
        assert response.status_code == 200
        assert "Overview" in response.text
        assert "pending" in response.text
        assert event_type in response.text

    async def test_overview_warns_when_nothing_is_being_delivered(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        """Queued but never delivered is nearly always a worker that is not running, and
        that is the first thing to check when a webhook does not arrive."""
        event_type = f"dash.{uuid4().hex[:6]}"
        await api.post(
            "/api/v1/endpoints",
            json={"url": "https://d.example.com/h", "event_types": [event_type]},
        )
        await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})

        response = await viewer.get("/dashboard")
        assert "hookline-worker" in response.text

    async def test_event_log_and_filter(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        wanted = f"wanted.{uuid4().hex[:6]}"
        other = f"other.{uuid4().hex[:6]}"
        await api.post("/api/v1/events", json={"event_type": wanted, "payload": {}})
        await api.post("/api/v1/events", json={"event_type": other, "payload": {}})

        unfiltered = await viewer.get("/dashboard/events")
        assert wanted in unfiltered.text
        assert other in unfiltered.text

        filtered = await viewer.get(f"/dashboard/events?event_type={wanted}")
        assert wanted in filtered.text
        assert other not in filtered.text

    async def test_event_with_no_subscribers_says_so(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        await api.post(
            "/api/v1/events", json={"event_type": f"lonely.{uuid4().hex[:6]}", "payload": {}}
        )
        assert "no subscribers" in (await viewer.get("/dashboard/events")).text

    async def test_event_detail_shows_payload_and_attempts(
        self,
        viewer: httpx.AsyncClient,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: Any,
        drain: Callable[..., Any],
    ) -> None:
        event_type = f"detail.{uuid4().hex[:6]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for("dash"), "event_types": [event_type]},
            )
        ).json()
        receiver.register("dash", endpoint["signing_secret"], [503, 200])

        event = (
            await api.post(
                "/api/v1/events",
                json={"event_type": event_type, "payload": {"order_id": 1001}},
            )
        ).json()
        await drain(worker)

        response = await viewer.get(f"/dashboard/events/{event['id']}")
        assert response.status_code == 200
        assert "order_id" in response.text
        assert "1001" in response.text
        assert "delivered" in response.text
        # Both attempts appear, the failure included - that is the point of the log.
        assert "503" in response.text

    async def test_unknown_event_is_404(self, viewer: httpx.AsyncClient) -> None:
        assert (await viewer.get(f"/dashboard/events/{uuid4()}")).status_code == 404

    async def test_delivery_list_defaults_to_the_dead_letter_queue(
        self, viewer: httpx.AsyncClient
    ) -> None:
        response = await viewer.get("/dashboard/deliveries")
        assert "Dead letter queue" in response.text

    async def test_delivery_list_filters_by_status(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        event_type = f"filt.{uuid4().hex[:6]}"
        await api.post(
            "/api/v1/endpoints",
            json={"url": "https://f.example.com/h", "event_types": [event_type]},
        )
        await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})

        pending = await viewer.get("/dashboard/deliveries?status=pending")
        assert "f.example.com" in pending.text

        dead = await viewer.get("/dashboard/deliveries?status=dead")
        assert "f.example.com" not in dead.text

    async def test_unknown_status_is_422(self, viewer: httpx.AsyncClient) -> None:
        assert (await viewer.get("/dashboard/deliveries?status=banana")).status_code == 422

    async def test_endpoints_page_never_shows_a_signing_secret(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        created = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://secret.example.com/h", "event_types": ["a.b"]},
            )
        ).json()

        response = await viewer.get("/dashboard/endpoints")
        assert "secret.example.com" in response.text
        assert created["signing_secret"] not in response.text

    async def test_api_keys_page_never_shows_a_key(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        minted = (
            await api.post("/api/v1/api-keys", json={"name": "shown", "scopes": ["admin"]})
        ).json()

        response = await viewer.get("/dashboard/api-keys")
        assert "shown" in response.text
        assert minted["display_prefix"] in response.text
        assert minted["key"] not in response.text

    async def test_static_css_is_served(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/static/dashboard.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]


class TestScopeGating:
    async def test_a_reader_cannot_see_the_api_keys_page(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        _, token, _ = await make_key("reader", [Scope.EVENTS_READ.value])
        await login(client, token)
        assert (await client.get("/dashboard/api-keys")).status_code == 403

    async def test_a_reader_gets_no_replay_button(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any], api: httpx.AsyncClient
    ) -> None:
        """The button is hidden *and* the endpoint refuses - hiding a control is a UI
        courtesy, not authorisation."""
        _, token, _ = await make_key(
            "read only", [Scope.DELIVERIES_READ.value, Scope.EVENTS_READ.value]
        )
        await login(client, token)

        response = await client.get("/dashboard/deliveries")
        assert response.status_code == 200
        # The form action, not the word - "Replay" also appears in the page's own
        # explanation of what the queue is for.
        assert "/replay" not in response.text

    async def test_a_reader_cannot_replay_even_by_posting(
        self,
        client: httpx.AsyncClient,
        make_key: Callable[..., Any],
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: Any,
        drain: Callable[..., Any],
    ) -> None:
        dead_id = await _make_dead_delivery(api, receiver, worker, drain)

        _, token, _ = await make_key("read only", [Scope.DELIVERIES_READ.value])
        await login(client, token)
        session = await session_store.resolve(
            _redis(), client.cookies.get(session_store.COOKIE_NAME)
        )
        assert session is not None

        response = await client.post(
            f"/dashboard/deliveries/{dead_id}/replay",
            data={"csrf_token": session.csrf_token},
        )
        assert response.status_code == 403


def _redis():
    from hookline.cache.client import get_redis

    return get_redis()


async def _make_dead_delivery(
    api: httpx.AsyncClient, receiver: Receiver, worker: Any, drain: Callable[..., Any]
) -> str:
    name = f"dead-{uuid4().hex[:6]}"
    event_type = f"dead.{uuid4().hex[:6]}"
    endpoint = (
        await api.post(
            "/api/v1/endpoints",
            json={"url": receiver.url_for(name), "event_types": [event_type]},
        )
    ).json()
    receiver.register(name, endpoint["signing_secret"], [400])

    event = (
        await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
    ).json()
    await drain(worker)

    rows = (await api.get(f"/api/v1/events/{event['id']}/deliveries")).json()
    assert rows[0]["status"] == "dead"
    return str(rows[0]["id"])


class TestReplay:
    async def test_replay_without_javascript_redirects_back(
        self,
        viewer: httpx.AsyncClient,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: Any,
        drain: Callable[..., Any],
    ) -> None:
        """The button is a real form, so the dashboard works with JavaScript disabled."""
        dead_id = await _make_dead_delivery(api, receiver, worker, drain)
        csrf = await _csrf_for(viewer)

        response = await viewer.post(
            f"/dashboard/deliveries/{dead_id}/replay",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard/deliveries"

        after = (await api.get(f"/api/v1/deliveries/{dead_id}")).json()
        assert after["status"] == "pending"
        # Budget extended from wherever it died, not reset to a fixed number. This delivery
        # died after one attempt (a 400 is not retryable), so it gets 1 + 5.
        assert after["max_attempts"] == after["attempt_count"] + 5

    async def test_replay_with_htmx_returns_just_the_row(
        self,
        viewer: httpx.AsyncClient,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: Any,
        drain: Callable[..., Any],
    ) -> None:
        dead_id = await _make_dead_delivery(api, receiver, worker, drain)
        csrf = await _csrf_for(viewer)

        response = await viewer.post(
            f"/dashboard/deliveries/{dead_id}/replay",
            data={"csrf_token": csrf},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        # A fragment, not a page: no layout, and it is the row that was targeted.
        assert "<html" not in response.text
        assert f"delivery-{dead_id}" in response.text
        assert "requeued" in response.text

    async def test_missing_csrf_token_is_refused(
        self,
        viewer: httpx.AsyncClient,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: Any,
        drain: Callable[..., Any],
    ) -> None:
        dead_id = await _make_dead_delivery(api, receiver, worker, drain)

        response = await viewer.post(f"/dashboard/deliveries/{dead_id}/replay", data={})
        assert response.status_code == 403

        unchanged = (await api.get(f"/api/v1/deliveries/{dead_id}")).json()
        assert unchanged["status"] == "dead"

    async def test_wrong_csrf_token_is_refused(
        self,
        viewer: httpx.AsyncClient,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: Any,
        drain: Callable[..., Any],
    ) -> None:
        dead_id = await _make_dead_delivery(api, receiver, worker, drain)
        response = await viewer.post(
            f"/dashboard/deliveries/{dead_id}/replay", data={"csrf_token": "not-it"}
        )
        assert response.status_code == 403

    async def test_replaying_a_live_delivery_is_a_conflict(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        event_type = f"live.{uuid4().hex[:6]}"
        await api.post(
            "/api/v1/endpoints",
            json={"url": "https://live.example.com/h", "event_types": [event_type]},
        )
        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()
        delivery = (await api.get(f"/api/v1/events/{event['id']}/deliveries")).json()[0]

        csrf = await _csrf_for(viewer)
        response = await viewer.post(
            f"/dashboard/deliveries/{delivery['id']}/replay", data={"csrf_token": csrf}
        )
        assert response.status_code == 409


async def _csrf_for(client: httpx.AsyncClient) -> str:
    stored = await session_store.resolve(_redis(), client.cookies.get(session_store.COOKIE_NAME))
    assert stored is not None
    return stored.csrf_token


class TestProgressiveEnhancement:
    async def test_htmx_gets_a_fragment_and_a_browser_gets_a_page(
        self, viewer: httpx.AsyncClient
    ) -> None:
        """One handler, two renderings, so the enhanced and unenhanced paths cannot drift."""
        page = await viewer.get("/dashboard/events")
        fragment = await viewer.get("/dashboard/events", headers={"HX-Request": "true"})

        assert "<html" in page.text
        assert "<html" not in fragment.text
        assert page.status_code == fragment.status_code == 200

    async def test_every_hx_post_is_also_a_real_form(
        self,
        viewer: httpx.AsyncClient,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: Any,
        drain: Callable[..., Any],
    ) -> None:
        """No action may exist only as an hx-* attribute, or it stops working without JS.

        Needs a dead delivery to exist, otherwise there is no replay form on the page and
        the assertion passes for the wrong reason.
        """
        await _make_dead_delivery(api, receiver, worker, drain)
        body = (await viewer.get("/dashboard/deliveries")).text

        forms = re.findall(r"<form\b[^>]*>", body, flags=re.DOTALL)
        hx_forms = [f for f in forms if "hx-post" in f]
        assert hx_forms, "expected a replay form on the page"
        for form in hx_forms:
            assert 'method="post"' in form
            assert "action=" in form


class TestEscaping:
    async def test_payload_content_is_escaped(
        self, viewer: httpx.AsyncClient, api: httpx.AsyncClient
    ) -> None:
        """Payloads are attacker-controlled - they come from whatever the API caller sent.

        Jinja2 autoescapes by default; this asserts nobody has turned it off or reached for
        `| safe` on the payload.
        """
        event = (
            await api.post(
                "/api/v1/events",
                json={
                    "event_type": "xss.test",
                    "payload": {"note": "<script>alert('x')</script>"},
                },
            )
        ).json()

        response = await viewer.get(f"/dashboard/events/{event['id']}")
        assert "<script>alert" not in response.text
        assert "&lt;script&gt;" in response.text

    async def test_event_type_is_escaped_in_the_log(self, viewer: httpx.AsyncClient) -> None:
        """The type is pattern-validated at ingest, so this is defence in depth rather than
        the only thing standing in the way."""
        response = await viewer.get("/dashboard/events?event_type=<b>bold</b>")
        assert "<b>bold</b>" not in response.text


class TestDisabled:
    async def test_dashboard_can_be_turned_off(self, monkeypatch) -> None:
        """An API-only deployment should not expose operator pages at all."""
        from hookline.config import get_settings
        from hookline.main import create_app

        get_settings.cache_clear()
        monkeypatch.setenv("HOOKLINE_DASHBOARD_ENABLED", "false")

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/dashboard/login")).status_code == 404
            # The API is unaffected.
            assert (await c.get("/health")).status_code == 200

        get_settings.cache_clear()
