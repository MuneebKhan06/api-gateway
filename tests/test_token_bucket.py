"""Token bucket tests.

Refill is driven by a clock the limiter reads from Redis, so these tests move
that clock rather than sleeping.
"""

import fakeredis.aioredis
import pytest

from gateway.rate_limit.token_bucket import TokenBucketLimiter

ROUTE = "/api/orders"
CLIENT = "user-1"


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def clock():
    """Explicit clock so refill can be driven without sleeping."""
    return FrozenClock()


@pytest.fixture
def limiter(redis_client, clock):
    # 10 requests per 10 seconds, so it refills at exactly 1 token per second.
    return TokenBucketLimiter(redis_client, limit=10, window_seconds=10, clock=clock)


class FrozenClock:
    """Stands in for the Redis clock so time can be moved deliberately."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    async def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestBurst:
    async def test_new_client_starts_with_a_full_bucket(self, limiter, clock):
        result = await limiter.check(CLIENT, ROUTE)
        assert result.allowed is True
        assert result.remaining == 9

    async def test_a_full_bucket_can_be_spent_at_once(self, limiter, clock):
        """The point of this algorithm: a legitimate short burst is fine."""
        results = [await limiter.check(CLIENT, ROUTE) for _ in range(10)]
        assert all(r.allowed for r in results)

    async def test_the_request_after_the_burst_is_rejected(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)

        result = await limiter.check(CLIENT, ROUTE)
        assert result.allowed is False
        assert result.remaining == 0

    async def test_time_does_not_pass_on_its_own(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)
        # Clock has not moved, so nothing has refilled.
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False


class TestRefill:
    async def test_one_second_buys_one_token(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

        clock.advance(1)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True

    async def test_partial_tokens_do_not_buy_a_request(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)

        clock.advance(0.5)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

    async def test_refill_is_capped_at_capacity(self, limiter, clock):
        """An idle client must not bank an unlimited burst."""
        await limiter.check(CLIENT, ROUTE)
        clock.advance(3600)

        results = [await limiter.check(CLIENT, ROUTE) for _ in range(10)]
        assert all(r.allowed for r in results)
        # Capacity is 10, so the eleventh fails despite an hour of idling.
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

    async def test_sustained_rate_is_enforced(self, limiter, clock):
        """Spend the bucket, then send one request per second forever. Each
        should be allowed, because that is exactly the sustained rate."""
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)

        for _ in range(20):
            clock.advance(1)
            assert (await limiter.check(CLIENT, ROUTE)).allowed is True

    async def test_faster_than_sustained_rate_is_throttled(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)

        allowed = 0
        for _ in range(20):
            clock.advance(0.5)  # twice the permitted rate
            if (await limiter.check(CLIENT, ROUTE)).allowed:
                allowed += 1
        # Roughly half get through, which is the rate being enforced.
        assert 8 <= allowed <= 12


class TestClockSafety:
    async def test_clock_going_backwards_does_not_grant_tokens(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)

        clock.advance(-60)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False


class TestHeaders:
    async def test_rejected_request_reports_when_to_retry(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)

        result = await limiter.check(CLIENT, ROUTE)
        # One token per second, so about a second until the next one.
        assert result.retry_after == pytest.approx(1.0, abs=0.05)

    async def test_allowed_request_has_no_retry_after(self, limiter, clock):
        assert (await limiter.check(CLIENT, ROUTE)).retry_after is None

    async def test_reset_after_is_time_to_a_full_bucket(self, limiter, clock):
        await limiter.check(CLIENT, ROUTE)
        result = await limiter.check(CLIENT, ROUTE)
        # Two tokens spent at one per second.
        assert result.reset_after == pytest.approx(2.0, abs=0.05)


class TestIsolation:
    async def test_clients_have_separate_buckets(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False
        assert (await limiter.check("user-2", ROUTE)).allowed is True

    async def test_routes_have_separate_buckets(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, "/api/users")).allowed is True


class TestState:
    async def test_bucket_carries_a_ttl(self, limiter, clock, redis_client):
        await limiter.check(CLIENT, ROUTE)
        assert await redis_client.ttl(limiter._key(CLIENT, ROUTE)) > 0

    async def test_refill_rate_is_derived_from_the_limit(self, redis_client):
        limiter = TokenBucketLimiter(redis_client, limit=100, window_seconds=60)
        assert limiter.refill_rate == pytest.approx(100 / 60)

    async def test_without_an_injected_clock_the_script_reads_its_own(self, redis_client):
        """Production path: no clock passed, so the whole check is one round
        trip and Redis supplies the time."""
        limiter = TokenBucketLimiter(redis_client, limit=3, window_seconds=60)
        assert limiter._clock is None

        verdicts = [(await limiter.check(CLIENT, ROUTE)).allowed for _ in range(4)]
        assert verdicts == [True, True, True, False]

    async def test_reset_gives_back_a_full_bucket(self, limiter, clock):
        for _ in range(10):
            await limiter.check(CLIENT, ROUTE)
        await limiter.reset(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).remaining == 9


class TestRedisFailure:
    async def test_fails_open(self, limiter, clock):
        async def broken(*args, **kwargs):
            raise ConnectionError("redis is gone")

        limiter._script = broken
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True
