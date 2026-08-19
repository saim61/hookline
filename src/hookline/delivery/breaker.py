"""Per-endpoint circuit breaker, in two interchangeable implementations.

Backoff alone is not enough. If one customer's endpoint has been dead for a day and a
thousand events are queued for it, the worker still burns a slot and a full HTTP timeout
on every single one, starving the endpoints that are actually up. The breaker notices
that an endpoint keeps failing and stops trying it for a while.

Three states:

    closed     normal. Failures counted; enough consecutive ones opens the circuit.
    open       skip this endpoint entirely, no request is made, until the cooldown ends.
    half_open  cooldown elapsed. Exactly one request is let through as a probe.
               It succeeds -> closed. It fails -> open again for another cooldown.

The half-open probe is the part that matters: without it, recovery means either waiting
for a fixed timer and then releasing the whole backlog at once, or never noticing the
endpoint came back. One request answers the question at the cost of one request.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

from hookline.observability.logging import get_logger

log = get_logger("hookline.breaker")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Breaker(Protocol):
    """What the worker needs. Async because the Redis implementation has to be.

    The in-memory version has nothing to await, which is the same trick the Phase 1
    in-memory store used: make the interface async at the boundary that will eventually
    do I/O, so swapping the implementation later is not a refactor of every caller.
    """

    async def allows(self, endpoint_id: UUID) -> bool: ...
    async def record_success(self, endpoint_id: UUID) -> None: ...
    async def record_failure(self, endpoint_id: UUID) -> None: ...
    async def state_of(self, endpoint_id: UUID) -> CircuitState: ...


# --------------------------------------------------------------------- in memory


@dataclass(slots=True)
class _EndpointState:
    consecutive_failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


@dataclass(slots=True)
class InMemoryCircuitBreaker:
    """State per process, so N workers hold N independent views.

    An endpoint may take up to N times the threshold in failures before every worker has
    tripped. Correctness is unaffected - deliveries are still retried and never lost - it
    just makes the protection weaker than the numbers suggest. Useful when there is one
    worker, or when Redis is not available.
    """

    failure_threshold: int
    cooldown_seconds: float
    # Injected so tests can advance time without sleeping. monotonic, not time.time: a
    # clock adjustment must not make a circuit look like it cooled down.
    clock: Callable[[], float] = time.monotonic
    _states: dict[UUID, _EndpointState] = field(default_factory=dict)

    def _state_for(self, endpoint_id: UUID) -> _EndpointState:
        return self._states.setdefault(endpoint_id, _EndpointState())

    async def state_of(self, endpoint_id: UUID) -> CircuitState:
        state = self._states.get(endpoint_id)
        if state is None or state.opened_at is None:
            return CircuitState.CLOSED
        if self.clock() - state.opened_at >= self.cooldown_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    async def allows(self, endpoint_id: UUID) -> bool:
        state = self._state_for(endpoint_id)
        match await self.state_of(endpoint_id):
            case CircuitState.CLOSED:
                return True
            case CircuitState.OPEN:
                return False
            case CircuitState.HALF_OPEN:
                if state.probe_in_flight:
                    return False
                state.probe_in_flight = True
                return True

    async def record_success(self, endpoint_id: UUID) -> None:
        self._states.pop(endpoint_id, None)

    async def record_failure(self, endpoint_id: UUID) -> None:
        state = self._state_for(endpoint_id)

        if state.probe_in_flight:
            # The probe failed. Still down - start a fresh cooldown rather than letting
            # every queued delivery through behind it.
            state.probe_in_flight = False
            state.opened_at = self.clock()
            return

        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold and state.opened_at is None:
            state.opened_at = self.clock()


# --------------------------------------------------------------------- redis


# Claiming the probe slot has to be atomic, which is the whole reason this is Lua. Two
# workers reaching half-open at the same moment would both read "no probe in flight" and
# both send one, so a recovering endpoint gets hit by one request per worker instead of
# one request total - and with fifty workers that is not a probe, it is a small flood.
_ALLOWS = """
local key      = KEYS[1]
local cooldown = tonumber(ARGV[1])
local now_ms   = tonumber(ARGV[2])

local opened_at = tonumber(redis.call('HGET', key, 'opened_at'))
if opened_at == nil then
  return {1, 'closed'}
end

if (now_ms - opened_at) < cooldown * 1000 then
  return {0, 'open'}
end

-- Half open. HSETNX is the atomic claim: exactly one caller gets the 1.
if redis.call('HSETNX', key, 'probe', '1') == 1 then
  return {1, 'half_open'}
end
return {0, 'half_open'}
"""

_RECORD_FAILURE = """
local key       = KEYS[1]
local threshold = tonumber(ARGV[1])
local now_ms    = tonumber(ARGV[2])
local ttl_ms    = tonumber(ARGV[3])

if redis.call('HGET', key, 'probe') then
  -- The probe failed: reopen with a fresh cooldown instead of releasing the backlog.
  redis.call('HDEL', key, 'probe')
  redis.call('HSET', key, 'opened_at', now_ms)
  redis.call('PEXPIRE', key, ttl_ms)
  return 'open'
end

local failures = redis.call('HINCRBY', key, 'failures', 1)
if failures >= threshold and not redis.call('HGET', key, 'opened_at') then
  redis.call('HSET', key, 'opened_at', now_ms)
end
redis.call('PEXPIRE', key, ttl_ms)
return failures >= threshold and 'open' or 'closed'
"""


class RedisCircuitBreaker:
    """One view of every endpoint's health, shared by every worker.

    Fails open on Redis errors. A breaker exists to protect customer endpoints from us;
    if it is unavailable the correct fallback is to attempt delivery - the retry budget
    and backoff still bound the damage - rather than to stop delivering entirely.
    """

    def __init__(self, redis: Redis, failure_threshold: int, cooldown_seconds: float) -> None:
        self._redis = redis
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._allows_script = redis.register_script(_ALLOWS)
        self._failure_script = redis.register_script(_RECORD_FAILURE)
        # Keys expire well after a cooldown so a long-idle endpoint does not keep a
        # stale failure count forever, but not so soon that state is lost mid-cooldown.
        self._ttl_ms = int(max(cooldown_seconds * 10, 600) * 1000)

    def _key(self, endpoint_id: UUID) -> str:
        return f"hookline:cb:{endpoint_id}"

    async def _now_ms(self) -> int:
        seconds, microseconds = await self._redis.time()
        return int(seconds) * 1000 + int(microseconds) // 1000

    async def allows(self, endpoint_id: UUID) -> bool:
        try:
            allowed, _state = await self._allows_script(
                keys=[self._key(endpoint_id)],
                args=[self.cooldown_seconds, await self._now_ms()],
            )
        except Exception:
            log.warning("breaker check failed, attempting delivery", exc_info=True)
            return True
        return bool(allowed)

    async def state_of(self, endpoint_id: UUID) -> CircuitState:
        try:
            key = self._key(endpoint_id)
            opened_at = await self._redis.hget(key, "opened_at")
            if opened_at is None:
                return CircuitState.CLOSED
            elapsed_ms = await self._now_ms() - int(float(opened_at))
            if elapsed_ms >= self.cooldown_seconds * 1000:
                return CircuitState.HALF_OPEN
            return CircuitState.OPEN
        except Exception:
            log.warning("breaker state read failed", exc_info=True)
            return CircuitState.CLOSED

    async def record_success(self, endpoint_id: UUID) -> None:
        try:
            await self._redis.delete(self._key(endpoint_id))
        except Exception:
            log.warning("breaker success write failed", exc_info=True)

    async def record_failure(self, endpoint_id: UUID) -> None:
        try:
            await self._failure_script(
                keys=[self._key(endpoint_id)],
                args=[self.failure_threshold, await self._now_ms(), self._ttl_ms],
            )
        except Exception:
            log.warning("breaker failure write failed", exc_info=True)
