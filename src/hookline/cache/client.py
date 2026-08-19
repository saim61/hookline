"""Redis connection, shared by the API and the worker.

Everything Hookline keeps in Redis is derived state - rate limit buckets, circuit
breaker counters, cached subscriber lists. None of it is a source of truth, so every
caller is written to work when Redis is unreachable. Losing Redis costs a cache miss,
a reset breaker, and an unenforced rate limit; it never costs a delivery.
"""

from redis.asyncio import ConnectionPool, Redis

from hookline.config import get_settings
from hookline.observability.logging import get_logger

log = get_logger("hookline.cache")

_pool: ConnectionPool | None = None


def get_redis() -> Redis:
    """A client over a lazily created pool.

    Same shape as db.session.get_engine: one pool per process, built on first use so
    importing the module does not open sockets, which would break test collection and
    `alembic` invocations that never touch Redis.
    """
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            # Fail fast. These calls sit in the request path, so waiting five seconds on
            # a dead Redis is far worse than skipping the rate limit check.
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=30,
        )
    return Redis(connection_pool=_pool)


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None
