import secrets
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.auth.keys import generate_key
from hookline.models.api_key import ApiKey


class ApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        name: str,
        scopes: list[str],
        expires_at: datetime | None = None,
        with_inbound_signing: bool = False,
    ) -> tuple[ApiKey, str]:
        """Create a key. Returns the row and the plaintext token, which exists only here."""
        token, display_prefix, key_hash = generate_key()
        api_key = ApiKey(
            name=name,
            display_prefix=display_prefix,
            key_hash=key_hash,
            scopes=scopes,
            expires_at=expires_at,
            inbound_signing_secret=(
                f"whsec_{secrets.token_urlsafe(32)}" if with_inbound_signing else None
            ),
        )
        self._session.add(api_key)
        await self._session.flush()
        await self._session.refresh(api_key)
        return api_key, token

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Authentication lookup. One indexed hit on the unique key_hash index."""
        result = await self._session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
        return result.scalar_one_or_none()

    async def get(self, key_id: UUID) -> ApiKey | None:
        return await self._session.get(ApiKey, key_id)

    async def list_all(self) -> list[ApiKey]:
        result = await self._session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
        return list(result.scalars().all())

    async def touch(self, key_id: UUID) -> None:
        await self._session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=func.now())
        )

    async def revoke(self, key_id: UUID) -> bool:
        """Deactivate rather than delete.

        A revoked key that still exists is auditable: `last_used_at` and the name survive,
        so "what was this key doing before we killed it" remains answerable. Deleting the
        row throws that away exactly when an incident needs it.
        """
        result = await self._session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .where(ApiKey.is_active.is_(True))
            .values(is_active=False)
            .returning(ApiKey.id)
        )
        return result.scalar_one_or_none() is not None

    async def delete(self, key_id: UUID) -> bool:
        result = await self._session.execute(
            delete(ApiKey).where(ApiKey.id == key_id).returning(ApiKey.id)
        )
        return result.scalar_one_or_none() is not None

    async def count(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(ApiKey))
        return result.scalar_one()
