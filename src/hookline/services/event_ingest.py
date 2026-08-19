from dataclasses import dataclass
from typing import Any

from hookline.models.event import Event
from hookline.repositories.delivery import DeliveryRepository
from hookline.repositories.endpoint import EndpointRepository
from hookline.repositories.event import EventRepository


@dataclass(frozen=True, slots=True)
class IngestResult:
    event: Event
    deliveries_scheduled: int
    duplicate: bool


class EventIngestService:
    """Accepts an event and schedules it for every subscribed endpoint.

    This spans three tables, so it belongs neither in a route handler (which should
    only translate HTTP) nor in a repository (which should only reach one table and
    knows nothing about business rules). It sits between them.

    It does not commit. The request-scoped session dependency owns that, so the event
    row and every delivery row land in one transaction: a fan-out that fails halfway
    leaves no event behind for a worker to find with half its destinations missing.
    """

    def __init__(
        self,
        events: EventRepository,
        endpoints: EndpointRepository,
        deliveries: DeliveryRepository,
        max_delivery_attempts: int,
    ) -> None:
        self._events = events
        self._endpoints = endpoints
        self._deliveries = deliveries
        self._max_delivery_attempts = max_delivery_attempts

    async def ingest(
        self,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> IngestResult:
        event, created = await self._events.create_idempotent(
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
        )

        if not created:
            # A replay of an earlier request. The original ingest already scheduled its
            # deliveries; scheduling again would double-deliver, which is the entire
            # thing the idempotency key exists to prevent.
            return IngestResult(event=event, deliveries_scheduled=0, duplicate=True)

        subscribers = await self._endpoints.list_subscribed_to(event_type)
        scheduled = await self._deliveries.create_many(
            event_id=event.id,
            endpoint_ids=[endpoint.id for endpoint in subscribers],
            # Snapshotted now so a later config change cannot retroactively shorten the
            # budget of deliveries already queued.
            max_attempts=self._max_delivery_attempts,
        )
        return IngestResult(
            event=event,
            deliveries_scheduled=len(scheduled),
            duplicate=False,
        )
