from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from hookline.api.deps import RateLimited, RepoDep, SubscriberCacheDep
from hookline.auth.dependencies import requires
from hookline.auth.scopes import Scope
from hookline.schemas.endpoint import EndpointCreate, EndpointCreated, EndpointRead

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


@router.post(
    "",
    response_model=EndpointCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[requires(Scope.ENDPOINTS_WRITE), RateLimited],
)
async def create_endpoint(
    payload: EndpointCreate, repo: RepoDep, cache: SubscriberCacheDep
) -> EndpointCreated:
    endpoint = await repo.create(
        url=str(payload.url),
        description=payload.description,
        event_types=payload.event_types,
    )
    # A newly registered endpoint must start receiving events immediately. Leaving this
    # to the cache TTL would mean silently missing everything for up to that long, which
    # to whoever just registered looks exactly like the service being broken.
    await cache.invalidate(payload.event_types)
    return EndpointCreated.model_validate(endpoint)


@router.get(
    "",
    response_model=list[EndpointRead],
    dependencies=[requires(Scope.ENDPOINTS_READ)],
)
async def list_endpoints(repo: RepoDep) -> list[EndpointRead]:
    return [EndpointRead.model_validate(e) for e in await repo.list_all()]


@router.get(
    "/{endpoint_id}",
    response_model=EndpointRead,
    dependencies=[requires(Scope.ENDPOINTS_READ)],
)
async def get_endpoint(endpoint_id: UUID, repo: RepoDep) -> EndpointRead:
    endpoint = await repo.get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="endpoint not found")
    return EndpointRead.model_validate(endpoint)


@router.delete(
    "/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[requires(Scope.ENDPOINTS_WRITE)],
)
async def delete_endpoint(endpoint_id: UUID, repo: RepoDep, cache: SubscriberCacheDep) -> None:
    event_types = await repo.delete(endpoint_id)
    if event_types is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="endpoint not found")
    await cache.invalidate(event_types)
