import asyncio
from uuid import uuid4

import httpx
import pytest


async def register(api: httpx.AsyncClient, event_types: list[str], url: str | None = None) -> dict:
    target = url or f"https://{uuid4().hex[:8]}.example.com/hooks"
    response = await api.post("/api/v1/endpoints", json={"url": target, "event_types": event_types})
    assert response.status_code == 201
    return response.json()


class TestIngest:
    async def test_returns_202_not_200(self, api: httpx.AsyncClient) -> None:
        """202 is the whole contract: accepted and queued, not delivered.

        Returning 200 would tell the caller their customer received it, which at this point
        is not true and may never be.
        """
        response = await api.post("/api/v1/events", json={"event_type": "a.b", "payload": {}})
        assert response.status_code == 202

    async def test_fans_out_to_subscribers_only(self, api: httpx.AsyncClient) -> None:
        wanted = f"order.created_{uuid4().hex[:6]}"
        both = await register(api, [wanted, "other.thing"])
        only = await register(api, [wanted])
        unrelated = await register(api, ["something.else"])

        response = await api.post(
            "/api/v1/events", json={"event_type": wanted, "payload": {"n": 1}}
        )
        assert response.json()["deliveries_scheduled"] == 2

        rows = (await api.get(f"/api/v1/events/{response.json()['id']}/deliveries")).json()
        assert {r["endpoint_id"] for r in rows} == {both["id"], only["id"]}
        assert unrelated["id"] not in {r["endpoint_id"] for r in rows}

    async def test_deliveries_start_pending_with_no_attempts(self, api: httpx.AsyncClient) -> None:
        event_type = f"e.{uuid4().hex[:6]}"
        await register(api, [event_type])
        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()

        row = (await api.get(f"/api/v1/events/{event['id']}/deliveries")).json()[0]
        assert row["status"] == "pending"
        assert row["attempt_count"] == 0
        assert row["max_attempts"] == 5
        assert row["last_error"] is None

    async def test_no_subscribers_is_still_a_success(self, api: httpx.AsyncClient) -> None:
        """Zero is a valid ingest, and surfacing it turns a silent no-op into something
        a caller can alert on."""
        response = await api.post(
            "/api/v1/events", json={"event_type": f"nobody.{uuid4().hex[:6]}", "payload": {}}
        )
        assert response.status_code == 202
        assert response.json()["deliveries_scheduled"] == 0

    async def test_inactive_endpoints_are_skipped(self, api: httpx.AsyncClient, session) -> None:
        from sqlalchemy import update

        from hookline.models.endpoint import Endpoint

        event_type = f"e.{uuid4().hex[:6]}"
        endpoint = await register(api, [event_type])
        await session.execute(
            update(Endpoint).where(Endpoint.id == endpoint["id"]).values(is_active=False)
        )
        await session.commit()

        response = await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        assert response.json()["deliveries_scheduled"] == 0

    async def test_payload_round_trips_through_jsonb(self, api: httpx.AsyncClient) -> None:
        payload = {"order": {"lines": [{"sku": "A", "qty": 2}], "total": 4500}, "flag": True}
        event = (
            await api.post("/api/v1/events", json={"event_type": "a.b", "payload": payload})
        ).json()
        assert (await api.get(f"/api/v1/events/{event['id']}")).json()["payload"] == payload


class TestIdempotency:
    async def test_replay_returns_the_original(self, api: httpx.AsyncClient) -> None:
        event_type = f"e.{uuid4().hex[:6]}"
        await register(api, [event_type])
        body = {"event_type": event_type, "payload": {"n": 1}}
        headers = {"Idempotency-Key": f"k-{uuid4().hex[:8]}"}

        first = await api.post("/api/v1/events", json=body, headers=headers)
        second = await api.post("/api/v1/events", json=body, headers=headers)

        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["deliveries_scheduled"] == 0

    async def test_replay_header_distinguishes_the_two(self, api: httpx.AsyncClient) -> None:
        """So a caller can tell them apart without diffing bodies."""
        headers = {"Idempotency-Key": f"k-{uuid4().hex[:8]}"}
        body = {"event_type": "a.b", "payload": {}}

        first = await api.post("/api/v1/events", json=body, headers=headers)
        second = await api.post("/api/v1/events", json=body, headers=headers)

        assert first.headers["Idempotent-Replay"] == "false"
        assert second.headers["Idempotent-Replay"] == "true"

    async def test_replay_does_not_double_schedule(self, api: httpx.AsyncClient) -> None:
        event_type = f"e.{uuid4().hex[:6]}"
        await register(api, [event_type])
        headers = {"Idempotency-Key": f"k-{uuid4().hex[:8]}"}
        body = {"event_type": event_type, "payload": {}}

        first = await api.post("/api/v1/events", json=body, headers=headers)
        await api.post("/api/v1/events", json=body, headers=headers)

        rows = (await api.get(f"/api/v1/events/{first.json()['id']}/deliveries")).json()
        assert len(rows) == 1

    async def test_same_key_different_body_returns_the_original(
        self, api: httpx.AsyncClient
    ) -> None:
        """The key identifies the request. Honouring it is the entire point."""
        headers = {"Idempotency-Key": f"k-{uuid4().hex[:8]}"}
        first = await api.post(
            "/api/v1/events", json={"event_type": "a.b", "payload": {"v": 1}}, headers=headers
        )
        second = await api.post(
            "/api/v1/events", json={"event_type": "a.b", "payload": {"v": 999}}, headers=headers
        )
        assert second.json()["id"] == first.json()["id"]

        stored = (await api.get(f"/api/v1/events/{first.json()['id']}")).json()
        assert stored["payload"] == {"v": 1}

    async def test_unkeyed_events_never_collide(self, api: httpx.AsyncClient) -> None:
        """NULL idempotency keys are distinct in Postgres, so unkeyed ingests are independent."""
        body = {"event_type": "a.b", "payload": {"n": 1}}
        ids = {(await api.post("/api/v1/events", json=body)).json()["id"] for _ in range(5)}
        assert len(ids) == 5

    async def test_concurrent_requests_with_one_key_produce_one_event(
        self, api: httpx.AsyncClient
    ) -> None:
        """The reason this uses ON CONFLICT DO NOTHING rather than SELECT-then-INSERT.

        Twenty concurrent callers would all see nothing on the SELECT and all insert.
        Letting Postgres arbitrate means the losers read the winner's row.
        """
        event_type = f"race.{uuid4().hex[:6]}"
        await register(api, [event_type])
        headers = {"Idempotency-Key": f"race-{uuid4().hex[:8]}"}
        body = {"event_type": event_type, "payload": {}}

        responses = await asyncio.gather(
            *[api.post("/api/v1/events", json=body, headers=headers) for _ in range(20)]
        )

        assert {r.status_code for r in responses} == {202}
        assert len({r.json()["id"] for r in responses}) == 1
        assert sum(r.json()["deliveries_scheduled"] for r in responses) == 1

        event_id = responses[0].json()["id"]
        assert len((await api.get(f"/api/v1/events/{event_id}/deliveries")).json()) == 1


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"event_type": "Order.Created", "payload": {}},
            {"event_type": "order created", "payload": {}},
            {"event_type": "", "payload": {}},
            {"event_type": "a.b"},
            {"event_type": "a.b", "payload": [1, 2]},
            {"payload": {}},
            {},
        ],
    )
    async def test_bad_bodies_are_422(self, api: httpx.AsyncClient, body: dict) -> None:
        assert (await api.post("/api/v1/events", json=body)).status_code == 422

    async def test_oversized_payload_is_422(self, api: httpx.AsyncClient) -> None:
        response = await api.post(
            "/api/v1/events", json={"event_type": "a.b", "payload": {"blob": "x" * 300_000}}
        )
        assert response.status_code == 422


class TestReads:
    async def test_pagination_bounds(self, api: httpx.AsyncClient) -> None:
        for _ in range(3):
            await api.post("/api/v1/events", json={"event_type": "a.b", "payload": {}})

        assert len((await api.get("/api/v1/events?limit=2")).json()) == 2
        assert (await api.get("/api/v1/events?limit=0")).status_code == 422
        assert (await api.get("/api/v1/events?limit=500")).status_code == 422
        assert (await api.get("/api/v1/events?offset=-1")).status_code == 422

    async def test_newest_first(self, api: httpx.AsyncClient) -> None:
        ids = [
            (
                await api.post("/api/v1/events", json={"event_type": "a.b", "payload": {"i": i}})
            ).json()["id"]
            for i in range(3)
        ]
        listed = [e["id"] for e in (await api.get("/api/v1/events?limit=3")).json()]
        assert listed == list(reversed(ids))

    async def test_unknown_ids_are_404(self, api: httpx.AsyncClient) -> None:
        missing = uuid4()
        assert (await api.get(f"/api/v1/events/{missing}")).status_code == 404
        assert (await api.get(f"/api/v1/events/{missing}/deliveries")).status_code == 404

    async def test_malformed_uuid_is_422_not_500(self, api: httpx.AsyncClient) -> None:
        assert (await api.get("/api/v1/events/not-a-uuid")).status_code == 422
