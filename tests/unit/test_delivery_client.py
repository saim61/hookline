"""What counts as retryable, and the exact bytes that get signed."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from hookline.delivery import signing
from hookline.delivery.client import (
    RETRYABLE_CLIENT_ERRORS,
    DeliveryClient,
    build_body,
)
from hookline.models.delivery_attempt import MAX_STORED_RESPONSE_BYTES

CREATED_AT = datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)


def make_client(handler: httpx.MockTransport) -> DeliveryClient:
    return DeliveryClient(httpx.AsyncClient(transport=handler), user_agent="hookline/test")


class TestBuildBody:
    def test_envelope_shape(self) -> None:
        event_id = uuid4()
        body = json.loads(build_body(event_id, "order.created", CREATED_AT, {"n": 1}))
        assert body == {
            "id": str(event_id),
            "type": "order.created",
            "created_at": CREATED_AT.isoformat(),
            "data": {"n": 1},
        }

    def test_serialisation_is_stable(self) -> None:
        """The same input must produce the same bytes.

        The signature is computed over these bytes and verified against them. If key order
        or separator spacing varied between calls, signatures would fail intermittently -
        the worst possible failure mode, because it looks like a network problem.
        """
        args = (uuid4(), "a.b", CREATED_AT, {"z": 1, "a": 2, "m": {"nested": True}})
        assert build_body(*args) == build_body(*args)

    def test_no_incidental_whitespace(self) -> None:
        body = build_body(uuid4(), "a.b", CREATED_AT, {"a": 1, "b": 2})
        assert b", " not in body
        assert b": " not in body


class TestOutcomes:
    @pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
    async def test_2xx_is_success(self, status: int) -> None:
        client = make_client(httpx.MockTransport(lambda _: httpx.Response(status, text="ok")))
        outcome = await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )
        assert outcome.succeeded is True
        assert outcome.retryable is False
        assert outcome.status_code == status
        assert outcome.error is None
        assert outcome.response_body == "ok"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_5xx_is_retryable(self, status: int) -> None:
        client = make_client(httpx.MockTransport(lambda _: httpx.Response(status, text="boom")))
        outcome = await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )
        assert outcome.succeeded is False
        assert outcome.retryable is True
        assert outcome.error == f"endpoint returned {status}"

    @pytest.mark.parametrize("status", sorted(RETRYABLE_CLIENT_ERRORS))
    async def test_the_three_retryable_4xx(self, status: int) -> None:
        """408, 425 and 429 say "not now", which is different from "not ever"."""
        client = make_client(httpx.MockTransport(lambda _: httpx.Response(status)))
        outcome = await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )
        assert outcome.retryable is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422])
    async def test_other_4xx_is_terminal(self, status: int) -> None:
        """Sending identical bytes again cannot change the answer.

        Retrying five times only delays the dead letter by an hour while burning the
        receiver's error budget.
        """
        client = make_client(httpx.MockTransport(lambda _: httpx.Response(status)))
        outcome = await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )
        assert outcome.succeeded is False
        assert outcome.retryable is False

    async def test_transport_failure_has_no_status_code(self) -> None:
        """DNS failure, refused connection, timeout - no response exists.

        status_code stays NULL and the reason goes in `error`, which is what lets the
        attempt log distinguish "they said no" from "we never reached them".
        """

        def boom(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow")

        client = make_client(httpx.MockTransport(boom))
        outcome = await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )
        assert outcome.succeeded is False
        assert outcome.retryable is True
        assert outcome.status_code is None
        assert outcome.response_body is None
        assert "ConnectTimeout" in str(outcome.error)

    async def test_client_never_raises(self) -> None:
        """The worker's control flow depends on getting an outcome, not an exception."""

        def boom(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("reset")

        client = make_client(httpx.MockTransport(boom))
        assert await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )

    async def test_huge_response_body_is_truncated(self) -> None:
        """One endpoint returning a 40MB HTML error page must not bloat the table."""
        client = make_client(httpx.MockTransport(lambda _: httpx.Response(500, text="x" * 100_000)))
        outcome = await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )
        assert outcome.response_body is not None
        assert len(outcome.response_body) <= MAX_STORED_RESPONSE_BYTES + 32
        assert outcome.response_body.endswith("[truncated]")


class TestSignedRequest:
    async def test_headers_verify_against_the_body_sent(self) -> None:
        seen: dict[str, object] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            seen["content"] = request.content
            return httpx.Response(200)

        delivery_id = uuid4()
        body = build_body(uuid4(), "order.created", CREATED_AT, {"n": 1})
        timestamp = int(CREATED_AT.timestamp())
        client = make_client(httpx.MockTransport(capture))
        await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_shhh",
            delivery_id=delivery_id,
            body=body,
            timestamp=timestamp,
        )

        headers = seen["headers"]
        assert isinstance(headers, dict)
        assert seen["content"] == body
        # webhook-id is the delivery id, not the event id: unique per destination and
        # stable across retries, so a receiver deduplicating on it gets exactly-once
        # processing.
        assert headers["webhook-id"] == str(delivery_id)
        assert signing.verify(
            "whsec_shhh",
            str(delivery_id),
            timestamp,
            body,
            str(headers["webhook-signature"]),
            now=timestamp,
        )

    async def test_user_agent_identifies_us(self) -> None:
        seen: dict[str, str] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200)

        client = make_client(httpx.MockTransport(capture))
        await client.deliver(
            url="https://x.example.com/h",
            signing_secret="whsec_a",
            delivery_id=uuid4(),
            body=b"{}",
        )
        assert seen["user-agent"] == "hookline/test"
        assert seen["content-type"] == "application/json"
