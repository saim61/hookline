"""Domain vocabulary shared across layers.

These are not storage details, so they do not live in `models/`. Keeping them here
lets `schemas/` describe a status without importing the ORM, which would break the
rule that the wire format knows nothing about how rows are stored.
"""

from enum import StrEnum


class DeliveryStatus(StrEnum):
    """Lifecycle of one event heading to one endpoint.

    pending    -> waiting for a worker to claim it at/after next_attempt_at
    in_flight  -> claimed by a worker, HTTP request in progress
    delivered  -> the endpoint returned 2xx, terminal
    failed     -> the attempt failed but retries remain, back to pending after backoff
    dead       -> max attempts exhausted, terminal, sits in the DLQ until replayed
    """

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"

    @property
    def is_terminal(self) -> bool:
        return self in (DeliveryStatus.DELIVERED, DeliveryStatus.DEAD)
