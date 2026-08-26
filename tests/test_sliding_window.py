"""Sliding window log tests.

The defining property is that there is no boundary to exploit, so several of
these deliberately probe the case fixed window gets wrong.
"""

import fakeredis.aioredis
import pytest

from gateway.rate_limit.fixed_window import FixedWindowLimiter
from gateway.rate_limit.sliding_window import SlidingWindowLimiter

ROUTE = "/api/orders"
CLIENT = "user-1"


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def limiter(redis_client):
    return SlidingWindowLimiter(redis_client, limit=5, window_seconds=60)


class FrozenClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    async def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(limiter):
    frozen = FrozenClock()
    limiter._now = frozen
    return frozen


class TestCounting:
    async def test_first_request_is_allowed(self, limiter, clock):
        result = await limiter.check(CLIENT, ROUTE)
        assert result.allowed is True
        assert result.remaining == 4

    async def test_remaining_counts_down(self, limiter, clock):
        seen = [(await limiter.check(CLIENT, ROUTE)).remaining for _ in range(5)]
        assert seen == [4, 3, 2, 1, 0]

    async def test_over_the_limit_is_rejected(self, limiter, clock):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

    async def test_rejected_requests_are_not_recorded(self, limiter, clock):
        """A rejected request must not consume a slot, otherwise a client that
        keeps retrying would extend its own lockout indefinitely."""
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)

        assert await limiter.entry_count(CLIENT, ROUTE) == 5


class TestSliding:
    async def test_slots_free_up_as_entries_age_out(self, limiter, clock):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

        # Move just past the window so the oldest entries expire.
        clock.advance(61)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True

    async def test_window_moves_continuously(self, limiter, clock):
        # One request every 20 seconds, well under 5 per 60s.
        for _ in range(10):
            assert (await limiter.check(CLIENT, ROUTE)).allowed is True
            clock.advance(20)

    async def test_partial_expiry_frees_exactly_what_aged_out(self, limiter, clock):
        # Three requests now, two more 30 seconds later.
        for _ in range(3):
            await limiter.check(CLIENT, ROUTE)
        clock.advance(30)
        for _ in range(2):
            await limiter.check(CLIENT, ROUTE)

        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

        # 31 more seconds: the first three have aged out, the later two have not.
        clock.advance(31)
        assert await limiter.entry_count(CLIENT, ROUTE) == 2

        results = [await limiter.check(CLIENT, ROUTE) for _ in range(3)]
        assert all(r.allowed for r in results)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False


class TestNoBoundaryProblem:
    async def test_burst_across_a_boundary_is_caught(self, redis_client):
        """The case fixed window gets wrong.

        Fixed window would allow 5 requests at the end of one calendar window
        and 5 more at the start of the next, so 10 within a couple of seconds
        against a 5 per 60s limit. Sliding window measures from now, so the
        second burst sees the first one and refuses.
        """
        sliding = SlidingWindowLimiter(redis_client, limit=5, window_seconds=60)
        clock = FrozenClock(start=1_000_059.0)
        sliding._now = clock

        for _ in range(5):
            assert (await sliding.check(CLIENT, ROUTE)).allowed is True

        # Two seconds later, which in fixed window terms is a new bucket.
        clock.advance(2)
        assert (await sliding.check(CLIENT, ROUTE)).allowed is False

    async def test_fixed_window_allows_what_sliding_window_blocks(self, redis_client):
        """Demonstrates the difference directly, so the ADR claim is backed by
        a test rather than an assertion in prose."""
        fixed = FixedWindowLimiter(redis_client, limit=5, window_seconds=60)

        allowed_in_first_bucket = 0
        for _ in range(5):
            if (await fixed.check(CLIENT, ROUTE)).allowed:
                allowed_in_first_bucket += 1

        # Simulate the calendar window rolling over.
        await redis_client.delete(fixed._key(CLIENT, ROUTE))

        allowed_in_second_bucket = 0
        for _ in range(5):
            if (await fixed.check(CLIENT, ROUTE)).allowed:
                allowed_in_second_bucket += 1

        # 10 requests got through a 5 per window limit.
        assert allowed_in_first_bucket + allowed_in_second_bucket == 10


class TestSameInstantRequests:
    async def test_requests_in_the_same_instant_all_count(self, limiter, clock):
        """Scoring by timestamp alone would let two requests in the same
        instant collide on one sorted set member, giving a free request."""
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)

        assert await limiter.entry_count(CLIENT, ROUTE) == 5
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False


class TestHeaders:
    async def test_retry_after_points_at_the_oldest_entry_expiring(self, limiter, clock):
        await limiter.check(CLIENT, ROUTE)
        clock.advance(10)
        for _ in range(4):
            await limiter.check(CLIENT, ROUTE)

        result = await limiter.check(CLIENT, ROUTE)
        # The oldest entry is 10s into a 60s window, so 50s until it frees up.
        assert result.retry_after == pytest.approx(50.0, abs=0.1)

    async def test_allowed_request_has_no_retry_after(self, limiter, clock):
        assert (await limiter.check(CLIENT, ROUTE)).retry_after is None


class TestIsolation:
    async def test_clients_are_independent(self, limiter, clock):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False
        assert (await limiter.check("user-2", ROUTE)).allowed is True

    async def test_routes_are_independent(self, limiter, clock):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, "/api/users")).allowed is True


class TestMemoryCost:
    async def test_entries_grow_with_request_count(self, limiter, clock):
        """This is the tradeoff that keeps it from being the default: state
        is proportional to traffic, not constant per client."""
        limiter._limit = 100
        for _ in range(50):
            await limiter.check(CLIENT, ROUTE)
        assert await limiter.stored_entry_count(CLIENT, ROUTE) == 50

    async def test_aged_out_entries_still_occupy_memory_until_swept(self, limiter, clock):
        """Pruning happens on the next request, not on a timer, so an idle
        client's stale entries sit in Redis until it comes back or the key
        expires. Worth knowing when reasoning about memory."""
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        clock.advance(61)

        assert await limiter.entry_count(CLIENT, ROUTE) == 0
        assert await limiter.stored_entry_count(CLIENT, ROUTE) == 5


class TestState:
    async def test_key_carries_a_ttl(self, limiter, clock, redis_client):
        await limiter.check(CLIENT, ROUTE)
        assert 0 < await redis_client.ttl(limiter._key(CLIENT, ROUTE)) <= 61

    async def test_reset_clears_the_log(self, limiter, clock):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        await limiter.reset(CLIENT, ROUTE)
        assert await limiter.stored_entry_count(CLIENT, ROUTE) == 0
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True


class TestRedisFailure:
    async def test_fails_open(self, limiter, clock):
        async def broken(*args, **kwargs):
            raise ConnectionError("redis is gone")

        limiter._script = broken
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True
