from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from hookline.api.deps import RepoDep
from hookline.schemas.endpoint import EndpointCreate, EndpointCreated, EndpointRead

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


@router.post("", response_model=EndpointCreated, status_code=status.HTTP_201_CREATED)
async def create_endpoint(payload: EndpointCreate, repo: RepoDep) -> EndpointCreated:
    endpoint = await repo.create(
        url=str(payload.url),
        description=payload.description,
        event_types=payload.event_types,
    )
    return EndpointCreated.model_validate(endpoint)


@router.get("", response_model=list[EndpointRead])
async def list_endpoints(repo: RepoDep) -> list[EndpointRead]:
    return [EndpointRead.model_validate(e) for e in await repo.list_all()]


@router.get("/{endpoint_id}", response_model=EndpointRead)
async def get_endpoint(endpoint_id: UUID, repo: RepoDep) -> EndpointRead:
    endpoint = await repo.get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="endpoint not found")
    return EndpointRead.model_validate(endpoint)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(endpoint_id: UUID, repo: RepoDep) -> None:
    if not await repo.delete(endpoint_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="endpoint not found")
