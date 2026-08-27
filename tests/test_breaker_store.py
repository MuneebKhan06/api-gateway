"""Redis-backed breaker state, against fakeredis executing the real Lua."""

import fakeredis.aioredis
import pytest

from gateway.circuit_breaker.breaker import BreakerConfig, BreakerState
from gateway.circuit_breaker.store import BreakerStore

UPSTREAM = "service-a"


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def config():
    return BreakerConfig(
        failure_threshold=3,
        failure_window_seconds=60,
        recovery_timeout_seconds=30,
        success_threshold=2,
    )


@pytest.fixture
def store(redis_client, config):
    store = BreakerStore(redis_client, config)
    store._clock = 1_000_000.0

    async def frozen_now():
        return store._clock

    store.now = frozen_now
    return store


def advance(store, seconds):
    store._clock += seconds


async def fail(store, times=1):
    for _ in range(times):
        await store.record_failure(UPSTREAM)


class TestInitialState:
    async def test_unknown_upstream_reads_as_closed(self, store):
        snapshot = await store.snapshot(UPSTREAM)
        assert snapshot.state is BreakerState.CLOSED
        assert snapshot.failures == 0

    async def test_unknown_upstream_allows_requests(self, store):
        allowed, state = await store.allow_request(UPSTREAM)
        assert allowed is True
        assert state is BreakerState.CLOSED


class TestOpening:
    async def test_failures_below_the_threshold_stay_closed(self, store):
        await fail(store, 2)
        snapshot = await store.snapshot(UPSTREAM)
        assert snapshot.state is BreakerState.CLOSED
        assert snapshot.failures == 2

    async def test_reaching_the_threshold_opens(self, store):
        await fail(store, 3)
        assert (await store.snapshot(UPSTREAM)).state is BreakerState.OPEN

    async def test_open_refuses_requests(self, store):
        await fail(store, 3)
        allowed, state = await store.allow_request(UPSTREAM)
        assert allowed is False
        assert state is BreakerState.OPEN

    async def test_a_success_clears_the_failure_count(self, store):
        await fail(store, 2)
        await store.record_success(UPSTREAM)
        assert (await store.snapshot(UPSTREAM)).failures == 0

    async def test_failures_outside_the_window_do_not_accumulate(self, store):
        await fail(store, 2)
        advance(store, 61)
        await fail(store, 1)
        snapshot = await store.snapshot(UPSTREAM)
        assert snapshot.state is BreakerState.CLOSED
        assert snapshot.failures == 1


class TestRecovery:
    async def test_still_open_before_the_timeout(self, store):
        await fail(store, 3)
        advance(store, 29)
        allowed, _ = await store.allow_request(UPSTREAM)
        assert allowed is False

    async def test_trial_allowed_once_the_timeout_passes(self, store):
        await fail(store, 3)
        advance(store, 31)
        allowed, state = await store.allow_request(UPSTREAM)
        assert allowed is True
        assert state is BreakerState.HALF_OPEN

    async def test_only_one_trial_request_gets_through(self, store):
        """The reason permission is a single atomic operation. Several
        requests arriving the instant the timeout lapses must not all be sent
        at a service that just proved it was unwell."""
        await fail(store, 3)
        advance(store, 31)

        verdicts = [(await store.allow_request(UPSTREAM))[0] for _ in range(10)]
        assert verdicts.count(True) == 1

    async def test_a_successful_trial_needs_two_successes_to_close(self, store):
        await fail(store, 3)
        advance(store, 31)
        await store.allow_request(UPSTREAM)

        assert await store.record_success(UPSTREAM) is BreakerState.HALF_OPEN
        assert await store.record_success(UPSTREAM) is BreakerState.CLOSED

    async def test_closed_after_recovery_allows_traffic_again(self, store):
        await fail(store, 3)
        advance(store, 31)
        await store.allow_request(UPSTREAM)
        await store.record_success(UPSTREAM)
        await store.record_success(UPSTREAM)

        allowed, state = await store.allow_request(UPSTREAM)
        assert allowed is True
        assert state is BreakerState.CLOSED

    async def test_a_failed_trial_reopens_and_restarts_the_timer(self, store):
        await fail(store, 3)
        advance(store, 31)
        await store.allow_request(UPSTREAM)

        assert await store.record_failure(UPSTREAM) is BreakerState.OPEN

        # The timer runs from the failed trial, not the original opening.
        advance(store, 20)
        assert (await store.allow_request(UPSTREAM))[0] is False
        advance(store, 11)
        assert (await store.allow_request(UPSTREAM))[0] is True

    async def test_an_abandoned_trial_does_not_wedge_the_breaker(self, store):
        """If the process handling the trial dies before reporting back, the
        breaker must not sit in pending forever."""
        await fail(store, 3)
        advance(store, 31)
        await store.allow_request(UPSTREAM)  # claimed, never reported

        advance(store, 5)
        assert (await store.allow_request(UPSTREAM))[0] is False

        advance(store, 60)
        assert (await store.allow_request(UPSTREAM))[0] is True


class TestPendingIsHidden:
    async def test_pending_is_reported_as_half_open(self, store):
        """half_open_pending is internal bookkeeping and should not leak into
        anything a human or an API response reads."""
        await fail(store, 3)
        advance(store, 31)
        await store.allow_request(UPSTREAM)

        assert (await store.snapshot(UPSTREAM)).state is BreakerState.HALF_OPEN


class TestIsolation:
    async def test_upstreams_have_separate_breakers(self, store):
        await fail(store, 3)
        assert (await store.allow_request(UPSTREAM))[0] is False
        assert (await store.allow_request("service-b"))[0] is True


class TestManualControl:
    async def test_reset_closes_an_open_breaker(self, store):
        await fail(store, 3)
        await store.reset(UPSTREAM)
        allowed, state = await store.allow_request(UPSTREAM)
        assert allowed is True
        assert state is BreakerState.CLOSED

    async def test_trip_opens_a_closed_breaker(self, store):
        await store.trip(UPSTREAM)
        allowed, state = await store.allow_request(UPSTREAM)
        assert allowed is False
        assert state is BreakerState.OPEN

    async def test_a_tripped_breaker_still_recovers_on_its_own(self, store):
        await store.trip(UPSTREAM)
        advance(store, 31)
        assert (await store.allow_request(UPSTREAM))[0] is True


class TestExpiry:
    async def test_breaker_state_carries_a_ttl(self, store, redis_client):
        await fail(store, 1)
        assert await redis_client.ttl(BreakerStore.key(UPSTREAM)) > 0

    async def test_a_recovered_breaker_leaves_nothing_behind(self, store, redis_client):
        await fail(store, 2)
        await store.record_success(UPSTREAM)
        # Fully recovered means no state worth keeping.
        assert await redis_client.exists(BreakerStore.key(UPSTREAM)) == 0


class TestSharedAcrossInstances:
    async def test_two_stores_on_one_redis_see_the_same_breaker(self, redis_client, config):
        """The whole reason state is in Redis: one instance discovering an
        outage has to protect the others."""
        instance_one = BreakerStore(redis_client, config)
        instance_two = BreakerStore(redis_client, config)

        for _ in range(3):
            await instance_one.record_failure(UPSTREAM)

        allowed, state = await instance_two.allow_request(UPSTREAM)
        assert allowed is False
        assert state is BreakerState.OPEN
