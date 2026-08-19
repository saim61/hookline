"""The delivery worker, end to end against Postgres and a fake receiver."""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from hookline.config import get_settings
from hookline.db.session import get_sessionmaker
from hookline.repositories.delivery import DeliveryRepository
from hookline.worker.runner import DeliveryWorker, build_worker

from .conftest import Receiver


async def setup_delivery(
    api: httpx.AsyncClient,
    receiver: Receiver,
    name: str,
    responses: list[object] | None = None,
) -> tuple[dict, dict]:
    """Register an endpoint pointing at the receiver, then ingest one event for it."""
    event_type = f"e.{uuid4().hex[:8]}"
    endpoint = (
        await api.post(
            "/api/v1/endpoints",
            json={"url": receiver.url_for(name), "event_types": [event_type]},
        )
    ).json()
    receiver.register(name, endpoint["signing_secret"], responses)

    event = (
        await api.post("/api/v1/events", json={"event_type": event_type, "payload": {"n": 1}})
    ).json()
    return endpoint, event


async def delivery_of(api: httpx.AsyncClient, event_id: str) -> dict:
    return (await api.get(f"/api/v1/events/{event_id}/deliveries")).json()[0]


class TestSuccessfulDelivery:
    async def test_delivers_and_records_the_attempt(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        name = f"ok-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [200])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == "delivered"
        assert delivery["attempt_count"] == 1
        assert delivery["last_error"] is None

        attempts = (await api.get(f"/api/v1/deliveries/{delivery['id']}/attempts")).json()
        assert len(attempts) == 1
        assert attempts[0]["attempt_number"] == 1
        assert attempts[0]["status_code"] == 200
        assert attempts[0]["error"] is None
        assert attempts[0]["response_body"] == "receiver said 200"
        assert attempts[0]["duration_ms"] >= 0

    async def test_the_receiver_can_verify_the_signature(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        """Verified by the receiver's own independent check, not by our signing code."""
        name = f"sig-{uuid4().hex[:6]}"
        await setup_delivery(api, receiver, name, [200])
        await drain(worker)

        hit = receiver.hits(name)[0]
        assert hit["signature_valid"] is True
        assert hit["content_type"] == "application/json"
        assert hit["user_agent"] == "hookline/0.1.0"

    async def test_webhook_id_is_the_delivery_id(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        """Unique per destination and stable across retries, which is what makes it usable
        as a deduplication key on the receiving side."""
        name = f"id-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [200])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert receiver.hits(name)[0]["webhook_id"] == delivery["id"]

    async def test_envelope_contents(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        name = f"env-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [200])
        await drain(worker)

        body = receiver.hits(name)[0]["body"]
        assert isinstance(body, dict)
        assert body["id"] == event["id"]
        assert body["data"] == {"n": 1}
        assert "created_at" in body


class TestRetries:
    async def test_retries_then_succeeds(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        name = f"flaky-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [503, 503, 200])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == "delivered"
        assert delivery["attempt_count"] == 3

        attempts = (await api.get(f"/api/v1/deliveries/{delivery['id']}/attempts")).json()
        assert [a["attempt_number"] for a in attempts] == [1, 2, 3]
        assert [a["status_code"] for a in attempts] == [503, 503, 200]
        assert attempts[0]["error"] == "endpoint returned 503"

    async def test_retries_reuse_the_same_webhook_id(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        """Otherwise a receiver deduplicating on it would process the same event twice."""
        name = f"stable-{uuid4().hex[:6]}"
        await setup_delivery(api, receiver, name, [503, 503, 200])
        await drain(worker)

        assert len({h["webhook_id"] for h in receiver.hits(name)}) == 1

    async def test_timeout_is_retryable(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        name = f"slow-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, ["slow", 200])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == "delivered"

        attempts = (await api.get(f"/api/v1/deliveries/{delivery['id']}/attempts")).json()
        assert attempts[0]["status_code"] is None
        assert "Timeout" in attempts[0]["error"]

    async def test_429_is_retried_unlike_other_4xx(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        name = f"rl-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [429, 200])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == "delivered"
        assert delivery["attempt_count"] == 2


class TestDeadLettering:
    async def test_exhausted_budget_goes_to_the_dlq(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        name = f"down-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [503])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == "dead"
        assert delivery["attempt_count"] == get_settings().max_delivery_attempts
        assert "gave up after 5 attempts" in delivery["last_error"]

        attempts = (await api.get(f"/api/v1/deliveries/{delivery['id']}/attempts")).json()
        assert len(attempts) == 5

        dlq = (await api.get("/api/v1/deliveries?status=dead")).json()
        assert delivery["id"] in {row["id"] for row in dlq}

    async def test_non_retryable_response_dies_after_one_attempt(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        """A 400 will be a 400 next time too, so spending four more attempts is waste."""
        name = f"bad-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [400])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == "dead"
        assert delivery["attempt_count"] == 1
        assert "not retryable" in delivery["last_error"]


class TestReplay:
    async def test_replay_extends_the_budget_and_appends_to_history(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        """Raising max_attempts rather than zeroing attempt_count keeps attempt_number
        monotonic, so replayed attempts append instead of colliding with the unique
        constraint - and the record of why it died stays readable."""
        name = f"replay-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [503])
        await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == "dead"

        receiver.plan[name] = [200]  # they fixed their server
        replayed = await api.post(f"/api/v1/deliveries/{delivery['id']}/replay")
        assert replayed.status_code == 200
        assert replayed.json()["status"] == "pending"
        assert replayed.json()["attempt_count"] == 5
        assert replayed.json()["max_attempts"] == 10

        await drain(worker)
        after = (await api.get(f"/api/v1/deliveries/{delivery['id']}")).json()
        assert after["status"] == "delivered"
        assert after["attempt_count"] == 6

        attempts = (await api.get(f"/api/v1/deliveries/{delivery['id']}/attempts")).json()
        assert [a["attempt_number"] for a in attempts] == [1, 2, 3, 4, 5, 6]
        assert attempts[0]["status_code"] == 503

    @pytest.mark.parametrize("status", ["pending", "delivered"])
    async def test_only_dead_deliveries_can_be_replayed(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
        status: str,
    ) -> None:
        """Replaying a pending one hands it to a second worker; replaying a delivered one
        sends the customer a duplicate. Both are refused rather than silently ignored."""
        name = f"guard-{uuid4().hex[:6]}"
        _, event = await setup_delivery(api, receiver, name, [200])
        if status == "delivered":
            await drain(worker)

        delivery = await delivery_of(api, event["id"])
        assert delivery["status"] == status

        response = await api.post(f"/api/v1/deliveries/{delivery['id']}/replay")
        assert response.status_code == 409
        assert status in response.json()["detail"]

    async def test_replaying_an_unknown_delivery_is_404(self, api: httpx.AsyncClient) -> None:
        assert (await api.post(f"/api/v1/deliveries/{uuid4()}/replay")).status_code == 404


class TestFanOutIsolation:
    async def test_one_event_two_endpoints_independent_outcomes(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        """The reason retry state lives on deliveries and not on events."""
        event_type = f"fan.{uuid4().hex[:6]}"
        good_name, bad_name = f"good-{uuid4().hex[:4]}", f"bad-{uuid4().hex[:4]}"

        good = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for(good_name), "event_types": [event_type]},
            )
        ).json()
        bad = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for(bad_name), "event_types": [event_type]},
            )
        ).json()
        receiver.register(good_name, good["signing_secret"], [200])
        receiver.register(bad_name, bad["signing_secret"], [400])

        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()
        await drain(worker)

        rows = (await api.get(f"/api/v1/events/{event['id']}/deliveries")).json()
        by_endpoint = {r["endpoint_id"]: r["status"] for r in rows}
        assert by_endpoint[good["id"]] == "delivered"
        assert by_endpoint[bad["id"]] == "dead"


class TestClaiming:
    async def test_skip_locked_gives_workers_disjoint_batches(
        self, api: httpx.AsyncClient, receiver: Receiver
    ) -> None:
        """The property that makes this a queue rather than a contention point.

        Plain FOR UPDATE would make worker B block on worker A's transaction, so N workers
        would deliver at the throughput of one.
        """
        event_type = f"conc.{uuid4().hex[:6]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for("conc"), "event_types": [event_type]},
            )
        ).json()
        receiver.register("conc", endpoint["signing_secret"], [200])

        for i in range(12):
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {"i": i}})

        sessionmaker = get_sessionmaker()

        async def claim(limit: int) -> set[str]:
            async with sessionmaker() as session:
                jobs = await DeliveryRepository(session).claim_batch(limit)
                await session.commit()
            return {str(j.delivery_id) for j in jobs}

        first, second = await asyncio.gather(claim(6), claim(6))

        assert first and second
        assert first & second == set()
        assert len(first | second) == 12

    async def test_claiming_marks_rows_in_flight(
        self, api: httpx.AsyncClient, receiver: Receiver
    ) -> None:
        """Committed immediately, so the rows stop matching `status = 'pending'` while the
        slow HTTP call runs outside any transaction."""
        event_type = f"claim.{uuid4().hex[:6]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for("claim"), "event_types": [event_type]},
            )
        ).json()
        receiver.register("claim", endpoint["signing_secret"], [200])
        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()

        async with get_sessionmaker()() as session:
            await DeliveryRepository(session).claim_batch(10)
            await session.commit()

        assert (await delivery_of(api, event["id"]))["status"] == "in_flight"

    async def test_a_claimed_row_is_not_claimed_again(
        self, api: httpx.AsyncClient, receiver: Receiver
    ) -> None:
        event_type = f"once.{uuid4().hex[:6]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for("once"), "event_types": [event_type]},
            )
        ).json()
        receiver.register("once", endpoint["signing_secret"], [200])
        await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})

        async with get_sessionmaker()() as session:
            first = await DeliveryRepository(session).claim_batch(10)
            await session.commit()
        async with get_sessionmaker()() as session:
            second = await DeliveryRepository(session).claim_batch(10)
            await session.commit()

        assert len(first) == 1
        assert second == []


class TestReaper:
    async def test_abandoned_in_flight_rows_are_returned_to_pending(
        self, api: httpx.AsyncClient, receiver: Receiver
    ) -> None:
        """Closes the crash window between claiming and recording.

        Without this a worker killed mid-batch strands its rows for ever: invisible to
        every other worker, never retried, never dead-lettered. That is the failure mode
        that turns at-least-once into sometimes-never.
        """
        event_type = f"reap.{uuid4().hex[:6]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for("reap"), "event_types": [event_type]},
            )
        ).json()
        receiver.register("reap", endpoint["signing_secret"], [200])
        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()

        async with get_sessionmaker()() as session:
            await DeliveryRepository(session).claim_batch(10)
            await session.commit()
        assert (await delivery_of(api, event["id"]))["status"] == "in_flight"

        # Nothing is stale yet, so the reaper must leave it alone.
        async with get_sessionmaker()() as session:
            assert await DeliveryRepository(session).reap_stale(60.0) == 0
            await session.commit()

        await asyncio.sleep(1.1)  # stale timeout is 1s in the test settings

        async with get_sessionmaker()() as session:
            assert await DeliveryRepository(session).reap_stale(1.0) == 1
            await session.commit()

        row = await delivery_of(api, event["id"])
        assert row["status"] == "pending"
        assert "reclaimed" in row["last_error"]

    async def test_a_reclaimed_delivery_still_gets_delivered(
        self,
        api: httpx.AsyncClient,
        receiver: Receiver,
        worker: DeliveryWorker,
        drain: Callable[..., Any],
    ) -> None:
        event_type = f"reap2.{uuid4().hex[:6]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": receiver.url_for("reap2"), "event_types": [event_type]},
            )
        ).json()
        receiver.register("reap2", endpoint["signing_secret"], [200])
        event = (
            await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        ).json()

        async with get_sessionmaker()() as session:
            await DeliveryRepository(session).claim_batch(10)
            await session.commit()

        await asyncio.sleep(1.1)
        await drain(worker)

        assert (await delivery_of(api, event["id"]))["status"] == "delivered"


class TestCircuitBreakerInTheLoop:
    async def test_the_breaker_fires_inside_a_single_batch(
        self, api: httpx.AsyncClient, receiver: Receiver
    ) -> None:
        """Its own worker with a real threshold.

        This is also a regression test for a real bug: the first implementation gathered
        over the flat batch, so all N coroutines passed the `allows()` check before the
        first failure was recorded and a dead endpoint still received the whole batch.
        Grouping by endpoint and going sequentially within a group is what fixed it.
        """
        settings = get_settings().model_copy(
            update={"circuit_breaker_failure_threshold": 3, "worker_batch_size": 25}
        )
        cb_worker, http = build_worker(settings, get_sessionmaker())

        try:
            event_type = f"cb.{uuid4().hex[:6]}"
            endpoint = (
                await api.post(
                    "/api/v1/endpoints",
                    json={"url": receiver.url_for("cb"), "event_types": [event_type]},
                )
            ).json()
            receiver.register("cb", endpoint["signing_secret"], [503])

            for i in range(8):
                await api.post(
                    "/api/v1/events", json={"event_type": event_type, "payload": {"i": i}}
                )

            stats = await cb_worker.run_once()
            assert stats.claimed == 8
            assert stats.skipped_open_circuit > 0
            assert len(receiver.hits("cb")) < 8
            assert stats.retrying + stats.dead + stats.skipped_open_circuit == 8
        finally:
            await http.aclose()

    async def test_a_skip_costs_no_attempt(
        self, api: httpx.AsyncClient, receiver: Receiver
    ) -> None:
        """The endpoint is known to be down, so charging the delivery for it would be
        unfair to the delivery."""
        settings = get_settings().model_copy(
            update={"circuit_breaker_failure_threshold": 2, "worker_batch_size": 25}
        )
        cb_worker, http = build_worker(settings, get_sessionmaker())

        try:
            event_type = f"cb2.{uuid4().hex[:6]}"
            endpoint = (
                await api.post(
                    "/api/v1/endpoints",
                    json={"url": receiver.url_for("cb2"), "event_types": [event_type]},
                )
            ).json()
            receiver.register("cb2", endpoint["signing_secret"], [503])

            for i in range(6):
                await api.post(
                    "/api/v1/events", json={"event_type": event_type, "payload": {"i": i}}
                )
            await cb_worker.run_once()

            pending = (await api.get("/api/v1/deliveries?status=pending&limit=100")).json()
            skipped = [
                row
                for row in pending
                if row["last_error"] == "skipped: circuit open for this endpoint"
            ]
            assert skipped
            assert {row["attempt_count"] for row in skipped} == {0}
        finally:
            await http.aclose()
