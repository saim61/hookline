from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from hookline.auth.keys import generate_key
from hookline.auth.scopes import Scope


class TestUnauthenticated:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/endpoints"),
            ("POST", "/api/v1/endpoints"),
            ("GET", "/api/v1/events"),
            ("POST", "/api/v1/events"),
            ("GET", "/api/v1/deliveries"),
            ("GET", "/api/v1/api-keys"),
        ],
    )
    async def test_every_api_route_requires_a_key(
        self, client: httpx.AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(method, path, json={})
        assert response.status_code == 401

    async def test_401_advertises_the_scheme(self, client: httpx.AsyncClient) -> None:
        """Without WWW-Authenticate a client library has nothing to act on."""
        response = await client.get("/api/v1/endpoints")
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("value", ["", "garbage", "Basic abc", "hl_", "Bearer"])
    async def test_malformed_credentials(self, client: httpx.AsyncClient, value: str) -> None:
        response = await client.get(
            "/api/v1/endpoints", headers={"Authorization": f"Bearer {value}"}
        )
        assert response.status_code == 401

    async def test_well_formed_but_unknown_key(self, client: httpx.AsyncClient) -> None:
        token, _, _ = generate_key()
        response = await client.get(
            "/api/v1/endpoints", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("path", ["/health", "/ready", "/metrics", "/docs", "/openapi.json"])
    async def test_probes_and_docs_stay_open(self, client: httpx.AsyncClient, path: str) -> None:
        """A load balancer has no credential, and a probe that can fail on auth reports
        the wrong thing."""
        assert (await client.get(path)).status_code == 200


class TestScopes:
    async def test_write_scope_cannot_read(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        _, token, _ = await make_key("writer", [Scope.EVENTS_WRITE.value])
        headers = {"Authorization": f"Bearer {token}"}

        assert (
            await client.post(
                "/api/v1/events", json={"event_type": "a.b", "payload": {}}, headers=headers
            )
        ).status_code == 202
        assert (await client.get("/api/v1/events", headers=headers)).status_code == 403

    async def test_403_names_the_missing_scope(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        """403 and 401 are kept distinct: one is "who are you", the other "you may not".

        Conflating them sends people hunting for a credential problem when they have a
        permissions problem.
        """
        _, token, _ = await make_key("writer", [Scope.EVENTS_WRITE.value])
        response = await client.get("/api/v1/events", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
        assert "events:read" in response.json()["detail"]

    async def test_scopes_do_not_cross_resources(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        _, token, _ = await make_key("reader", [Scope.ENDPOINTS_READ.value])
        headers = {"Authorization": f"Bearer {token}"}

        assert (await client.get("/api/v1/endpoints", headers=headers)).status_code == 200
        assert (
            await client.post(
                "/api/v1/endpoints",
                json={"url": "https://a.example.com/h", "event_types": []},
                headers=headers,
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/events", json={"event_type": "a.b", "payload": {}}, headers=headers
            )
        ).status_code == 403

    async def test_admin_reaches_everything(self, api: httpx.AsyncClient) -> None:
        paths = (
            "/api/v1/endpoints",
            "/api/v1/events",
            "/api/v1/deliveries",
            "/api/v1/api-keys",
        )
        for path in paths:
            assert (await api.get(path)).status_code == 200

    async def test_only_admin_manages_keys(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        """A key that can mint keys can grant itself anything, so there is no narrower scope."""
        _, token, _ = await make_key("writer", [Scope.EVENTS_WRITE.value])
        headers = {"Authorization": f"Bearer {token}"}

        assert (await client.get("/api/v1/api-keys", headers=headers)).status_code == 403
        assert (
            await client.post(
                "/api/v1/api-keys",
                json={"name": "escalation", "scopes": ["admin"]},
                headers=headers,
            )
        ).status_code == 403


class TestKeyLifecycle:
    async def test_key_is_returned_exactly_once(self, api: httpx.AsyncClient) -> None:
        created = await api.post(
            "/api/v1/api-keys", json={"name": "once", "scopes": ["events:write"]}
        )
        assert created.status_code == 201
        minted = created.json()
        assert minted["key"].startswith("hl_")

        listed = (await api.get("/api/v1/api-keys")).json()
        row = next(k for k in listed if k["id"] == minted["id"])
        assert "key" not in row
        assert "inbound_signing_secret" not in row

        single = (await api.get(f"/api/v1/api-keys/{minted['id']}")).json()
        assert "key" not in single
        # The prefix survives, which is what makes a key identifiable in a log line.
        assert single["display_prefix"] == minted["key"][:11]

    async def test_a_minted_key_works(self, api: httpx.AsyncClient) -> None:
        minted = (
            await api.post("/api/v1/api-keys", json={"name": "usable", "scopes": ["events:write"]})
        ).json()
        response = await api.post(
            "/api/v1/events",
            json={"event_type": "a.b", "payload": {}},
            headers={"Authorization": f"Bearer {minted['key']}"},
        )
        assert response.status_code == 202

    async def test_revocation_takes_effect_immediately(self, api: httpx.AsyncClient) -> None:
        minted = (
            await api.post("/api/v1/api-keys", json={"name": "doomed", "scopes": ["events:write"]})
        ).json()
        headers = {"Authorization": f"Bearer {minted['key']}"}
        body = {"event_type": "a.b", "payload": {}}
        before = await api.post("/api/v1/events", json=body, headers=headers)
        assert before.status_code == 202

        revoked = await api.post(f"/api/v1/api-keys/{minted['id']}/revoke")
        assert revoked.status_code == 200
        assert revoked.json()["is_active"] is False

        after = await api.post("/api/v1/events", json=body, headers=headers)
        assert after.status_code == 401

    async def test_revoked_row_survives_for_audit(self, api: httpx.AsyncClient) -> None:
        """During an incident, "what was this key doing before we killed it" is the question."""
        minted = (
            await api.post("/api/v1/api-keys", json={"name": "audited", "scopes": ["admin"]})
        ).json()
        await api.post(f"/api/v1/api-keys/{minted['id']}/revoke")

        row = await api.get(f"/api/v1/api-keys/{minted['id']}")
        assert row.status_code == 200
        assert row.json()["name"] == "audited"

    async def test_double_revoke_is_a_conflict(self, api: httpx.AsyncClient) -> None:
        minted = (
            await api.post("/api/v1/api-keys", json={"name": "x", "scopes": ["admin"]})
        ).json()
        assert (await api.post(f"/api/v1/api-keys/{minted['id']}/revoke")).status_code == 200
        assert (await api.post(f"/api/v1/api-keys/{minted['id']}/revoke")).status_code == 409

    async def test_revoking_an_unknown_key_is_404(self, api: httpx.AsyncClient) -> None:
        assert (await api.post(f"/api/v1/api-keys/{uuid4()}/revoke")).status_code == 404

    async def test_expired_key_is_refused(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        _, token, _ = await make_key(
            "stale",
            [Scope.EVENTS_WRITE.value],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        response = await client.post(
            "/api/v1/events",
            json={"event_type": "a.b", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    async def test_unexpired_key_still_works(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        _, token, _ = await make_key(
            "fresh", [Scope.EVENTS_WRITE.value], expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
        response = await client.post(
            "/api/v1/events",
            json={"event_type": "a.b", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202

    async def test_revoked_and_unknown_look_identical(
        self, api: httpx.AsyncClient, client: httpx.AsyncClient
    ) -> None:
        """Deliberate. Telling a caller "revoked" confirms the leaked key was real."""
        minted = (
            await api.post("/api/v1/api-keys", json={"name": "x", "scopes": ["events:write"]})
        ).json()
        await api.post(f"/api/v1/api-keys/{minted['id']}/revoke")

        unknown, _, _ = generate_key()
        revoked_response = await client.get(
            "/api/v1/endpoints", headers={"Authorization": f"Bearer {minted['key']}"}
        )
        unknown_response = await client.get(
            "/api/v1/endpoints", headers={"Authorization": f"Bearer {unknown}"}
        )
        assert revoked_response.status_code == unknown_response.status_code == 401
        assert revoked_response.json() == unknown_response.json()

    async def test_last_used_at_is_recorded(
        self, api: httpx.AsyncClient, admin_key: tuple[str, str]
    ) -> None:
        key_id, _ = admin_key
        row = (await api.get(f"/api/v1/api-keys/{key_id}")).json()
        assert row["last_used_at"] is not None


class TestSignedRequests:
    async def test_signed_key_rejects_unsigned_requests(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        _, token, secret = await make_key(
            "signed", [Scope.EVENTS_WRITE.value], with_inbound_signing=True
        )
        assert secret is not None

        response = await client.post(
            "/api/v1/events",
            json={"event_type": "a.b", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert "signed requests" in response.json()["detail"]

    async def test_correctly_signed_request_is_accepted(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        import json
        import time

        from hookline.delivery import signing

        _, token, secret = await make_key(
            "signed", [Scope.EVENTS_WRITE.value], with_inbound_signing=True
        )
        assert secret is not None

        body = json.dumps({"event_type": "a.b", "payload": {"x": 1}}).encode()
        message_id, timestamp = str(uuid4()), int(time.time())
        response = await client.post(
            "/api/v1/events",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "content-type": "application/json",
                "webhook-id": message_id,
                "webhook-timestamp": str(timestamp),
                "webhook-signature": signing.sign(secret, message_id, timestamp, body),
            },
        )
        assert response.status_code == 202

    @pytest.mark.parametrize(
        "mutate",
        ["body", "secret", "timestamp", "non_numeric_timestamp", "missing_headers"],
    )
    async def test_every_way_of_getting_it_wrong_is_rejected(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any], mutate: str
    ) -> None:
        import json
        import time

        from hookline.delivery import signing

        _, token, secret = await make_key(
            "signed", [Scope.EVENTS_WRITE.value], with_inbound_signing=True
        )
        assert secret is not None

        body = json.dumps({"event_type": "a.b", "payload": {"x": 1}}).encode()
        message_id, timestamp = str(uuid4()), int(time.time())
        headers = {
            "Authorization": f"Bearer {token}",
            "content-type": "application/json",
            "webhook-id": message_id,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": signing.sign(secret, message_id, timestamp, body),
        }

        if mutate == "body":
            body = json.dumps({"event_type": "a.b", "payload": {"x": 2}}).encode()
        elif mutate == "secret":
            headers["webhook-signature"] = signing.sign("whsec_wrong", message_id, timestamp, body)
        elif mutate == "timestamp":
            # Signed correctly, but an hour old: replay protection catches it.
            old = timestamp - 3600
            headers["webhook-timestamp"] = str(old)
            headers["webhook-signature"] = signing.sign(secret, message_id, old, body)
        elif mutate == "non_numeric_timestamp":
            headers["webhook-timestamp"] = "soon"
        elif mutate == "missing_headers":
            del headers["webhook-signature"]

        response = await client.post("/api/v1/events", content=body, headers=headers)
        assert response.status_code == 401

    async def test_unsigned_keys_are_unaffected(
        self, client: httpx.AsyncClient, make_key: Callable[..., Any]
    ) -> None:
        _, token, secret = await make_key("plain", [Scope.EVENTS_WRITE.value])
        assert secret is None
        response = await client.post(
            "/api/v1/events",
            json={"event_type": "a.b", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202
