import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Webhook payloads are metadata about something that happened, not file transfer.
# Rejecting oversized bodies at the edge keeps one caller from filling the outbox
# with megabytes the delivery worker then has to push over the wire five times.
MAX_PAYLOAD_BYTES = 256 * 1024

# Dotted lowercase segments: "order.created", "invoice.payment.failed".
EVENT_TYPE_PATTERN = r"^[a-z0-9_]+(\.[a-z0-9_]+)*$"


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=100, pattern=EVENT_TYPE_PATTERN)
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def _size_limit(cls, v: dict[str, Any]) -> dict[str, Any]:
        size = len(json.dumps(v).encode())
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload is {size} bytes, limit is {MAX_PAYLOAD_BYTES}")
        return v


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str | None
    created_at: datetime


class EventAccepted(BaseModel):
    """Returned with 202. Nothing has been delivered yet at this point."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    created_at: datetime

    # How many endpoints matched this event_type. Zero is a valid, successful ingest -
    # the event is stored, nobody is subscribed. Surfacing it turns a silent no-op into
    # something the caller can notice and alert on.
    deliveries_scheduled: int

    # True when an Idempotency-Key replayed an earlier request. The body describes the
    # original event, and no new deliveries were scheduled.
    duplicate: bool
