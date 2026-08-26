"""Algorithm selection.

Routes name their algorithm in routes.yaml. This turns that name into a
limiter instance, and caches the result so a route does not build a new
limiter, and re-register its Lua script, on every request.
"""

import logging

import redis.asyncio as redis

from gateway.rate_limit.base import BaseRateLimiter
from gateway.rate_limit.fixed_window import FixedWindowLimiter
from gateway.rate_limit.sliding_window import SlidingWindowLimiter
from gateway.rate_limit.token_bucket import TokenBucketLimiter
from gateway.schemas.gateway import RateLimitConfig

logger = logging.getLogger(__name__)

LIMITERS: dict[str, type[BaseRateLimiter]] = {
    FixedWindowLimiter.name: FixedWindowLimiter,
    TokenBucketLimiter.name: TokenBucketLimiter,
    SlidingWindowLimiter.name: SlidingWindowLimiter,
}


class UnknownAlgorithm(ValueError):
    pass


def available_algorithms() -> list[str]:
    return sorted(LIMITERS)


def build_limiter(
    client: redis.Redis, config: RateLimitConfig
) -> BaseRateLimiter:
    """Construct one limiter from a route's rate_limit block."""
    limiter_class = LIMITERS.get(config.algorithm)
    if limiter_class is None:
        raise UnknownAlgorithm(
            f"unknown rate limit algorithm {config.algorithm!r}, "
            f"expected one of {', '.join(available_algorithms())}"
        )
    return limiter_class(client, limit=config.requests, window_seconds=config.window_seconds)


class RateLimiterRegistry:
    """One limiter per distinct route configuration, built once and reused.

    Keyed on the configuration rather than the route path, so two routes with
    identical limits share an instance. They still have separate counters,
    because the client key includes the route prefix.
    """

    def __init__(self, client: redis.Redis) -> None:
        self._redis = client
        self._limiters: dict[tuple[str, int, int], BaseRateLimiter] = {}

    def get(self, config: RateLimitConfig) -> BaseRateLimiter:
        key = (config.algorithm, config.requests, config.window_seconds)

        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = build_limiter(self._redis, config)
            self._limiters[key] = limiter
            logger.info(
                "Built %s limiter (%d per %ds)",
                config.algorithm,
                config.requests,
                config.window_seconds,
            )
        return limiter

    def clear(self) -> None:
        """Drop cached limiters. Called after a route table reload, so a
        changed limit does not keep serving the old one."""
        self._limiters.clear()

    def __len__(self) -> int:
        return len(self._limiters)
