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
