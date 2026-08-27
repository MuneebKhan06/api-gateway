"""Circuit breaker state machine.

If an upstream is down, sending it more requests helps nobody. Each one waits
out a timeout, holds a connection while it does, and delays the upstream's own
recovery. The breaker notices the failures and starts refusing immediately.

    CLOSED  normal, requests pass through
      |
      |  failure_threshold failures inside failure_window_seconds
      v
    OPEN    every request refused at once with 503
      |
      |  recovery_timeout_seconds elapses
      v
    HALF_OPEN  one trial request allowed through
      |
      |-- success_threshold consecutive successes --> CLOSED
      |-- any failure                              --> OPEN, timer reset

This module is pure logic and holds no state of its own. It takes a snapshot
of where a breaker currently is, plus the clock, and returns what should
happen next. The Redis read/modify/write lives in store.py.

Keeping it separate means every transition here is testable without a Redis,
and the tricky part (atomicity) stays in one place instead of being smeared
across the state machine.
"""

from dataclasses import dataclass, replace
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerConfig:
    #: Failures inside the window needed to open a closed breaker.
    failure_threshold: int = 5
    #: How far back failures are counted. Older ones no longer count.
    failure_window_seconds: int = 60
    #: How long a breaker stays open before allowing a trial request.
    recovery_timeout_seconds: int = 30
    #: Consecutive successes in HALF_OPEN needed to close.
    success_threshold: int = 2

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.success_threshold < 1:
            raise ValueError("success_threshold must be at least 1")
        if self.failure_window_seconds <= 0:
            raise ValueError("failure_window_seconds must be positive")
        if self.recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive")


@dataclass(frozen=True)
class BreakerSnapshot:
    """Where one upstream's breaker currently stands."""

    state: BreakerState = BreakerState.CLOSED
    #: Failures counted so far in the current window.
    failures: int = 0
    #: Consecutive successes since entering HALF_OPEN.
    successes: int = 0
    #: When the breaker last moved to OPEN.
    opened_at: float | None = None
    #: When the most recent failure was recorded, for windowing.
    first_failure_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN


def recovery_due(snapshot: BreakerSnapshot, config: BreakerConfig, now: float) -> bool:
    """Has an open breaker waited long enough to try again?"""
    if snapshot.state is not BreakerState.OPEN or snapshot.opened_at is None:
        return False
    return (now - snapshot.opened_at) >= config.recovery_timeout_seconds


def effective_state(
    snapshot: BreakerSnapshot, config: BreakerConfig, now: float
) -> BreakerState:
    """The state accounting for elapsed time.

    A breaker sitting in OPEN past its recovery timeout is really HALF_OPEN;
    nothing writes that transition until a request arrives, because there is
    no background timer. This resolves it on read.
    """
    if recovery_due(snapshot, config, now):
        return BreakerState.HALF_OPEN
    return snapshot.state


def allows_request(snapshot: BreakerSnapshot, config: BreakerConfig, now: float) -> bool:
    """Should this request be attempted against the upstream?"""
    return effective_state(snapshot, config, now) is not BreakerState.OPEN


def on_success(
    snapshot: BreakerSnapshot, config: BreakerConfig, now: float
) -> BreakerSnapshot:
    """Fold a successful call into the breaker state."""
    state = effective_state(snapshot, config, now)

    if state is BreakerState.HALF_OPEN:
        successes = snapshot.successes + 1
        if successes >= config.success_threshold:
            # Recovered. Everything resets.
            return BreakerSnapshot(state=BreakerState.CLOSED)
        return replace(
            snapshot, state=BreakerState.HALF_OPEN, successes=successes, failures=0
        )

    # A success in CLOSED clears the failure count. Failures have to be
    # consecutive-ish within the window to mean anything: a service handling
    # traffic fine with the occasional blip is not a service that is down.
    return BreakerSnapshot(state=BreakerState.CLOSED)


def on_failure(
    snapshot: BreakerSnapshot, config: BreakerConfig, now: float
) -> BreakerSnapshot:
    """Fold a failed call into the breaker state."""
    state = effective_state(snapshot, config, now)

    if state is BreakerState.HALF_OPEN:
        # The trial request failed, so the upstream is still unwell. Back to
        # OPEN with the recovery timer restarted from now.
        return BreakerSnapshot(state=BreakerState.OPEN, opened_at=now)

    if state is BreakerState.OPEN:
        # Already open; nothing to count.
        return snapshot

    # CLOSED. Count the failure, but only within the window: failures spread
    # thinly over hours should not eventually add up to an open breaker.
    window_start = snapshot.first_failure_at
    if window_start is None or (now - window_start) > config.failure_window_seconds:
        failures = 1
        window_start = now
    else:
        failures = snapshot.failures + 1

    if failures >= config.failure_threshold:
        return BreakerSnapshot(state=BreakerState.OPEN, opened_at=now)

    return BreakerSnapshot(
        state=BreakerState.CLOSED, failures=failures, first_failure_at=window_start
    )


def counts_as_failure(status_code: int | None, exception: Exception | None = None) -> bool:
    """Does this outcome say the upstream is unwell?

    A 5xx, a timeout or a refused connection do. A 4xx does not: a flood of
    malformed client requests is a client problem, and letting it open the
    breaker would let any client take a healthy upstream offline for everyone.
    """
    if exception is not None:
        return True
    if status_code is None:
        return True
    return status_code >= 500
