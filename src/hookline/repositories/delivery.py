from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.models.delivery import Delivery
from hookline.models.delivery_attempt import DeliveryAttempt


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_many(self, event_id: UUID, endpoint_ids: Sequence[UUID]) -> list[Delivery]:
        """Fan one event out to a set of endpoints. Returns the rows actually created.

        ON CONFLICT DO NOTHING makes this safe to call twice for the same event: the
        (event_id, endpoint_id) unique constraint absorbs the repeat and the second call
        returns an empty list rather than duplicating deliveries.
        """
        if not endpoint_ids:
            return []

        stmt = (
            pg_insert(Delivery)
            .values(
                [
                    {"id": uuid4(), "event_id": event_id, "endpoint_id": endpoint_id}
                    for endpoint_id in endpoint_ids
                ]
            )
            .on_conflict_do_nothing(index_elements=[Delivery.event_id, Delivery.endpoint_id])
            .returning(Delivery.id)
        )
        created_ids = list((await self._session.execute(stmt)).scalars().all())
        if not created_ids:
            return []

        result = await self._session.execute(select(Delivery).where(Delivery.id.in_(created_ids)))
        return list(result.scalars().all())

    async def get(self, delivery_id: UUID) -> Delivery | None:
        return await self._session.get(Delivery, delivery_id)

    async def list_for_event(self, event_id: UUID) -> list[Delivery]:
        result = await self._session.execute(
            select(Delivery).where(Delivery.event_id == event_id).order_by(Delivery.created_at)
        )
        return list(result.scalars().all())

    async def list_attempts(self, delivery_id: UUID) -> list[DeliveryAttempt]:
        result = await self._session.execute(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery_id)
            .order_by(DeliveryAttempt.attempt_number)
        )
        return list(result.scalars().all())
