from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from hookline.db.base import Base

# Responses are stored to answer "what did their server actually say", not to be replayed.
# Truncating keeps one endpoint that returns a 40MB HTML error page from bloating the table.
MAX_STORED_RESPONSE_BYTES = 4096


class DeliveryAttempt(Base):
    """One HTTP request to a customer endpoint. Append-only, never updated.

    This table is the answer to "I never got the webhook" - it records what was sent,
    when, and exactly what came back, including for attempts that failed.
    """

    __tablename__ = "delivery_attempts"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # NULL when no response arrived at all - DNS failure, connection refused, timeout.
    # In that case `error` carries the reason instead.
    status_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # A retry that races with itself must not be able to log attempt 3 twice.
        UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_delivery_attempts_delivery_id_attempt_number",
        ),
        Index("ix_delivery_attempts_delivery_id", "delivery_id"),
    )
