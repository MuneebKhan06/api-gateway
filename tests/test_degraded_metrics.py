"""Degraded mode visibility.

Three components fail open when Redis is unreachable: the token blacklist, the
rate limiters, and breaker permission checks. Failing open is the right call,
but it means the gateway is enforcing less than it claims to, and that needs
to be visible as more than a log line.
"""

import fakeredis.aioredis
import pytest

from gateway.auth.blacklist import TokenBlacklist
from gateway.metrics.prometheus import REGISTRY
from gateway.rate_limit.fixed_window import FixedWindowLimiter


def sample(component: str, reason: str = "redis_unavailable") -> float:
    value = REGISTRY.get_sample_value(
        "gateway_degraded_operations_total", {"component": component, "reason": reason}
    )
    return 0.0 if value is None else value


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class BrokenRedis:
    async def exists(self, *args):
        raise ConnectionError("redis is gone")


class TestBlacklistDegradation:
    async def test_a_failed_lookup_is_counted(self, redis_client):
        blacklist = TokenBlacklist(redis_client)
        blacklist._redis = BrokenRedis()

        before = sample("token_blacklist")
        assert await blacklist.contains("jti-1") is False
        assert sample("token_blacklist") == before + 1

    async def test_healthy_lookups_are_not_counted(self, redis_client):
        blacklist = TokenBlacklist(redis_client)
        before = sample("token_blacklist")
        await blacklist.contains("jti-1")
        assert sample("token_blacklist") == before


class TestRateLimiterDegradation:
    async def test_a_failed_check_is_counted(self, redis_client):
        limiter = FixedWindowLimiter(redis_client, limit=5, window_seconds=60)

        async def broken(*args, **kwargs):
            raise ConnectionError("redis is gone")

        limiter._script = broken

        before = sample("rate_limiter_fixed_window")
        result = await limiter.check("user-1", "/api/orders")
        assert result.allowed is True
        assert sample("rate_limiter_fixed_window") == before + 1

    async def test_algorithms_are_counted_separately(self, redis_client):
        """Knowing which limiter is degraded matters, since routes use
        different algorithms."""
        limiter = FixedWindowLimiter(redis_client, limit=5, window_seconds=60)

        async def broken(*args, **kwargs):
            raise ConnectionError("redis is gone")

        limiter._script = broken
        await limiter.check("user-1", "/api/orders")

        assert sample("rate_limiter_fixed_window") > 0
        assert sample("rate_limiter_token_bucket") == 0

    async def test_healthy_checks_are_not_counted(self, redis_client):
        limiter = FixedWindowLimiter(redis_client, limit=5, window_seconds=60)
        before = sample("rate_limiter_fixed_window")
        await limiter.check("user-1", "/api/orders")
        assert sample("rate_limiter_fixed_window") == before


class TestExposedInScrape:
    def test_degraded_metric_appears_in_the_scrape(self, gateway):
        body = gateway.get("/metrics").text
        assert "gateway_degraded_operations_total" in body
