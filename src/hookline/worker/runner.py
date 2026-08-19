import asyncio
import contextlib
import time
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hookline.cache.client import get_redis
from hookline.cache.ratelimit import TokenBucketLimiter
from hookline.config import Settings
from hookline.delivery.backoff import next_delay_seconds
from hookline.delivery.breaker import Breaker, InMemoryCircuitBreaker, RedisCircuitBreaker
from hookline.delivery.client import DeliveryClient, build_body
from hookline.observability import metrics
from hookline.observability.logging import get_logger
from hookline.repositories.delivery import DeliveryJob, DeliveryRepository

log = get_logger("hookline.worker")


@dataclass(slots=True)
class BatchStats:
    claimed: int = 0
    delivered: int = 0
    retrying: int = 0
    dead: int = 0
    skipped_open_circuit: int = 0
    throttled: int = 0


class DeliveryWorker:
    """Drains the delivery outbox.

    The loop is claim -> deliver -> record, and the three steps deliberately do not
    share a transaction. Claiming is a short write that marks rows in_flight so no other
    worker takes them. Delivering is a slow network call with no transaction open at
    all. Recording is a second short write. Holding a row lock across the HTTP request
    would put every worker's throughput at the mercy of the slowest customer endpoint.

    The cost of that split is a crash window: a worker killed between claim and record
    leaves rows in_flight with nobody working them. `reap_stale` closes it. That makes
    the guarantee at-least-once, not exactly-once - a delivery whose response was lost
    in transit is sent again. Receivers deduplicate on the `webhook-id` header, which is
    stable across retries.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: DeliveryClient,
        breaker: Breaker,
        settings: Settings,
        outbound_limiter: TokenBucketLimiter | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._client = client
        self._breaker = breaker
        self._settings = settings
        self._outbound_limiter = outbound_limiter
        self._last_reap = 0.0

    # ------------------------------------------------------------------ loop

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info(
            "worker started",
            batch_size=self._settings.worker_batch_size,
            poll_interval_seconds=self._settings.worker_poll_interval_seconds,
            max_attempts=self._settings.max_delivery_attempts,
            breaker_backend=self._settings.circuit_breaker_backend,
        )
        while not stop.is_set():
            try:
                stats = await self.run_once()
            except Exception:
                # One bad iteration must not kill the worker. Postgres restarts, a
                # transient network blip, a malformed row - all of these should cost one
                # poll interval, not the whole process.
                log.exception("poll failed, continuing")
                stats = BatchStats()

            if stats.claimed == 0:
                # Nothing due. Wait, but wake immediately on shutdown rather than
                # sleeping out the full interval. asyncio.sleep here would add up to a
                # poll interval of dead time to every restart.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=self._settings.worker_poll_interval_seconds
                    )
        log.info("worker stopped")

    async def run_once(self) -> BatchStats:
        await self._maybe_reap()

        async with self._sessionmaker() as session:
            jobs = await DeliveryRepository(session).claim_batch(self._settings.worker_batch_size)
            await session.commit()

        if not jobs:
            return BatchStats()

        # Grouped by destination, then sequential within a group and parallel across
        # groups. Three reasons this is not just `gather` over the flat batch:
        #
        #   1. The circuit breaker would never fire inside a batch. Firing all twenty
        #      coroutines at once means all twenty pass the `allows()` check before the
        #      first failure is recorded, so a dead endpoint still gets the full batch.
        #      Sequential-per-endpoint lets failure 3 stop attempts 4 through 20.
        #   2. Twenty simultaneous requests is not a friendly thing to do to one
        #      customer's server, particularly one that is already struggling.
        #   3. Deliveries to a destination arrive in roughly the order they were queued,
        #      which is what receivers expect even though retries mean it is not a
        #      guarantee.
        #
        # The cost is throughput per destination: one endpoint with a full batch queued
        # is limited to one in-flight request per worker. Fine while destinations are
        # many and each has few events, which is the normal shape. The outbound token
        # bucket then caps the aggregate across workers, which sequencing alone cannot -
        # ten workers each doing one at a time is still ten concurrent requests.
        by_endpoint: dict[UUID, list[DeliveryJob]] = {}
        for job in jobs:
            by_endpoint.setdefault(job.endpoint_id, []).append(job)

        grouped = await asyncio.gather(
            *(self._process_endpoint(group) for group in by_endpoint.values())
        )

        stats = BatchStats(claimed=len(jobs))
        for result in [r for group in grouped for r in group]:
            match result:
                case "delivered":
                    stats.delivered += 1
                case "retrying":
                    stats.retrying += 1
                case "dead":
                    stats.dead += 1
                case "skipped":
                    stats.skipped_open_circuit += 1
                case "throttled":
                    stats.throttled += 1

        metrics.worker_batches.inc()
        for outcome, count in (
            ("delivered", stats.delivered),
            ("retrying", stats.retrying),
            ("dead", stats.dead),
            ("skipped", stats.skipped_open_circuit),
            ("throttled", stats.throttled),
        ):
            if count:
                metrics.worker_outcomes.labels(outcome=outcome).inc(count)

        log.info(
            "batch",
            claimed=stats.claimed,
            endpoints=len(by_endpoint),
            delivered=stats.delivered,
            retrying=stats.retrying,
            dead=stats.dead,
            skipped_open_circuit=stats.skipped_open_circuit,
            throttled=stats.throttled,
        )
        return stats

    async def _process_endpoint(self, jobs: list[DeliveryJob]) -> list[str]:
        """All claimed deliveries for one destination, one at a time."""
        results: list[str] = []
        for job in jobs:
            try:
                results.append(await self._process(job))
            except Exception:
                # The row stays in_flight and the reaper will return it later. Recording
                # a guessed state would be worse than recording nothing. Carry on with
                # this endpoint's remaining jobs rather than abandoning the group.
                log.exception(
                    "delivery errored outside the client",
                    delivery_id=str(job.delivery_id),
                    endpoint_id=str(job.endpoint_id),
                )
                results.append("errored")
        return results

    # ------------------------------------------------------------------ one delivery

    async def _process(self, job: DeliveryJob) -> str:
        # Checked before the breaker: being over the destination's rate budget is not a
        # health signal about that destination, so it must not count toward opening its
        # circuit. Like a breaker skip, no request is made and no attempt is consumed.
        if self._outbound_limiter is not None:
            budget = await self._outbound_limiter.check(f"ep:{job.endpoint_id}")
            if not budget.allowed:
                async with self._sessionmaker() as session:
                    await DeliveryRepository(session).schedule_retry(
                        job.delivery_id,
                        delay_seconds=max(budget.retry_after_seconds, 0.1),
                        error="deferred: endpoint delivery rate limit",
                    )
                    await session.commit()
                return "throttled"

        if not await self._breaker.allows(job.endpoint_id):
            # No request is made and no attempt is consumed - the endpoint is known to
            # be down, so spending part of this delivery's budget on it would be unfair
            # to the delivery.
            async with self._sessionmaker() as session:
                await DeliveryRepository(session).schedule_retry(
                    job.delivery_id,
                    delay_seconds=self._settings.circuit_breaker_cooldown_seconds,
                    error="skipped: circuit open for this endpoint",
                )
                await session.commit()
            return "skipped"

        body = build_body(
            event_id=job.event_id,
            event_type=job.event_type,
            created_at=job.event_created_at,
            payload=job.payload,
        )
        outcome = await self._client.deliver(
            url=job.url,
            signing_secret=job.signing_secret,
            delivery_id=job.delivery_id,
            body=body,
        )

        metrics.delivery_attempts.labels(
            outcome="success" if outcome.succeeded else "failure",
            status_class=metrics.status_class(outcome.status_code),
        ).inc()
        metrics.delivery_duration.observe(outcome.duration_ms / 1000.0)

        async with self._sessionmaker() as session:
            repo = DeliveryRepository(session)
            await repo.record_attempt(
                delivery_id=job.delivery_id,
                attempt_number=job.attempt_number,
                status_code=outcome.status_code,
                response_body=outcome.response_body,
                error=outcome.error,
                duration_ms=outcome.duration_ms,
            )

            if outcome.succeeded:
                await self._breaker.record_success(job.endpoint_id)
                await repo.mark_delivered(job.delivery_id)
                result = "delivered"
            else:
                await self._breaker.record_failure(job.endpoint_id)
                if not outcome.retryable:
                    await repo.mark_dead(job.delivery_id, f"{outcome.error} (not retryable)")
                    result = "dead"
                elif job.attempt_number >= job.max_attempts:
                    await repo.mark_dead(
                        job.delivery_id,
                        f"{outcome.error} (gave up after {job.attempt_number} attempts)",
                    )
                    result = "dead"
                else:
                    delay = next_delay_seconds(
                        job.attempt_number,
                        base_seconds=self._settings.retry_base_delay_seconds,
                        max_seconds=self._settings.retry_max_delay_seconds,
                    )
                    await repo.schedule_retry(job.delivery_id, delay, outcome.error)
                    result = "retrying"

            await session.commit()

        log.info(
            "attempt",
            delivery_id=str(job.delivery_id),
            endpoint_id=str(job.endpoint_id),
            event_type=job.event_type,
            attempt=job.attempt_number,
            max_attempts=job.max_attempts,
            status_code=outcome.status_code,
            duration_ms=outcome.duration_ms,
            result=result,
            error=outcome.error,
        )
        return result

    # ------------------------------------------------------------------ reaper

    async def _maybe_reap(self) -> None:
        """Run the reaper periodically, not every poll - it is a table-wide scan."""
        interval = self._settings.stale_delivery_timeout_seconds / 2
        now = time.monotonic()
        if now - self._last_reap < interval:
            return
        self._last_reap = now

        async with self._sessionmaker() as session:
            reclaimed = await DeliveryRepository(session).reap_stale(
                self._settings.stale_delivery_timeout_seconds
            )
            await session.commit()
        if reclaimed:
            metrics.stale_reclaimed.inc(reclaimed)
            log.warning("reclaimed stale in_flight deliveries", count=reclaimed)


def build_breaker(settings: Settings) -> Breaker:
    if settings.circuit_breaker_backend == "redis":
        return RedisCircuitBreaker(
            get_redis(),
            failure_threshold=settings.circuit_breaker_failure_threshold,
            cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        )
    return InMemoryCircuitBreaker(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
    )


def build_worker(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> tuple[DeliveryWorker, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        timeout=settings.delivery_timeout_seconds,
        # Customer endpoints redirecting is almost always a misconfiguration, and
        # following one would send the signed body to a URL the signature was not
        # negotiated for.
        follow_redirects=False,
        limits=httpx.Limits(max_connections=settings.worker_batch_size * 2),
    )
    limiter = (
        TokenBucketLimiter(
            get_redis(),
            capacity=settings.delivery_rate_limit_capacity,
            refill_per_second=settings.delivery_rate_limit_per_second,
        )
        if settings.delivery_rate_limit_enabled
        else None
    )
    worker = DeliveryWorker(
        sessionmaker=sessionmaker,
        client=DeliveryClient(http, user_agent=f"{settings.app_name}/0.1.0"),
        breaker=build_breaker(settings),
        settings=settings,
        outbound_limiter=limiter,
    )
    return worker, http
