import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(slots=True)
class Endpoint:
    id: UUID
    url: str
    description: str | None
    event_types: list[str]
    signing_secret: str
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EndpointStore:
    """In-memory store. will be replaced with a database in the future."""

    def __init__(self) -> None:
        self._items: dict[UUID, Endpoint] = {}

    async def create(
        self, url: str, description: str | None, event_types: list[str]
    ) -> Endpoint:
        endpoint = Endpoint(
            id=uuid4(),
            url=url,
            description=description,
            event_types=event_types,
            signing_secret=f"whsec_{secrets.token_urlsafe(32)}",
        )
        self._items[endpoint.id] = endpoint
        return endpoint

    async def list_all(self) -> list[Endpoint]:
        return list(self._items.values())

    async def get(self, endpoint_id: UUID) -> Endpoint | None:
        return self._items.get(endpoint_id)

    async def delete(self, endpoint_id: UUID) -> bool:
        return self._items.pop(endpoint_id, None) is not None


_store = EndpointStore()


def get_endpoint_store() -> EndpointStore:
    return _store