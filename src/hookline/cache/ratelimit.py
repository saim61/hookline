"""Distributed token bucket rate limiter.

Token bucket rather than a fixed window because a fixed window has a boundary problem:
with a 100/minute limit, a caller can send 100 at 11:59:59 and another 100 at 12:00:00
and stay inside the rules while delivering 200 requests in one second. A bucket has no
boundaries - it refills continuously, so the sustained rate is the refill rate and the
burst is the capacity, and both are enforced at every instant.

The whole check runs as one Lua script. Read-modify-write from Python would race between
API replicas: two processes read 1 token remaining, both decide they may proceed, both
write 0, and two requests get through on one token. Redis runs a script atomically, so
the read, the refill and the decrement cannot be interleaved.
"""

from dataclasses import dataclass

from redis.asyncio import Redis

from hookline.observability.logging import get_logger

log = get_logger("hookline.ratelimit")

# Returns {allowed, tokens_left, retry_after_ms}.
#
# State is two fields plus a TTL rather than a counter per window: `t` is the token
# count as of `ts`, and refill is computed lazily on read. That means no background
# job, no per-window keys accumulating, and a bucket that costs nothing while idle.
_SCRIPT = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])   -- tokens per second
local now_ms   = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 't', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil then
  tokens = capacity
  ts = now_ms
end

-- Lazy refill: credit whatever accrued since the last touch, capped at capacity.
local elapsed = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  retry_after_ms = math.ceil(((cost - tokens) / refill) * 1000)
end

redis.call('HSET', key, 't', tokens, 'ts', now_ms)
-- Expire after a full refill would have happened anyway: at that point the stored state
-- is indistinguishable from a fresh bucket, so keeping it wastes memory.
redis.call('PEXPIRE', key, math.ceil((capacity / refill) * 1000) + 1000)

return {allowed, math.floor(tokens), retry_after_ms}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    tokens_left: int
    retry_after_seconds: float
    # True when Redis could not be reached and the request was let through anyway.
    degraded: bool = False


class TokenBucketLimiter:
    def __init__(self, redis: Redis, capacity: int, refill_per_second: float) -> None:
        self._redis = redis
        self._capacity = capacity
        self._refill = refill_per_second
        # register_script uses EVALSHA with an automatic EVAL fallback, so the script
        # body is sent once per Redis rather than on every request.
        self._script = redis.register_script(_SCRIPT)

    async def check(self, identity: str, cost: int = 1) -> RateLimitResult:
        """Consume `cost` tokens for `identity`.

        Fails open. A rate limiter is a protection mechanism, not a correctness one - if
        Redis is down, rejecting every request converts a Redis outage into a total
        outage, which is strictly worse than briefly serving unlimited traffic.
        """
        now_ms = await self._now_ms()
        if now_ms is None:
            return RateLimitResult(True, self._capacity, 0.0, degraded=True)

        try:
            allowed, tokens_left, retry_after_ms = await self._script(
                keys=[f"hookline:rl:{identity}"],
                args=[self._capacity, self._refill, now_ms, cost],
            )
        except Exception:
            log.warning("rate limit check failed, allowing request", exc_info=True)
            return RateLimitResult(True, self._capacity, 0.0, degraded=True)

        return RateLimitResult(
            allowed=bool(allowed),
            tokens_left=int(tokens_left),
            retry_after_seconds=int(retry_after_ms) / 1000.0,
        )

    async def _now_ms(self) -> int | None:
        """Redis's clock, not the caller's.

        With several API replicas, using each process's own clock means their buckets
        disagree by whatever their clocks disagree by, and a skewed replica can hand out
        free tokens by claiming more time has passed than really has. One clock for
        everyone removes the question.
        """
        try:
            seconds, microseconds = await self._redis.time()
        except Exception:
            log.warning("redis unreachable, rate limiting disabled", exc_info=True)
            return None
        return int(seconds) * 1000 + int(microseconds) // 1000
