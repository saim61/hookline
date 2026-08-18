from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.models.event import Event


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_idempotent(
        self,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[Event, bool]:
        """Insert an event, or return the existing one if the key was already used.

        Returns (event, created). `created` is False when an Idempotency-Key replayed
        an earlier request, which tells the caller not to schedule deliveries again.

        Uses INSERT ... ON CONFLICT DO NOTHING rather than a SELECT-then-INSERT, which
        would race: two concurrent requests with the same key would both see nothing and
        both insert. Letting Postgres arbitrate means the loser gets zero rows back and
        reads the winner's row. Catching IntegrityError instead would work but poisons
        the surrounding transaction, forcing a SAVEPOINT to recover.
        """
        stmt = (
            pg_insert(Event)
            .values(
                id=uuid4(),
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=[Event.idempotency_key])
            .returning(Event.id)
        )
        inserted_id = (await self._session.execute(stmt)).scalar_one_or_none()

        if inserted_id is None:
            # The key already existed. It cannot have been NULL to get here: Postgres
            # treats NULLs as distinct, so unkeyed inserts never conflict.
            assert idempotency_key is not None
            existing = await self.get_by_idempotency_key(idempotency_key)
            assert existing is not None
            return existing, False

        event = await self._session.get_one(Event, inserted_id)
        return event, True

    async def get(self, event_id: UUID) -> Event | None:
        return await self._session.get(Event, event_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> Event | None:
        result = await self._session.execute(
            select(Event).where(Event.idempotency_key == idempotency_key)
        )
        return result.scalar_one_or_none()

    async def list_recent(self, limit: int = 50, offset: int = 0) -> list[Event]:
        result = await self._session.execute(
            select(Event).order_by(Event.created_at.desc(), Event.id).limit(limit).offset(offset)
        )
        return list(result.scalars().all())
