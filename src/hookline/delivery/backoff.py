import random

# 2 ** 33 seconds already exceeds any sane cap; clamping the exponent keeps a delivery
# with a corrupt attempt_count from overflowing the float rather than being capped.
_MAX_EXPONENT = 32

# Own generator rather than the `random` module's global one, so a caller can pass a
# seeded Random for deterministic tests without reaching into global state.
_SYSTEM_RNG = random.Random()


def next_delay_seconds(
    failed_attempt: int,
    base_seconds: float,
    max_seconds: float,
    rng: random.Random = _SYSTEM_RNG,
) -> float:
    """How long to wait after `failed_attempt` before trying again. 1-based.

    Exponential with equal jitter. Without jitter, a receiver that goes down takes every
    pending delivery with it and hands them all the same retry timestamp. When it comes
    back up they all arrive in the same instant and knock it over again - the retry storm
    the backoff was supposed to prevent. Randomising each delay across the back half of
    its window breaks that synchronisation.

    Equal jitter rather than full jitter (`uniform(0, exp)`): full jitter can schedule a
    retry almost immediately after a failure, wasting an attempt on an endpoint that is
    plainly still down. This keeps a guaranteed minimum wait while still scattering
    arrivals.

    With base=10 and max=3600 the windows after each failure are roughly 5-10s, 10-20s,
    20-40s, 40-80s, 80-160s, converging on 30-60 minutes.
    """
    if failed_attempt < 1:
        raise ValueError(f"failed_attempt must be >= 1, got {failed_attempt}")

    # 2.0 rather than 2: mypy types `int ** int` as Any, because a negative exponent
    # would produce a float. A float base makes the whole expression honestly a float.
    growth = 2.0 ** min(failed_attempt - 1, _MAX_EXPONENT)
    exponential = min(base_seconds * growth, max_seconds)
    half = exponential / 2
    return half + rng.uniform(0, half)
