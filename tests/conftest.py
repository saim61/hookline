"""Test configuration and fixtures.

Environment is set up **before** any `hookline` module is imported, because
`get_settings()` is `@lru_cache`d - the first call wins for the whole process, and by the
time a fixture runs it is too late to change the database URL.

Two ways to get infrastructure, chosen by `HOOKLINE_TEST_CONTAINERS`:

    unset (default)  reuse the compose services, on a dedicated database and Redis db
                     index. Fast, which is what makes the suite worth running on every
                     save.
    1                start throwaway Postgres and Redis containers with testcontainers.
                     Slower to start but needs nothing running, which is what CI wants.

Either way the tests never touch the development database. A suite that truncates the
table you were about to demo from is a suite people stop running.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------- infrastructure

_containers: list[object] = []


def _start_containers() -> tuple[str, str]:
    # testcontainers.community.*, not the top-level shims - those emit a
    # DeprecationWarning, which the suite's `filterwarnings = error` turns into a
    # collection failure.
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.redis import RedisContainer

    pg = PostgresContainer("postgres:17-alpine", driver="asyncpg")
    pg.start()
    _containers.append(pg)

    redis = RedisContainer("redis:8-alpine")
    redis.start()
    _containers.append(redis)

    redis_url = f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"
    return pg.get_connection_url(), redis_url


def _resolve_infrastructure() -> tuple[str, str]:
    if os.environ.get("HOOKLINE_TEST_CONTAINERS") == "1":
        return _start_containers()

    # A separate database on the dev Postgres, and Redis db 15 rather than 0, so a test
    # run cannot flush the cache the dev server is using.
    database_url = os.environ.get(
        "HOOKLINE_TEST_DATABASE_URL",
        "postgresql+asyncpg://hookline:hookline@localhost:5432/hookline_test",
    )
    redis_url = os.environ.get("HOOKLINE_TEST_REDIS_URL", "redis://localhost:6379/15")
    return database_url, redis_url


DATABASE_URL, REDIS_URL = _resolve_infrastructure()

os.environ.update(
    HOOKLINE_DATABASE_URL=DATABASE_URL,
    HOOKLINE_REDIS_URL=REDIS_URL,
    HOOKLINE_DEBUG="false",
    HOOKLINE_AUTH_ENABLED="true",
    # Generous enough that ordinary tests never trip it. The rate limit tests build their
    # own limiter with tight numbers instead of relying on the global setting.
    HOOKLINE_RATE_LIMIT_CAPACITY="100000",
    HOOKLINE_RATE_LIMIT_REFILL_PER_SECOND="100000",
    HOOKLINE_DELIVERY_RATE_LIMIT_ENABLED="false",
    # Retries that would take an hour in production need to take milliseconds here.
    HOOKLINE_RETRY_BASE_DELAY_SECONDS="0.02",
    HOOKLINE_RETRY_MAX_DELAY_SECONDS="0.05",
    HOOKLINE_DELIVERY_TIMEOUT_SECONDS="1.5",
    # Effectively off by default so the retry and DLQ tests are not masked by the breaker
    # tripping first. Breaker tests construct their own with a real threshold.
    HOOKLINE_CIRCUIT_BREAKER_FAILURE_THRESHOLD="100000",
    HOOKLINE_CIRCUIT_BREAKER_COOLDOWN_SECONDS="0.3",
    HOOKLINE_CIRCUIT_BREAKER_BACKEND="memory",
    HOOKLINE_STALE_DELIVERY_TIMEOUT_SECONDS="1",
    HOOKLINE_METRICS_ENABLED="true",
)
os.environ.pop("HOOKLINE_OTEL_ENDPOINT", None)

# Safe to import hookline from here on.
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from hookline.config import get_settings  # noqa: E402


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    for container in _containers:
        container.stop()  # type: ignore[attr-defined]


# --------------------------------------------------------------------- schema


def _maintenance_url(url: str) -> tuple[str, str]:
    """Split a DSN into (server URL pointing at `postgres`, target database name)."""
    base, _, dbname = url.rpartition("/")
    return f"{base}/postgres", dbname


@pytest.fixture(scope="session", autouse=True)
def _database() -> Iterator[None]:
    """Create the test database if needed, then migrate it.

    Migrations run through the real `alembic upgrade head`, in a subprocess. Two reasons:
    Alembic's async template calls `asyncio.run` itself, which cannot be nested inside a
    running loop; and running the actual migrations means the suite fails when a migration
    is broken, which `Base.metadata.create_all` would quietly hide.
    """
    if os.environ.get("HOOKLINE_TEST_CONTAINERS") != "1":
        _ensure_database_exists()

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")
    yield


def _ensure_database_exists() -> None:
    import asyncio

    server_url, dbname = _maintenance_url(DATABASE_URL)

    async def create() -> None:
        # AUTOCOMMIT because CREATE DATABASE cannot run inside a transaction block.
        engine = create_async_engine(server_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                exists = await conn.scalar(
                    text("select 1 from pg_database where datname = :name"), {"name": dbname}
                )
                if not exists:
                    await conn.execute(text(f'create database "{dbname}"'))
        finally:
            await engine.dispose()

    asyncio.run(create())


# --------------------------------------------------------------------- per-test state


@pytest.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    """Truncate every table and flush Redis between tests.

    TRUNCATE rather than a rolled-back transaction: the code under test manages its own
    transactions and commits, and the worker uses several sessions per delivery, so
    wrapping a test in one outer transaction would not survive contact with it.
    RESTART IDENTITY CASCADE keeps it to a single statement.

    The pools are deliberately *not* disposed here. They are process-wide singletons bound
    to the event loop that created them, and the loop is session-scoped, so tearing them
    down between tests only costs reconnections.
    """
    from hookline.cache.client import get_redis
    from hookline.db.session import get_sessionmaker

    yield

    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "truncate table api_keys, delivery_attempts, deliveries, events, endpoints "
                "restart identity cascade"
            )
        )
        await session.commit()

    try:
        await get_redis().flushdb()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
async def _close_pools() -> AsyncIterator[None]:
    """Dispose the pools once, at the end, on the loop that owns them."""
    yield

    from hookline.cache.client import close_redis
    from hookline.db.session import dispose_engine

    await dispose_engine()
    await close_redis()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session for arranging fixtures and asserting on rows directly.

    Not the same session the app uses - the app gets its own per request, which is the
    point. Anything written here must be committed before an API call can see it.
    """
    from hookline.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Let a test change a setting without leaking it into the next one."""
    yield
    get_settings.cache_clear()
