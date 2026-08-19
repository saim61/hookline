"""Cache for the subscriber lookup that every ingested event performs.

`SELECT ... WHERE event_types @> ARRAY['order.created'] AND is_active` runs once per
ingested event, which makes it the highest-frequency query in the system. It is also
almost perfectly cacheable: endpoint registrations change rarely, events arrive
constantly.

Invalidation is explicit on every endpoint mutation, with a short TTL as the backstop.
Relying on TTL alone would mean a newly registered endpoint silently misses events for
up to the TTL - which looks exactly like a bug to whoever just registered it.
"""

from uuid import UUID

from redis.asyncio import Redis

from hookline.observability.logging import get_logger

log = get_logger("hookline.cache")

_MISS = object()


class SubscriberCache:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    def _key(self, event_type: str) -> str:
        return f"hookline:subs:{event_type}"

    async def get(self, event_type: str) -> list[UUID] | None:
        """Cached endpoint ids, or None on a miss or any Redis problem.

        An empty subscriber list is cached as a real value, not treated as a miss.
        Unsubscribed event types are common - a caller emitting twenty types to
        endpoints that want two - and treating "nobody is listening" as a miss would
        send exactly those events to the database every single time.
        """
        try:
            raw = await self._redis.get(self._key(event_type))
        except Exception:
            log.debug("subscriber cache read failed", exc_info=True)
            return None
        if raw is None:
            return None
        # decode_responses=True is set on the pool, so this is already str at runtime -
        # but that is a constructor argument the type system cannot see, so redis-py
        # types every read as `bytes | str`. Normalising here beats a cast, which would
        # silently be wrong if the pool setting ever changed.
        text = raw.decode() if isinstance(raw, bytes) else raw
        if text == "":
            return []
        return [UUID(part) for part in text.split(",")]

    async def set(self, event_type: str, endpoint_ids: list[UUID]) -> None:
        try:
            await self._redis.set(
                self._key(event_type),
                ",".join(str(i) for i in endpoint_ids),
                ex=self._ttl,
            )
        except Exception:
            log.debug("subscriber cache write failed", exc_info=True)

    async def invalidate(self, event_types: list[str]) -> None:
        """Drop the entries an endpoint change could have affected.

        Only the types the endpoint actually subscribes to, not the whole namespace: a
        FLUSH-style invalidation on every registration would make the cache useless for
        anyone with churn in their endpoint list.
        """
        if not event_types:
            return
        try:
            await self._redis.delete(*[self._key(t) for t in event_types])
        except Exception:
            log.warning("subscriber cache invalidation failed", exc_info=True)
