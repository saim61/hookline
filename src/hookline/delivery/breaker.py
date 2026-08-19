"""Per-endpoint circuit breaker.

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

State is in memory, so it is per worker process: three workers hold three independent
views and an endpoint may take up to three times the threshold in failures before all of
them trip. Correctness is unaffected - deliveries are still retried and never lost - it
just makes the protection weaker than it looks. Phase 5 moves this into Redis so the
workers share one view.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _EndpointState:
    consecutive_failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int
    cooldown_seconds: float
    # Injected so tests can advance time without sleeping. monotonic, not time.time:
    # a clock adjustment must not make a circuit look like it cooled down.
    clock: Callable[[], float] = time.monotonic
    _states: dict[UUID, _EndpointState] = field(default_factory=dict)

    def _state_for(self, endpoint_id: UUID) -> _EndpointState:
        return self._states.setdefault(endpoint_id, _EndpointState())

    def state_of(self, endpoint_id: UUID) -> CircuitState:
        state = self._states.get(endpoint_id)
        if state is None or state.opened_at is None:
            return CircuitState.CLOSED
        if self.clock() - state.opened_at >= self.cooldown_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def allows(self, endpoint_id: UUID) -> bool:
        """Whether a request may be attempted now. Claims the probe slot if half-open."""
        state = self._state_for(endpoint_id)
        match self.state_of(endpoint_id):
            case CircuitState.CLOSED:
                return True
            case CircuitState.OPEN:
                return False
            case CircuitState.HALF_OPEN:
                if state.probe_in_flight:
                    return False
                state.probe_in_flight = True
                return True

    def record_success(self, endpoint_id: UUID) -> None:
        self._states.pop(endpoint_id, None)

    def record_failure(self, endpoint_id: UUID) -> None:
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
