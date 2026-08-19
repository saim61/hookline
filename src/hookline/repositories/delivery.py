from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.enums import DeliveryStatus
from hookline.models.delivery import Delivery
from hookline.models.delivery_attempt import DeliveryAttempt
from hookline.models.endpoint import Endpoint
from hookline.models.event import Event


@dataclass(frozen=True, slots=True)
class DeliveryJob:
    """Everything the worker needs to send one webhook, read in a single query.

    A flat snapshot rather than ORM objects on purpose: the worker holds this across an
    HTTP request that can take ten seconds, long after its database session has been
    closed. Detached ORM instances would raise MissingGreenlet on the first lazy load.
    """

    delivery_id: UUID
    attempt_number: int
    max_attempts: int
    event_id: UUID
    event_type: str
    payload: dict[str, Any]
    event_created_at: datetime
    endpoint_id: UUID
    url: str
    signing_secret: str


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ fan-out

    async def create_many(
        self, event_id: UUID, endpoint_ids: Sequence[UUID], max_attempts: int
    ) -> list[UUID]:
        """Fan one event out to a set of endpoints. Returns the ids actually created.

        INSERT ... SELECT FROM endpoints rather than INSERT ... VALUES, so the set of
        rows is filtered by the database against live, active endpoints. That matters
        because `endpoint_ids` may come from a cache: an id that was deleted since it was
        cached would raise a foreign key violation and turn an ingest into a 500, and one
        that was deactivated would be delivered to anyway. Filtering in the statement
        makes a stale cache an optimisation problem instead of a correctness problem.

        ON CONFLICT DO NOTHING covers the other direction - calling this twice for the
        same event is absorbed by the (event_id, endpoint_id) unique constraint rather
        than duplicating deliveries.
        """
        if not endpoint_ids:
            return []

        source = (
            select(
                func.gen_random_uuid(),
                literal(event_id, type_=PgUUID(as_uuid=True)),
                Endpoint.id,
                literal(max_attempts),
            )
            .where(Endpoint.id.in_(endpoint_ids))
            .where(Endpoint.is_active.is_(True))
        )
        stmt = (
            pg_insert(Delivery)
            .from_select(["id", "event_id", "endpoint_id", "max_attempts"], source)
            .on_conflict_do_nothing(index_elements=[Delivery.event_id, Delivery.endpoint_id])
            .returning(Delivery.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------ worker

    async def claim_batch(self, limit: int) -> list[DeliveryJob]:
        """Atomically take ownership of up to `limit` due deliveries.

        FOR UPDATE SKIP LOCKED is what makes this a work queue rather than a contention
        point. Plain FOR UPDATE would make worker B block until worker A commits, so N
        workers would run at the speed of one. SKIP LOCKED tells Postgres to step over
        rows another transaction already holds and take the next free ones instead, so
        every worker gets a disjoint batch with no coordination and no external lock
        service.

        Claiming and marking in_flight happen in one transaction. Once it commits, the
        rows no longer match `status = 'pending'`, so nobody claims them again even
        though the lock is gone while the slow part - the HTTP request - runs outside
        any transaction. Holding a row lock across a ten-second POST would be the
        classic mistake here.
        """
        locked_ids = (
            (
                await self._session.execute(
                    select(Delivery.id)
                    .where(Delivery.status == DeliveryStatus.PENDING)
                    .where(Delivery.next_attempt_at <= func.now())
                    .order_by(Delivery.next_attempt_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not locked_ids:
            return []

        await self._session.execute(
            update(Delivery)
            .where(Delivery.id.in_(locked_ids))
            .values(status=DeliveryStatus.IN_FLIGHT)
        )

        rows = await self._session.execute(
            select(
                Delivery.id,
                Delivery.attempt_count,
                Delivery.max_attempts,
                Event.id,
                Event.event_type,
                Event.payload,
                Event.created_at,
                Endpoint.id,
                Endpoint.url,
                Endpoint.signing_secret,
            )
            .join(Event, Delivery.event_id == Event.id)
            .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
            .where(Delivery.id.in_(locked_ids))
        )

        return [
            DeliveryJob(
                delivery_id=delivery_id,
                attempt_number=attempt_count + 1,
                max_attempts=max_attempts,
                event_id=event_id,
                event_type=event_type,
                payload=payload,
                event_created_at=event_created_at,
                endpoint_id=endpoint_id,
                url=url,
                signing_secret=signing_secret,
            )
            for (
                delivery_id,
                attempt_count,
                max_attempts,
                event_id,
                event_type,
                payload,
                event_created_at,
                endpoint_id,
                url,
                signing_secret,
            ) in rows
        ]

    async def record_attempt(
        self,
        delivery_id: UUID,
        attempt_number: int,
        status_code: int | None,
        response_body: str | None,
        error: str | None,
        duration_ms: int,
    ) -> None:
        """Append to the audit log and consume one unit of the attempt budget.

        ON CONFLICT DO NOTHING guards the (delivery_id, attempt_number) unique
        constraint: if a reaped delivery is retried while the original worker is somehow
        still alive, the duplicate attempt row is dropped instead of crashing the loop.
        """
        await self._session.execute(
            pg_insert(DeliveryAttempt)
            .values(
                id=uuid4(),
                delivery_id=delivery_id,
                attempt_number=attempt_number,
                status_code=status_code,
                response_body=response_body,
                error=error,
                duration_ms=duration_ms,
            )
            .on_conflict_do_nothing(
                index_elements=[DeliveryAttempt.delivery_id, DeliveryAttempt.attempt_number]
            )
        )
        await self._session.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(attempt_count=Delivery.attempt_count + 1)
        )

    async def mark_delivered(self, delivery_id: UUID) -> None:
        await self._session.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(status=DeliveryStatus.DELIVERED, last_error=None)
        )

    async def mark_dead(self, delivery_id: UUID, error: str | None) -> None:
        await self._session.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(status=DeliveryStatus.DEAD, last_error=error)
        )

    async def schedule_retry(
        self, delivery_id: UUID, delay_seconds: float, error: str | None
    ) -> None:
        await self._session.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status=DeliveryStatus.PENDING,
                last_error=error,
                next_attempt_at=func.now() + timedelta(seconds=delay_seconds),
            )
        )

    async def reap_stale(self, older_than_seconds: float) -> int:
        """Return in_flight rows abandoned by a dead worker to the pending pool.

        Without this, a worker killed between claiming and recording leaves its batch
        in_flight for ever: invisible to every other worker, never retried, never
        dead-lettered. This is the failure mode that turns "at least once" into
        "sometimes never", and it is invisible until it has already lost data.

        The threshold must exceed the HTTP timeout, or rows still being worked on get
        reclaimed and delivered twice.
        """
        result = await self._session.execute(
            update(Delivery)
            .where(Delivery.status == DeliveryStatus.IN_FLIGHT)
            .where(Delivery.updated_at < func.now() - timedelta(seconds=older_than_seconds))
            .values(
                status=DeliveryStatus.PENDING,
                last_error="reclaimed after worker went away mid-delivery",
            )
            .returning(Delivery.id)
        )
        return len(result.scalars().all())

    # ------------------------------------------------------------------ reads

    async def get(self, delivery_id: UUID) -> Delivery | None:
        return await self._session.get(Delivery, delivery_id)

    async def list_for_event(self, event_id: UUID) -> list[Delivery]:
        result = await self._session.execute(
            select(Delivery).where(Delivery.event_id == event_id).order_by(Delivery.created_at)
        )
        return list(result.scalars().all())

    async def list_by_status(
        self, status: DeliveryStatus, limit: int = 50, offset: int = 0
    ) -> list[Delivery]:
        result = await self._session.execute(
            select(Delivery)
            .where(Delivery.status == status)
            .order_by(Delivery.updated_at.desc(), Delivery.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_attempts(self, delivery_id: UUID) -> list[DeliveryAttempt]:
        result = await self._session.execute(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery_id)
            .order_by(DeliveryAttempt.attempt_number)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ replay

    async def replay(self, delivery_id: UUID, extra_attempts: int) -> Delivery | None:
        """Put a dead delivery back in the queue with a fresh attempt budget.

        Raising max_attempts rather than zeroing attempt_count keeps attempt_number
        increasing, so the replayed attempts append to the audit trail instead of
        colliding with the numbers already recorded. The history of why it died stays
        readable next to the attempts that followed.

        Only `dead` rows are replayable: replaying something already pending would give
        it a second worker, and replaying a delivered one would double-deliver.
        """
        result = await self._session.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .where(Delivery.status == DeliveryStatus.DEAD)
            .values(
                status=DeliveryStatus.PENDING,
                max_attempts=Delivery.attempt_count + extra_attempts,
                next_attempt_at=func.now(),
                last_error=None,
            )
            .returning(Delivery)
        )
        return result.scalars().one_or_none()
