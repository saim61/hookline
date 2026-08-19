"""Rate limiting, subscriber cache and the shared circuit breaker, against real Redis."""

import asyncio
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from hookline.cache.client import get_redis
from hookline.cache.ratelimit import TokenBucketLimiter
from hookline.cache.subscribers import SubscriberCache
from hookline.delivery.breaker import CircuitState, RedisCircuitBreaker


@pytest.fixture
def redis() -> Redis:
    return get_redis()


class TestTokenBucket:
    async def test_burst_is_the_capacity(self, redis: Redis) -> None:
        limiter = TokenBucketLimiter(redis, capacity=5, refill_per_second=1.0)
        identity = f"burst-{uuid4().hex[:8]}"

        results = [await limiter.check(identity) for _ in range(7)]
        assert [r.allowed for r in results[:5]] == [True] * 5
        assert [r.allowed for r in results[5:]] == [False, False]
        assert results[4].tokens_left == 0
        assert results[5].retry_after_seconds > 0

    async def test_refills_continuously(self, redis: Redis) -> None:
        """A bucket, not a window.

        A fixed 100/minute window lets a caller send 100 at 11:59:59 and 100 more at
        12:00:00 - 200 requests in one second while technically inside the rules.
        """
        limiter = TokenBucketLimiter(redis, capacity=2, refill_per_second=4.0)
        identity = f"refill-{uuid4().hex[:8]}"

        assert (await limiter.check(identity)).allowed
        assert (await limiter.check(identity)).allowed
        assert not (await limiter.check(identity)).allowed

        await asyncio.sleep(0.35)  # ~1.4 tokens at 4/s
        assert (await limiter.check(identity)).allowed

    async def test_buckets_are_per_identity(self, redis: Redis) -> None:
        limiter = TokenBucketLimiter(redis, capacity=1, refill_per_second=0.001)
        assert (await limiter.check(f"a-{uuid4().hex[:8]}")).allowed
        assert (await limiter.check(f"b-{uuid4().hex[:8]}")).allowed

    async def test_concurrent_checks_cannot_oversell(self, redis: Redis) -> None:
        """The reason the whole check is one Lua script.

        Read-modify-write from Python races between replicas: two processes read "1 token
        left", both proceed, both write 0, and two requests go through on one token.
        """
        limiter = TokenBucketLimiter(redis, capacity=10, refill_per_second=0.001)
        identity = f"race-{uuid4().hex[:8]}"

        results = await asyncio.gather(*[limiter.check(identity) for _ in range(60)])
        assert sum(1 for r in results if r.allowed) == 10

    async def test_cost_greater_than_one(self, redis: Redis) -> None:
        limiter = TokenBucketLimiter(redis, capacity=10, refill_per_second=0.001)
        identity = f"cost-{uuid4().hex[:8]}"

        assert (await limiter.check(identity, cost=7)).allowed
        assert not (await limiter.check(identity, cost=7)).allowed
        assert (await limiter.check(identity, cost=3)).allowed


class TestApiRateLimit:
    async def test_writes_are_throttled(self, api, monkeypatch) -> None:
        """Configured tightly for this test only; the suite's global limit is generous."""
        from hookline.api import deps
        from hookline.cache.ratelimit import TokenBucketLimiter as Limiter

        real_settings = deps.get_settings()
        monkeypatch.setattr(
            deps,
            "TokenBucketLimiter",
            lambda redis, capacity, refill_per_second: Limiter(
                redis, capacity=5, refill_per_second=0.001
            ),
        )
        assert real_settings.rate_limit_enabled

        body = {"event_type": "a.b", "payload": {}}
        codes = [
            r.status_code
            for r in await asyncio.gather(
                *[api.post("/api/v1/events", json=body) for _ in range(20)]
            )
        ]
        assert 202 in codes
        assert 429 in codes
        assert set(codes) <= {202, 429}

    async def test_429_carries_retry_after(self, api, monkeypatch) -> None:
        """Without it a client's only option is to guess, and clients guess "immediately"."""
        from hookline.api import deps
        from hookline.cache.ratelimit import TokenBucketLimiter as Limiter

        monkeypatch.setattr(
            deps,
            "TokenBucketLimiter",
            lambda redis, capacity, refill_per_second: Limiter(
                redis, capacity=1, refill_per_second=0.05
            ),
        )
        body = {"event_type": "a.b", "payload": {}}
        await api.post("/api/v1/events", json=body)
        limited = await api.post("/api/v1/events", json=body)

        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1

    async def test_reads_are_not_throttled(self, api, monkeypatch) -> None:
        """Throttling the DLQ while an operator works through an incident is unhelpful,
        and reads are cheap."""
        from hookline.api import deps
        from hookline.cache.ratelimit import TokenBucketLimiter as Limiter

        monkeypatch.setattr(
            deps,
            "TokenBucketLimiter",
            lambda redis, capacity, refill_per_second: Limiter(
                redis, capacity=1, refill_per_second=0.001
            ),
        )
        codes = {
            r.status_code
            for r in await asyncio.gather(*[api.get("/api/v1/events?limit=1") for _ in range(15)])
        }
        assert codes == {200}


class TestSubscriberCache:
    async def test_round_trip(self, redis: Redis) -> None:
        cache = SubscriberCache(redis, ttl_seconds=30)
        event_type = f"c.{uuid4().hex[:8]}"

        assert await cache.get(event_type) is None
        ids = [uuid4(), uuid4()]
        await cache.set(event_type, ids)
        assert await cache.get(event_type) == ids

    async def test_empty_list_is_a_value_not_a_miss(self, redis: Redis) -> None:
        """Unsubscribed event types are common. Treating "nobody is listening" as a miss
        would send exactly those to the database every single time."""
        cache = SubscriberCache(redis, ttl_seconds=30)
        event_type = f"c.{uuid4().hex[:8]}"

        await cache.set(event_type, [])
        assert await cache.get(event_type) == []

    async def test_invalidation_is_targeted(self, redis: Redis) -> None:
        """Only the affected types, not the namespace - a flush-style invalidation would
        make the cache useless for anyone whose endpoint list changes."""
        cache = SubscriberCache(redis, ttl_seconds=30)
        a, b = f"a.{uuid4().hex[:6]}", f"b.{uuid4().hex[:6]}"
        await cache.set(a, [uuid4()])
        await cache.set(b, [uuid4()])

        await cache.invalidate([a])
        assert await cache.get(a) is None
        assert await cache.get(b) is not None

    async def test_new_endpoint_receives_the_very_next_event(self, api) -> None:
        """Invalidation on registration, not just TTL.

        Waiting out a TTL would mean a newly registered endpoint silently misses events -
        which to whoever just registered looks exactly like the service being broken.
        """
        event_type = f"fresh.{uuid4().hex[:8]}"
        first = await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        assert first.json()["deliveries_scheduled"] == 0

        await api.post(
            "/api/v1/endpoints",
            json={"url": "https://fresh.example.com/h", "event_types": [event_type]},
        )
        second = await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        assert second.json()["deliveries_scheduled"] == 1

    async def test_a_stale_cached_id_cannot_break_ingest(self, api, redis: Redis) -> None:
        """The fan-out INSERT filters against live, active endpoints, so a cached id
        belonging to a deleted endpoint is dropped by the database rather than raising a
        foreign key error and turning an ingest into a 500."""
        event_type = f"stale.{uuid4().hex[:8]}"
        endpoint = (
            await api.post(
                "/api/v1/endpoints",
                json={"url": "https://doomed.example.com/h", "event_types": [event_type]},
            )
        ).json()
        await api.delete(f"/api/v1/endpoints/{endpoint['id']}")

        # Re-poison the cache after the delete cleared it.
        await SubscriberCache(redis, 30).set(event_type, [uuid4().__class__(endpoint["id"])])

        response = await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        assert response.status_code == 202
        assert response.json()["deliveries_scheduled"] == 0


class TestSharedCircuitBreaker:
    async def test_full_state_machine(self, redis: Redis) -> None:
        endpoint = uuid4()
        breaker = RedisCircuitBreaker(redis, failure_threshold=3, cooldown_seconds=0.4)

        assert await breaker.state_of(endpoint) is CircuitState.CLOSED
        await breaker.record_failure(endpoint)
        await breaker.record_failure(endpoint)
        assert await breaker.state_of(endpoint) is CircuitState.CLOSED

        await breaker.record_failure(endpoint)
        assert await breaker.state_of(endpoint) is CircuitState.OPEN
        assert await breaker.allows(endpoint) is False

        await asyncio.sleep(0.45)
        assert await breaker.state_of(endpoint) is CircuitState.HALF_OPEN
        assert await breaker.allows(endpoint) is True
        assert await breaker.allows(endpoint) is False  # one probe only

        await breaker.record_failure(endpoint)
        assert await breaker.state_of(endpoint) is CircuitState.OPEN

        await asyncio.sleep(0.45)
        assert await breaker.allows(endpoint) is True
        await breaker.record_success(endpoint)
        assert await breaker.state_of(endpoint) is CircuitState.CLOSED

    async def test_state_is_shared_between_workers(self, redis: Redis) -> None:
        """The reason for the Redis backend at all.

        With the in-memory one, each worker learns independently that an endpoint is down,
        so it absorbs up to N times the threshold in failures first.
        """
        endpoint = uuid4()
        worker_a = RedisCircuitBreaker(redis, failure_threshold=3, cooldown_seconds=5.0)
        worker_b = RedisCircuitBreaker(redis, failure_threshold=3, cooldown_seconds=5.0)

        await worker_a.record_failure(endpoint)
        await worker_a.record_failure(endpoint)
        await worker_b.record_failure(endpoint)  # third failure, seen by the other worker

        assert await worker_b.state_of(endpoint) is CircuitState.OPEN
        assert await worker_a.allows(endpoint) is False

    async def test_only_one_worker_gets_the_probe(self, redis: Redis) -> None:
        """HSETNX is the atomic claim.

        Without it, fifty workers reaching half-open together would each send "one" probe -
        which is not a probe, it is a small flood at exactly the wrong moment.
        """
        endpoint = uuid4()
        breakers = [
            RedisCircuitBreaker(redis, failure_threshold=1, cooldown_seconds=0.3) for _ in range(12)
        ]
        await breakers[0].record_failure(endpoint)
        await asyncio.sleep(0.35)

        allowed = await asyncio.gather(*[b.allows(endpoint) for b in breakers])
        assert sum(allowed) == 1

    async def test_endpoints_are_independent(self, redis: Redis) -> None:
        breaker = RedisCircuitBreaker(redis, failure_threshold=1, cooldown_seconds=5.0)
        broken, healthy = uuid4(), uuid4()

        await breaker.record_failure(broken)
        assert await breaker.allows(broken) is False
        assert await breaker.allows(healthy) is True


class TestGracefulDegradation:
    """Everything in Redis is derived state, so every caller fails open.

    A dead Redis pointed at a closed port stands in for an outage without stopping the
    container the rest of the suite is using.
    """

    @pytest.fixture
    def dead_redis(self) -> Redis:
        from redis.asyncio import ConnectionPool

        pool = ConnectionPool.from_url(
            "redis://127.0.0.1:6399/0",
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        return Redis(connection_pool=pool)

    async def test_rate_limiter_allows_the_request(self, dead_redis: Redis) -> None:
        """Rejecting everything would convert a Redis outage into a total outage, which is
        strictly worse than briefly serving unlimited traffic."""
        limiter = TokenBucketLimiter(dead_redis, capacity=1, refill_per_second=0.001)
        result = await limiter.check("anyone")

        assert result.allowed is True
        assert result.degraded is True

    async def test_breaker_attempts_the_delivery(self, dead_redis: Redis) -> None:
        """It exists to protect customer endpoints from us; if it is unavailable the right
        fallback is to deliver, since the retry budget still bounds the damage."""
        breaker = RedisCircuitBreaker(dead_redis, failure_threshold=1, cooldown_seconds=1)
        assert await breaker.allows(uuid4()) is True
        assert await breaker.state_of(uuid4()) is CircuitState.CLOSED

    async def test_cache_reads_as_a_miss(self, dead_redis: Redis) -> None:
        cache = SubscriberCache(dead_redis, ttl_seconds=30)
        assert await cache.get("anything") is None
        await cache.set("anything", [uuid4()])  # must not raise
        await cache.invalidate(["anything"])  # must not raise
