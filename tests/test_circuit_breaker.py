"""State machine tests.

Pure logic, no Redis. Time is passed in explicitly so transitions that depend
on elapsed time can be tested without sleeping.
"""

import pytest

from gateway.circuit_breaker.breaker import (
    BreakerConfig,
    BreakerSnapshot,
    BreakerState,
    allows_request,
    counts_as_failure,
    effective_state,
    on_failure,
    on_success,
    recovery_due,
)

NOW = 1_000_000.0


@pytest.fixture
def config():
    return BreakerConfig(
        failure_threshold=3,
        failure_window_seconds=60,
        recovery_timeout_seconds=30,
        success_threshold=2,
    )


def fail_repeatedly(snapshot, config, times, now=NOW, step=0.0):
    for i in range(times):
        snapshot = on_failure(snapshot, config, now + i * step)
    return snapshot


class TestConfigValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"failure_threshold": 0},
            {"success_threshold": 0},
            {"failure_window_seconds": 0},
            {"recovery_timeout_seconds": -1},
        ],
    )
    def test_nonsense_config_is_rejected(self, kwargs):
        with pytest.raises(ValueError):
            BreakerConfig(**kwargs)

    def test_defaults_match_the_documented_thresholds(self):
        config = BreakerConfig()
        assert config.failure_threshold == 5
        assert config.recovery_timeout_seconds == 30
        assert config.success_threshold == 2


class TestClosedState:
    def test_starts_closed(self):
        assert BreakerSnapshot().state is BreakerState.CLOSED

    def test_closed_allows_requests(self, config):
        assert allows_request(BreakerSnapshot(), config, NOW) is True

    def test_failures_below_the_threshold_stay_closed(self, config):
        snapshot = fail_repeatedly(BreakerSnapshot(), config, 2)
        assert snapshot.state is BreakerState.CLOSED
        assert snapshot.failures == 2

    def test_reaching_the_threshold_opens(self, config):
        snapshot = fail_repeatedly(BreakerSnapshot(), config, 3)
        assert snapshot.state is BreakerState.OPEN
        assert snapshot.opened_at == NOW

    def test_a_success_clears_accumulated_failures(self, config):
        snapshot = fail_repeatedly(BreakerSnapshot(), config, 2)
        snapshot = on_success(snapshot, config, NOW)
        assert snapshot.failures == 0
        assert snapshot.state is BreakerState.CLOSED

    def test_success_between_failures_prevents_opening(self, config):
        """A service handling traffic with the occasional blip is not down."""
        snapshot = BreakerSnapshot()
        for _ in range(5):
            snapshot = on_failure(snapshot, config, NOW)
            snapshot = on_failure(snapshot, config, NOW)
            snapshot = on_success(snapshot, config, NOW)
        assert snapshot.state is BreakerState.CLOSED


class TestFailureWindow:
    def test_failures_spread_past_the_window_do_not_accumulate(self, config):
        """Three failures an hour apart is not an outage."""
        snapshot = fail_repeatedly(BreakerSnapshot(), config, 3, step=3600)
        assert snapshot.state is BreakerState.CLOSED
        assert snapshot.failures == 1

    def test_failures_inside_the_window_do_accumulate(self, config):
        snapshot = fail_repeatedly(BreakerSnapshot(), config, 3, step=10)
        assert snapshot.state is BreakerState.OPEN

    def test_window_restarts_after_it_lapses(self, config):
        snapshot = fail_repeatedly(BreakerSnapshot(), config, 2)
        # 61 seconds later the earlier failures are out of the window.
        snapshot = on_failure(snapshot, config, NOW + 61)
        assert snapshot.failures == 1
        assert snapshot.state is BreakerState.CLOSED


class TestOpenState:
    @pytest.fixture
    def opened(self, config):
        return fail_repeatedly(BreakerSnapshot(), config, 3)

    def test_open_refuses_requests(self, config, opened):
        assert allows_request(opened, config, NOW) is False

    def test_still_refuses_before_the_recovery_timeout(self, config, opened):
        assert allows_request(opened, config, NOW + 29) is False

    def test_allows_a_trial_once_the_timeout_passes(self, config, opened):
        assert allows_request(opened, config, NOW + 30) is True

    def test_reads_as_half_open_after_the_timeout(self, config, opened):
        """There is no background timer, so the transition has to resolve on
        read rather than being written by something."""
        assert effective_state(opened, config, NOW + 29) is BreakerState.OPEN
        assert effective_state(opened, config, NOW + 31) is BreakerState.HALF_OPEN

    def test_recovery_due_only_applies_to_open_breakers(self, config):
        assert recovery_due(BreakerSnapshot(), config, NOW + 9999) is False

    def test_failures_while_open_are_not_counted(self, config, opened):
        after = on_failure(opened, config, NOW + 1)
        assert after == opened


class TestHalfOpenState:
    @pytest.fixture
    def half_open(self, config):
        opened = fail_repeatedly(BreakerSnapshot(), config, 3)
        # Past the recovery timeout, so it reads as HALF_OPEN.
        return opened

    def test_one_success_is_not_enough_to_close(self, config, half_open):
        after = on_success(half_open, config, NOW + 31)
        assert after.state is BreakerState.HALF_OPEN
        assert after.successes == 1

    def test_enough_consecutive_successes_close_it(self, config, half_open):
        after = on_success(half_open, config, NOW + 31)
        after = on_success(after, config, NOW + 32)
        assert after.state is BreakerState.CLOSED
        assert after.failures == 0

    def test_a_failed_trial_reopens_it(self, config, half_open):
        after = on_failure(half_open, config, NOW + 31)
        assert after.state is BreakerState.OPEN

    def test_a_failed_trial_restarts_the_recovery_timer(self, config, half_open):
        after = on_failure(half_open, config, NOW + 31)
        assert after.opened_at == NOW + 31
        # The clock starts again from the failed trial, not the original open.
        assert allows_request(after, config, NOW + 40) is False
        assert allows_request(after, config, NOW + 61) is True

    def test_failure_after_partial_recovery_discards_the_successes(self, config, half_open):
        after = on_success(half_open, config, NOW + 31)
        assert after.successes == 1
        after = on_failure(after, config, NOW + 32)
        assert after.state is BreakerState.OPEN
        assert after.successes == 0


class TestFullCycle:
    def test_closed_to_open_to_half_open_to_closed(self, config):
        snapshot = BreakerSnapshot()
        assert snapshot.state is BreakerState.CLOSED

        snapshot = fail_repeatedly(snapshot, config, 3)
        assert snapshot.state is BreakerState.OPEN

        assert effective_state(snapshot, config, NOW + 31) is BreakerState.HALF_OPEN

        snapshot = on_success(snapshot, config, NOW + 31)
        snapshot = on_success(snapshot, config, NOW + 32)
        assert snapshot.state is BreakerState.CLOSED

        # And it can open again afterwards.
        snapshot = fail_repeatedly(snapshot, config, 3, now=NOW + 100)
        assert snapshot.state is BreakerState.OPEN


class TestFailureClassification:
    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_server_errors_count(self, status):
        assert counts_as_failure(status) is True

    @pytest.mark.parametrize("status", [200, 201, 301, 400, 401, 404, 409, 429])
    def test_client_errors_and_successes_do_not_count(self, status):
        """A flood of bad client requests must not let any client take a
        healthy upstream offline for everyone else."""
        assert counts_as_failure(status) is False

    def test_an_exception_counts(self):
        assert counts_as_failure(None, TimeoutError("timed out")) is True

    def test_no_response_at_all_counts(self):
        assert counts_as_failure(None) is True

    def test_an_exception_counts_even_with_a_status(self):
        assert counts_as_failure(200, ConnectionError("reset")) is True
