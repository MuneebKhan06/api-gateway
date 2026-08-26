import fakeredis.aioredis
import pytest

from gateway.rate_limit.factory import (
    RateLimiterRegistry,
    UnknownAlgorithm,
    available_algorithms,
    build_limiter,
)
from gateway.rate_limit.fixed_window import FixedWindowLimiter
from gateway.rate_limit.sliding_window import SlidingWindowLimiter
from gateway.rate_limit.token_bucket import TokenBucketLimiter
from gateway.schemas.gateway import RateLimitConfig


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def config(algorithm="token_bucket", requests=100, window_seconds=60) -> RateLimitConfig:
    return RateLimitConfig(
        algorithm=algorithm, requests=requests, window_seconds=window_seconds
    )


class TestBuild:
    @pytest.mark.parametrize(
        "algorithm,expected",
        [
            ("fixed_window", FixedWindowLimiter),
            ("token_bucket", TokenBucketLimiter),
            ("sliding_window", SlidingWindowLimiter),
        ],
    )
    def test_each_algorithm_builds_its_limiter(self, redis_client, algorithm, expected):
        assert isinstance(build_limiter(redis_client, config(algorithm)), expected)

    def test_limits_are_passed_through(self, redis_client):
        limiter = build_limiter(redis_client, config(requests=42, window_seconds=7))
        assert limiter.limit == 42
        assert limiter.window_seconds == 7

    def test_every_declared_algorithm_is_buildable(self, redis_client):
        """The schema's Literal and the factory's table must not drift apart."""
        for algorithm in available_algorithms():
            assert build_limiter(redis_client, config(algorithm)) is not None

    def test_unknown_algorithm_is_rejected(self, redis_client):
        bad = config()
        # Bypass the schema's own validation to reach the factory's guard.
        object.__setattr__(bad, "algorithm", "magic")
        with pytest.raises(UnknownAlgorithm):
            build_limiter(redis_client, bad)

    def test_schema_rejects_an_unknown_algorithm_up_front(self):
        with pytest.raises(ValueError):
            RateLimitConfig(algorithm="magic", requests=10, window_seconds=60)


class TestRegistry:
    def test_same_config_returns_the_same_instance(self, redis_client):
        registry = RateLimiterRegistry(redis_client)
        first = registry.get(config())
        second = registry.get(config())
        assert first is second
        assert len(registry) == 1

    def test_different_configs_get_different_instances(self, redis_client):
        registry = RateLimiterRegistry(redis_client)
        registry.get(config(requests=100))
        registry.get(config(requests=50))
        assert len(registry) == 2

    def test_algorithm_change_gets_a_new_instance(self, redis_client):
        registry = RateLimiterRegistry(redis_client)
        bucket = registry.get(config("token_bucket"))
        window = registry.get(config("fixed_window"))
        assert bucket is not window

    async def test_shared_instance_still_keeps_routes_separate(self, redis_client):
        """Two routes with identical limits reuse one limiter, so this checks
        their counters stay independent."""
        registry = RateLimiterRegistry(redis_client)
        limiter = registry.get(config(requests=2, window_seconds=60))

        for _ in range(2):
            await limiter.check("user-1", "/api/orders")
        assert (await limiter.check("user-1", "/api/orders")).allowed is False
        assert (await limiter.check("user-1", "/api/users")).allowed is True

    def test_clear_drops_cached_limiters(self, redis_client):
        registry = RateLimiterRegistry(redis_client)
        registry.get(config())
        registry.clear()
        assert len(registry) == 0

    def test_rebuild_after_clear_reflects_new_limits(self, redis_client):
        """A route table reload that changes a limit must not keep serving the
        old one out of the cache."""
        registry = RateLimiterRegistry(redis_client)
        assert registry.get(config(requests=100)).limit == 100

        registry.clear()
        assert registry.get(config(requests=5)).limit == 5
