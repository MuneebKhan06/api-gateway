"""Fixed window tests against fakeredis.

fakeredis executes Lua scripts, so these run the same script Redis would.
"""

import fakeredis.aioredis
import pytest

from gateway.rate_limit.fixed_window import FixedWindowLimiter

ROUTE = "/api/orders"
CLIENT = "user-1"


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def limiter(redis_client):
    return FixedWindowLimiter(redis_client, limit=5, window_seconds=60)


class TestConstruction:
    def test_rejects_a_zero_limit(self, redis_client):
        with pytest.raises(ValueError):
            FixedWindowLimiter(redis_client, limit=0, window_seconds=60)

    def test_rejects_a_negative_window(self, redis_client):
        with pytest.raises(ValueError):
            FixedWindowLimiter(redis_client, limit=5, window_seconds=-1)


class TestCounting:
    async def test_first_request_is_allowed(self, limiter):
        result = await limiter.check(CLIENT, ROUTE)
        assert result.allowed is True
        assert result.limit == 5
        assert result.remaining == 4

    async def test_remaining_counts_down(self, limiter):
        seen = [(await limiter.check(CLIENT, ROUTE)).remaining for _ in range(5)]
        assert seen == [4, 3, 2, 1, 0]

    async def test_request_over_the_limit_is_rejected(self, limiter):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)

        result = await limiter.check(CLIENT, ROUTE)
        assert result.allowed is False
        assert result.remaining == 0

    async def test_remaining_never_goes_negative(self, limiter):
        for _ in range(20):
            result = await limiter.check(CLIENT, ROUTE)
        assert result.remaining == 0

    async def test_exactly_the_limit_is_allowed(self, limiter):
        results = [await limiter.check(CLIENT, ROUTE) for _ in range(5)]
        assert all(r.allowed for r in results)


class TestIsolation:
    async def test_clients_do_not_share_a_budget(self, limiter):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

        # A different client starts fresh.
        assert (await limiter.check("user-2", ROUTE)).allowed is True

    async def test_routes_do_not_share_a_budget(self, limiter):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False
        assert (await limiter.check(CLIENT, "/api/users")).allowed is True


class TestExpiry:
    async def test_counter_carries_a_ttl(self, limiter, redis_client):
        await limiter.check(CLIENT, ROUTE)
        key = limiter._key(CLIENT, ROUTE)
        assert 0 < await redis_client.ttl(key) <= 60

    async def test_ttl_is_not_refreshed_by_later_requests(self, limiter, redis_client):
        """The window must not slide forward for an active client, or the
        counter would never reset and the client would be locked out."""
        await limiter.check(CLIENT, ROUTE)
        key = limiter._key(CLIENT, ROUTE)
        await redis_client.expire(key, 10)

        await limiter.check(CLIENT, ROUTE)
        assert await redis_client.ttl(key) <= 10

    async def test_window_expiry_restores_the_budget(self, limiter, redis_client):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

        # Expire the window the way Redis eventually would.
        await redis_client.delete(limiter._key(CLIENT, ROUTE))
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True

    async def test_a_counter_without_a_ttl_is_repaired(self, limiter, redis_client):
        """A key that lost its TTL would count forever and lock the client out
        permanently, so the script re-applies one."""
        await limiter.check(CLIENT, ROUTE)
        key = limiter._key(CLIENT, ROUTE)
        await redis_client.persist(key)
        assert await redis_client.ttl(key) == -1

        await limiter.check(CLIENT, ROUTE)
        assert await redis_client.ttl(key) > 0


class TestRetryAfter:
    async def test_allowed_requests_have_no_retry_after(self, limiter):
        assert (await limiter.check(CLIENT, ROUTE)).retry_after is None

    async def test_rejected_requests_say_when_to_retry(self, limiter):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)

        result = await limiter.check(CLIENT, ROUTE)
        assert result.retry_after is not None
        assert 0 < result.retry_after <= 60


class TestReset:
    async def test_reset_clears_the_counter(self, limiter):
        for _ in range(5):
            await limiter.check(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

        await limiter.reset(CLIENT, ROUTE)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True


class TestRedisFailure:
    async def test_fails_open(self, redis_client):
        """A Redis outage must not reject every request."""
        limiter = FixedWindowLimiter(redis_client, limit=5, window_seconds=60)

        async def broken(*args, **kwargs):
            raise ConnectionError("redis is gone")

        limiter._script = broken
        result = await limiter.check(CLIENT, ROUTE)
        assert result.allowed is True
