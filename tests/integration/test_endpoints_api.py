from uuid import uuid4

import httpx


class TestCreate:
    async def test_returns_the_signing_secret_once(self, api: httpx.AsyncClient) -> None:
        created = await api.post(
            "/api/v1/endpoints",
            json={"url": "https://merchant.example.com/hooks", "event_types": ["order.created"]},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["signing_secret"].startswith("whsec_")

    async def test_secret_is_absent_from_every_later_read(self, api: httpx.AsyncClient) -> None:
        """Enforced by using a different response model, not by remembering to delete a key.

        `response_model=EndpointRead` drops unknown fields, so the secret cannot leak even
        though the handler returns the full ORM object.
        """
        created = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://a.example.com/hooks", "event_types": []},
            )
        ).json()

        single = (await api.get(f"/api/v1/endpoints/{created['id']}")).json()
        assert "signing_secret" not in single

        listed = (await api.get("/api/v1/endpoints")).json()
        assert all("signing_secret" not in row for row in listed)

    async def test_secrets_are_unique_per_endpoint(self, api: httpx.AsyncClient) -> None:
        secrets = set()
        for _ in range(5):
            created = await api.post(
                "/api/v1/endpoints",
                json={"url": "https://a.example.com/hooks", "event_types": []},
            )
            secrets.add(created.json()["signing_secret"])
        assert len(secrets) == 5

    async def test_defaults(self, api: httpx.AsyncClient) -> None:
        created = (
            await api.post("/api/v1/endpoints", json={"url": "https://a.example.com/hooks"})
        ).json()
        assert created["is_active"] is True
        assert created["event_types"] == []
        assert created["description"] is None
        assert created["created_at"] is not None

    async def test_invalid_input_is_422(self, api: httpx.AsyncClient) -> None:
        assert (await api.post("/api/v1/endpoints", json={"url": "not-a-url"})).status_code == 422
        assert (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://a.example.com/h", "event_types": ["a", "a"]},
            )
        ).status_code == 422


class TestReadAndDelete:
    async def test_get_by_id(self, api: httpx.AsyncClient) -> None:
        created = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://a.example.com/hooks", "event_types": ["a.b"]},
            )
        ).json()
        fetched = await api.get(f"/api/v1/endpoints/{created['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["event_types"] == ["a.b"]

    async def test_delete_then_gone(self, api: httpx.AsyncClient) -> None:
        created = (
            await api.post("/api/v1/endpoints", json={"url": "https://a.example.com/hooks"})
        ).json()

        assert (await api.delete(f"/api/v1/endpoints/{created['id']}")).status_code == 204
        assert (await api.get(f"/api/v1/endpoints/{created['id']}")).status_code == 404

    async def test_delete_is_not_idempotent_by_design(self, api: httpx.AsyncClient) -> None:
        """A second delete returns 404, not 204.

        204 would tell the caller they deleted something when the id was wrong - which
        hides typos and stale ids.
        """
        created = (
            await api.post("/api/v1/endpoints", json={"url": "https://a.example.com/hooks"})
        ).json()
        assert (await api.delete(f"/api/v1/endpoints/{created['id']}")).status_code == 204
        assert (await api.delete(f"/api/v1/endpoints/{created['id']}")).status_code == 404

    async def test_unknown_id_is_404(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(f"/api/v1/endpoints/{uuid4()}")).status_code == 404

    async def test_malformed_uuid_is_422(self, api: httpx.AsyncClient) -> None:
        assert (await api.get("/api/v1/endpoints/nope")).status_code == 422


class TestCascade:
    async def test_deleting_an_endpoint_removes_its_deliveries(
        self, api: httpx.AsyncClient
    ) -> None:
        """ON DELETE CASCADE, so no orphan delivery rows point at a gone endpoint."""
        event_type = f"e.{uuid4().hex[:6]}"
        keep = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://keep.example.com/h", "event_types": [event_type]},
            )
        ).json()
        remove = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://remove.example.com/h", "event_types": [event_type]},
            )
        ).json()

        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()
        assert event["deliveries_scheduled"] == 2

        await api.delete(f"/api/v1/endpoints/{remove['id']}")

        rows = (await api.get(f"/api/v1/events/{event['id']}/deliveries")).json()
        assert len(rows) == 1
        assert rows[0]["endpoint_id"] == keep["id"]

    async def test_the_event_itself_survives(self, api: httpx.AsyncClient) -> None:
        """The audit trail outlives the destination it was going to."""
        event_type = f"e.{uuid4().hex[:6]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://a.example.com/h", "event_types": [event_type]},
            )
        ).json()
        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()

        await api.delete(f"/api/v1/endpoints/{endpoint['id']}")
        assert (await api.get(f"/api/v1/events/{event['id']}")).status_code == 200
