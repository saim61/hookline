"""In-memory circuit breaker. The Redis one is exercised in the integration suite.

Time is injected, so nothing here sleeps. A breaker test that waits out a real 60 second
cooldown is a test nobody runs.
"""

from uuid import uuid4

import pytest

from hookline.delivery.breaker import CircuitState, InMemoryCircuitBreaker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def breaker(clock: Clock) -> InMemoryCircuitBreaker:
    return InMemoryCircuitBreaker(failure_threshold=3, cooldown_seconds=10.0, clock=clock)


async def test_starts_closed(breaker: InMemoryCircuitBreaker) -> None:
    endpoint = uuid4()
    assert await breaker.state_of(endpoint) is CircuitState.CLOSED
    assert await breaker.allows(endpoint) is True


async def test_opens_only_at_the_threshold(breaker: InMemoryCircuitBreaker) -> None:
    endpoint = uuid4()
    for _ in range(2):
        await breaker.record_failure(endpoint)
    assert await breaker.state_of(endpoint) is CircuitState.CLOSED

    await breaker.record_failure(endpoint)
    assert await breaker.state_of(endpoint) is CircuitState.OPEN
    assert await breaker.allows(endpoint) is False


async def test_success_resets_the_counter_not_just_the_state(
    breaker: InMemoryCircuitBreaker,
) -> None:
    """Two failures, a success, then two more must not open it.

    If success only flipped the state and left the counter, an endpoint that fails
    intermittently would eventually trip on cumulative failures rather than consecutive
    ones - which is not what "consecutive" means and would take healthy endpoints offline.
    """
    endpoint = uuid4()
    await breaker.record_failure(endpoint)
    await breaker.record_failure(endpoint)
    await breaker.record_success(endpoint)
    await breaker.record_failure(endpoint)
    await breaker.record_failure(endpoint)
    assert await breaker.state_of(endpoint) is CircuitState.CLOSED


async def test_cooldown_must_fully_elapse(breaker: InMemoryCircuitBreaker, clock: Clock) -> None:
    endpoint = uuid4()
    for _ in range(3):
        await breaker.record_failure(endpoint)

    clock.advance(9.99)
    assert await breaker.allows(endpoint) is False

    clock.advance(0.01)
    assert await breaker.state_of(endpoint) is CircuitState.HALF_OPEN


async def test_half_open_admits_exactly_one_probe(
    breaker: InMemoryCircuitBreaker, clock: Clock
) -> None:
    """The probe is one request, not a released backlog.

    Without the single-slot rule, the moment the cooldown expires every queued delivery
    for that endpoint would go through at once - which is the stampede the breaker exists
    to prevent, just delayed by the cooldown.
    """
    endpoint = uuid4()
    for _ in range(3):
        await breaker.record_failure(endpoint)
    clock.advance(10.0)

    assert await breaker.allows(endpoint) is True
    assert await breaker.allows(endpoint) is False
    assert await breaker.allows(endpoint) is False


async def test_failed_probe_reopens_with_a_fresh_cooldown(
    breaker: InMemoryCircuitBreaker, clock: Clock
) -> None:
    endpoint = uuid4()
    for _ in range(3):
        await breaker.record_failure(endpoint)
    clock.advance(10.0)
    await breaker.allows(endpoint)  # claim the probe
    await breaker.record_failure(endpoint)  # probe fails

    assert await breaker.state_of(endpoint) is CircuitState.OPEN
    clock.advance(9.9)
    assert await breaker.allows(endpoint) is False
    clock.advance(0.1)
    assert await breaker.allows(endpoint) is True


async def test_successful_probe_closes_the_circuit(
    breaker: InMemoryCircuitBreaker, clock: Clock
) -> None:
    endpoint = uuid4()
    for _ in range(3):
        await breaker.record_failure(endpoint)
    clock.advance(10.0)
    await breaker.allows(endpoint)
    await breaker.record_success(endpoint)

    assert await breaker.state_of(endpoint) is CircuitState.CLOSED
    assert await breaker.allows(endpoint) is True
    assert await breaker.allows(endpoint) is True  # no probe limit any more


async def test_endpoints_are_independent(breaker: InMemoryCircuitBreaker) -> None:
    """One broken customer must not stop deliveries to everyone else."""
    broken, healthy = uuid4(), uuid4()
    for _ in range(3):
        await breaker.record_failure(broken)

    assert await breaker.allows(broken) is False
    assert await breaker.allows(healthy) is True


async def test_monotonic_clock_is_the_default() -> None:
    """Not time.time: a clock adjustment must not make a circuit look cooled down."""
    import time

    assert InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=1).clock is time.monotonic
