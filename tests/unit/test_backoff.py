import random
from itertools import pairwise

import pytest

from hookline.delivery.backoff import next_delay_seconds

# base=10, so the equal-jitter window after failure N is [exp/2, exp] where
# exp = 10 * 2^(N-1).
WINDOWS = [(1, 5.0, 10.0), (2, 10.0, 20.0), (3, 20.0, 40.0), (4, 40.0, 80.0), (5, 80.0, 160.0)]


@pytest.mark.parametrize(("attempt", "low", "high"), WINDOWS)
def test_delay_stays_in_its_window(attempt: int, low: float, high: float) -> None:
    rng = random.Random(1234)
    values = [next_delay_seconds(attempt, 10.0, 3600.0, rng) for _ in range(500)]
    assert all(low <= v <= high for v in values)


def test_windows_do_not_overlap() -> None:
    """Each retry waits strictly longer than the last could have.

    The guaranteed floor is what makes the schedule an actual backoff rather than a
    lottery: full jitter (uniform(0, exp)) would let attempt 4 fire sooner than attempt 2,
    wasting a retry on an endpoint that is obviously still down.
    """
    rng = random.Random(7)
    for (_, _, high), (_, next_low, _) in pairwise(WINDOWS):
        assert next_low >= high
    for attempt, low, _ in WINDOWS:
        assert min(next_delay_seconds(attempt, 10.0, 3600.0, rng) for _ in range(200)) >= low


def test_jitter_actually_spreads() -> None:
    """The whole point: two deliveries failing together must not retry together.

    Without jitter every pending delivery for a downed endpoint gets the same retry
    timestamp, so they all return in the same instant and knock it over again.
    """
    rng = random.Random(99)
    values = {round(next_delay_seconds(3, 10.0, 3600.0, rng), 9) for _ in range(200)}
    assert len(values) > 190


def test_cap_applies_and_keeps_its_floor() -> None:
    rng = random.Random(5)
    values = [next_delay_seconds(40, 10.0, 100.0, rng) for _ in range(300)]
    assert max(values) <= 100.0
    # Still half the cap at minimum: capping must not collapse the delay to zero.
    assert min(values) >= 50.0


def test_huge_attempt_number_does_not_overflow() -> None:
    """A corrupt attempt_count must be capped, not turned into inf.

    2 ** 5000 overflows a float; the exponent is clamped so the result is the cap.
    """
    assert next_delay_seconds(5000, 10.0, 100.0) <= 100.0


def test_attempt_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        next_delay_seconds(0, 10.0, 100.0)


def test_deterministic_with_a_seeded_generator() -> None:
    """An injectable rng is what makes the schedule testable at all."""
    a = [next_delay_seconds(2, 10.0, 3600.0, random.Random(42)) for _ in range(5)]
    b = [next_delay_seconds(2, 10.0, 3600.0, random.Random(42)) for _ in range(5)]
    assert a == b
