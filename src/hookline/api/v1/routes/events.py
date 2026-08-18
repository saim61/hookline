from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response, status

from hookline.api.deps import DeliveryRepoDep, EventIngestDep, EventRepoDep
from hookline.schemas.delivery import DeliveryRead
from hookline.schemas.event import EventAccepted, EventCreate, EventRead

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(
    payload: EventCreate,
    service: EventIngestDep,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=255,
            description="Replaying a request with the same key returns the original event "
            "instead of ingesting a second copy.",
        ),
    ] = None,
) -> EventAccepted:
    """Accept an event for delivery.

    Returns 202, not 200: the event has been persisted and scheduled, but nothing has
    been delivered yet. That happens out of band, which is what keeps this endpoint fast
    regardless of how slow the receiving servers are.
    """
    result = await service.ingest(
        event_type=payload.event_type,
        payload=payload.payload,
        idempotency_key=idempotency_key,
    )
    # Lets a caller distinguish "we accepted your event" from "you already sent this"
    # without having to diff the body against what they sent.
    response.headers["Idempotent-Replay"] = "true" if result.duplicate else "false"

    return EventAccepted(
        id=result.event.id,
        event_type=result.event.event_type,
        created_at=result.event.created_at,
        deliveries_scheduled=result.deliveries_scheduled,
        duplicate=result.duplicate,
    )


@router.get("", response_model=list[EventRead])
async def list_events(
    repo: EventRepoDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventRead]:
    events = await repo.list_recent(limit=limit, offset=offset)
    return [EventRead.model_validate(e) for e in events]


@router.get("/{event_id}", response_model=EventRead)
async def get_event(event_id: UUID, repo: EventRepoDep) -> EventRead:
    event = await repo.get(event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="event not found")
    return EventRead.model_validate(event)


@router.get("/{event_id}/deliveries", response_model=list[DeliveryRead])
async def list_event_deliveries(
    event_id: UUID,
    events: EventRepoDep,
    deliveries: DeliveryRepoDep,
) -> list[DeliveryRead]:
    """Per-destination status for one event.

    An empty list is meaningful: the event was accepted but no active endpoint is
    subscribed to its type.
    """
    if await events.get(event_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="event not found")
    rows = await deliveries.list_for_event(event_id)
    return [DeliveryRead.model_validate(d) for d in rows]
