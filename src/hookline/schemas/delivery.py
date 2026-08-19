from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from hookline.enums import DeliveryStatus


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    endpoint_id: UUID
    status: DeliveryStatus
    attempt_count: int
    # Exposed alongside attempt_count so a caller can render "3 of 5" without having to
    # know the server's configuration - and because replay changes it per delivery.
    max_attempts: int
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class DeliveryAttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    delivery_id: UUID
    attempt_number: int
    status_code: int | None
    response_body: str | None
    error: str | None
    duration_ms: int
    created_at: datetime
