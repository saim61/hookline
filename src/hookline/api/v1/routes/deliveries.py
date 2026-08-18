from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from hookline.api.deps import DeliveryRepoDep
from hookline.schemas.delivery import DeliveryAttemptRead, DeliveryRead

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


@router.get("/{delivery_id}", response_model=DeliveryRead)
async def get_delivery(delivery_id: UUID, repo: DeliveryRepoDep) -> DeliveryRead:
    delivery = await repo.get(delivery_id)
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
    return DeliveryRead.model_validate(delivery)


@router.get("/{delivery_id}/attempts", response_model=list[DeliveryAttemptRead])
async def list_delivery_attempts(
    delivery_id: UUID, repo: DeliveryRepoDep
) -> list[DeliveryAttemptRead]:
    """Every HTTP request made for this delivery, oldest first.

    This is the "I never got the webhook" endpoint: it shows what was sent, when, and
    what the receiving server returned each time.

    Empty until Phase 4 puts a worker behind it.
    """
    if await repo.get(delivery_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
    attempts = await repo.list_attempts(delivery_id)
    return [DeliveryAttemptRead.model_validate(a) for a in attempts]
