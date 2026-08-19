from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from hookline.api.deps import DeliveryRepoDep, RateLimited, SettingsDep
from hookline.auth.dependencies import requires
from hookline.auth.scopes import Scope
from hookline.enums import DeliveryStatus
from hookline.schemas.delivery import DeliveryAttemptRead, DeliveryRead

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


@router.get(
    "",
    response_model=list[DeliveryRead],
    dependencies=[requires(Scope.DELIVERIES_READ)],
)
async def list_deliveries(
    repo: DeliveryRepoDep,
    delivery_status: Annotated[
        DeliveryStatus,
        Query(alias="status", description="Use `dead` to read the dead letter queue."),
    ] = DeliveryStatus.DEAD,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DeliveryRead]:
    """Deliveries by status, most recently touched first.

    Defaults to `dead`, which is the dead letter queue: everything that exhausted its
    attempt budget or hit a non-retryable response. This is the list a human works
    through after an outage.
    """
    rows = await repo.list_by_status(delivery_status, limit=limit, offset=offset)
    return [DeliveryRead.model_validate(d) for d in rows]


@router.get(
    "/{delivery_id}",
    response_model=DeliveryRead,
    dependencies=[requires(Scope.DELIVERIES_READ)],
)
async def get_delivery(delivery_id: UUID, repo: DeliveryRepoDep) -> DeliveryRead:
    delivery = await repo.get(delivery_id)
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
    return DeliveryRead.model_validate(delivery)


@router.get(
    "/{delivery_id}/attempts",
    response_model=list[DeliveryAttemptRead],
    dependencies=[requires(Scope.DELIVERIES_READ)],
)
async def list_delivery_attempts(
    delivery_id: UUID, repo: DeliveryRepoDep
) -> list[DeliveryAttemptRead]:
    """Every HTTP request made for this delivery, oldest first.

    This is the "I never got the webhook" endpoint: what was sent, when, how long it
    took, and exactly what the receiving server returned each time - including the
    attempts that failed.
    """
    if await repo.get(delivery_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
    attempts = await repo.list_attempts(delivery_id)
    return [DeliveryAttemptRead.model_validate(a) for a in attempts]


@router.post(
    "/{delivery_id}/replay",
    response_model=DeliveryRead,
    dependencies=[requires(Scope.DELIVERIES_WRITE), RateLimited],
)
async def replay_delivery(
    delivery_id: UUID, repo: DeliveryRepoDep, settings: SettingsDep
) -> DeliveryRead:
    """Requeue a dead delivery with a fresh attempt budget.

    Only `dead` deliveries can be replayed. Requeuing a pending one would hand it to a
    second worker, and requeuing a delivered one would send the customer a duplicate -
    so both are refused with 409 rather than silently doing nothing.

    Earlier attempts stay in the log. The replayed attempts are numbered after them, so
    the record of why it died sits next to what happened when it was retried.
    """
    delivery = await repo.replay(delivery_id, extra_attempts=settings.max_delivery_attempts)
    if delivery is not None:
        return DeliveryRead.model_validate(delivery)

    # The conditional UPDATE matched nothing: either no such row, or it was not dead.
    existing = await repo.get(delivery_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail=f"only dead deliveries can be replayed, this one is {existing.status}",
    )
