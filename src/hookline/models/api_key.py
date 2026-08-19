from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from hookline.db.base import Base


class ApiKey(Base):
    """A credential for calling the Hookline API.

    The key itself is never stored - only its SHA-256 - so this table is safe to dump.
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # First few characters, in the clear, so a human can tell which key a log line or
    # dashboard row is about without the key being recoverable from it.
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)

    # Unique so authentication is a single indexed lookup by the hash of whatever was
    # presented, rather than a scan comparing against every row.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )

    # Optional per-key secret for verifying signatures on inbound requests. Lets a caller
    # prove it holds the secret without putting the secret on the wire.
    inbound_signing_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # NULL means no expiry. A key with a deadline is strictly better than one without,
    # but forcing an expiry on every key guarantees an outage nobody diarised.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Written at most once a minute per key, throttled through Redis - updating it on
    # every request would add a write to the hot path purely for reporting.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def signed_requests_required(self) -> bool:
        """Derived, not stored. Whether the key has a secret is the whole condition, and a
        separate boolean column could disagree with it."""
        return self.inbound_signing_secret is not None

    def is_usable(self, now: datetime) -> bool:
        if not self.is_active:
            return False
        return self.expires_at is None or self.expires_at > now
