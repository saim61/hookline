from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from hookline.db.base import Base


class Event(Base):
    """Something that happened, as reported by the API caller.

    Ingested once and never mutated. Fan-out to destinations lives in `deliveries`,
    so the payload is stored a single time no matter how many endpoints receive it.
    """

    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Supplied by the caller via the Idempotency-Key header. NULL means "no key given",
    # and Postgres treats NULLs as distinct, so unkeyed events never collide.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_events_event_type_created_at", "event_type", "created_at"),)
