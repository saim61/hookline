import secrets
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.models.endpoint import Endpoint


class EndpointRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, url: str, description: str | None, event_types: list[str]) -> Endpoint:
        endpoint = Endpoint(
            url=url,
            description=description,
            event_types=event_types,
            signing_secret=f"whsec_{secrets.token_urlsafe(32)}",
        )
        self._session.add(endpoint)
        await self._session.flush()
        await self._session.refresh(endpoint)
        return endpoint

    async def list_all(self) -> list[Endpoint]:
        result = await self._session.execute(select(Endpoint).order_by(Endpoint.created_at.desc()))
        return list(result.scalars().all())

    async def get(self, endpoint_id: UUID) -> Endpoint | None:
        return await self._session.get(Endpoint, endpoint_id)

    async def delete(self, endpoint_id: UUID) -> bool:
        result = await self._session.execute(
            delete(Endpoint).where(Endpoint.id == endpoint_id).returning(Endpoint.id)
        )
        return result.scalar_one_or_none() is not None
