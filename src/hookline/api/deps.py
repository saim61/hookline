from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.config import Settings, get_settings
from hookline.db.session import get_session
from hookline.repositories.delivery import DeliveryRepository
from hookline.repositories.endpoint import EndpointRepository
from hookline.repositories.event import EventRepository
from hookline.services.event_ingest import EventIngestService

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_endpoint_repo(session: SessionDep) -> EndpointRepository:
    return EndpointRepository(session)


async def get_event_repo(session: SessionDep) -> EventRepository:
    return EventRepository(session)


async def get_delivery_repo(session: SessionDep) -> DeliveryRepository:
    return DeliveryRepository(session)


EndpointRepoDep = Annotated[EndpointRepository, Depends(get_endpoint_repo)]
EventRepoDep = Annotated[EventRepository, Depends(get_event_repo)]
DeliveryRepoDep = Annotated[DeliveryRepository, Depends(get_delivery_repo)]

# Alias kept so the endpoints routes still read as `repo: RepoDep`. Now that a second
# repository exists, new code should use the explicit name.
RepoDep = EndpointRepoDep


async def get_event_ingest_service(
    events: EventRepoDep,
    endpoints: EndpointRepoDep,
    deliveries: DeliveryRepoDep,
) -> EventIngestService:
    return EventIngestService(events=events, endpoints=endpoints, deliveries=deliveries)


EventIngestDep = Annotated[EventIngestService, Depends(get_event_ingest_service)]
