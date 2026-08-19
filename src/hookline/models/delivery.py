from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from hookline.db.base import Base
from hookline.enums import DeliveryStatus

# Stored as VARCHAR, not a Postgres ENUM type. Postgres enums need ALTER TYPE to gain a
# value and can never lose one, and Alembic's autogenerate does not detect changes to
# their members at all - a new status would be silently missing from the migration.
#
# create_constraint=False is deliberate. Letting Enum build the CHECK attaches it to the
# *type* rather than to the table's metadata, which autogenerate cannot see: it finds the
# constraint in the database, finds nothing matching in the model, and emits a
# drop_constraint on every subsequent migration. The CHECK is declared explicitly in
# __table_args__ below instead, where autogenerate compares it by name and stays quiet.
#
# values_callable makes SQLAlchemy persist "pending" rather than the member name
# "PENDING", which is what it would store by default.
DELIVERY_STATUS = Enum(
    DeliveryStatus,
    name="delivery_status",
    native_enum=False,
    create_constraint=False,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)

_STATUS_VALUES = ", ".join(f"'{member.value}'" for member in DeliveryStatus)


class Delivery(Base):
    """One event's journey to one endpoint. This is the outbox row a worker claims."""

    __tablename__ = "deliveries"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[DeliveryStatus] = mapped_column(
        DELIVERY_STATUS, nullable=False, server_default=DeliveryStatus.PENDING.value
    )
    # Actual HTTP requests made. Incremented when an attempt is recorded, not when the
    # row is claimed, so a worker that crashes before sending anything does not burn
    # part of the budget.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Budget for this row specifically, snapshotted from settings at fan-out. Held per
    # delivery rather than read from config at retry time so that changing the setting
    # cannot retroactively dead-letter deliveries already in flight - and so replay can
    # grant a fresh allowance by raising it, keeping attempt_number monotonic and the
    # (delivery_id, attempt_number) unique constraint intact.
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))

    # When this row next becomes claimable. Set to now() on creation so the first
    # attempt happens immediately; pushed forward by the backoff schedule after a failure.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Built from the enum so the database and DeliveryStatus cannot drift apart.
        # The "ck" convention template does contain %(constraint_name)s, so this short
        # name becomes ck_deliveries_status.
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status"),
        # An event is delivered to a given endpoint exactly once. Makes replay and
        # re-ingest safe at the database level rather than by application convention.
        # Named in full: the "uq" naming convention template has no %(constraint_name)s
        # token, so an explicit name is used verbatim rather than being prefixed.
        UniqueConstraint("event_id", "endpoint_id", name="uq_deliveries_event_id_endpoint_id"),
        # The worker's claim query is
        #   WHERE status = 'pending' AND next_attempt_at <= now()
        #   ORDER BY next_attempt_at
        # so this index is what keeps it from degrading into a sequential scan as the
        # delivered rows pile up.
        Index("ix_deliveries_claim", "status", "next_attempt_at"),
        Index("ix_deliveries_event_id", "event_id"),
        Index("ix_deliveries_endpoint_id", "endpoint_id"),
    )
