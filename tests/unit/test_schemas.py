"""Wire-format validation. This is the boundary that keeps bad input out of the database."""

import pytest
from pydantic import ValidationError

from hookline.schemas.api_key import ApiKeyCreate
from hookline.schemas.endpoint import EndpointCreate
from hookline.schemas.event import MAX_PAYLOAD_BYTES, EventCreate


class TestEventCreate:
    @pytest.mark.parametrize(
        "event_type",
        ["order.created", "invoice.payment.failed", "ping", "a_b.c_d", "user.2fa_enabled"],
    )
    def test_accepts_dotted_lowercase(self, event_type: str) -> None:
        assert EventCreate(event_type=event_type, payload={}).event_type == event_type

    @pytest.mark.parametrize(
        "event_type",
        [
            "Order.Created",  # uppercase
            "order created",  # space
            "order..created",  # empty segment
            ".order",  # leading dot
            "order.",  # trailing dot
            "order-created",  # hyphen
            "order/created",  # slash
            "",  # empty
            "x" * 101,  # too long
        ],
    )
    def test_rejects_anything_else(self, event_type: str) -> None:
        with pytest.raises(ValidationError):
            EventCreate(event_type=event_type, payload={})

    def test_payload_must_be_an_object(self) -> None:
        """A top-level array or scalar would break the envelope's `data` field."""
        for bad in ([1, 2], "string", 42, None):
            with pytest.raises(ValidationError):
                EventCreate(event_type="a.b", payload=bad)  # type: ignore[arg-type]

    def test_empty_payload_is_fine(self) -> None:
        """Plenty of real events carry no data - `ping`, `subscription.cancelled`."""
        assert EventCreate(event_type="a.b", payload={}).payload == {}

    def test_oversized_payload_is_rejected(self) -> None:
        """Rejected at the edge, not after it is in the outbox.

        A webhook payload is metadata about something that happened, not file transfer.
        Accepting megabytes means the worker then pushes them over the wire up to five
        times each.
        """
        with pytest.raises(ValidationError, match="limit is"):
            EventCreate(event_type="a.b", payload={"blob": "x" * (MAX_PAYLOAD_BYTES + 100)})

    def test_payload_just_under_the_limit_is_accepted(self) -> None:
        payload = {"blob": "x" * (MAX_PAYLOAD_BYTES - 200)}
        assert EventCreate(event_type="a.b", payload=payload)

    def test_nested_payloads_are_allowed(self) -> None:
        payload = {"order": {"lines": [{"sku": "A", "qty": 2}], "total": 4500}}
        assert EventCreate(event_type="a.b", payload=payload).payload == payload


class TestEndpointCreate:
    def test_requires_a_valid_url(self) -> None:
        for bad in ("not-a-url", "ftp:/nope", "", "example.com"):
            with pytest.raises(ValidationError):
                EndpointCreate(url=bad)  # type: ignore[arg-type]

    def test_accepts_http_and_https(self) -> None:
        assert EndpointCreate(url="https://a.example.com/hooks")
        assert EndpointCreate(url="http://localhost:9000/hooks")

    def test_event_types_must_be_unique(self) -> None:
        """A duplicate would create two identical subscriptions and confuse fan-out counts."""
        with pytest.raises(ValidationError, match="unique"):
            EndpointCreate(url="https://a.example.com/h", event_types=["a.b", "a.b"])

    def test_event_types_default_to_empty(self) -> None:
        """Subscribed to nothing is the safe default for a fresh registration."""
        assert EndpointCreate(url="https://a.example.com/h").event_types == []

    def test_description_is_length_limited(self) -> None:
        with pytest.raises(ValidationError):
            EndpointCreate(url="https://a.example.com/h", description="x" * 201)


class TestApiKeyCreate:
    def test_requires_at_least_one_scope(self) -> None:
        """A key with no scopes can do nothing, so creating one is always a mistake."""
        with pytest.raises(ValidationError):
            ApiKeyCreate(name="x", scopes=[])

    def test_rejects_unknown_scopes_and_says_which(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ApiKeyCreate(name="x", scopes=["events:write", "events:destroy"])
        message = str(exc.value)
        assert "events:destroy" in message
        # The error lists what is valid, so the caller does not have to go looking.
        assert "events:write" in message

    def test_rejects_duplicate_scopes(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            ApiKeyCreate(name="x", scopes=["events:write", "events:write"])

    def test_signed_requests_defaults_off(self) -> None:
        assert ApiKeyCreate(name="x", scopes=["admin"]).require_signed_requests is False
