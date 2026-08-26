"""Rate limiter interface and shared pieces.

Three algorithms live in this package and they all answer the same question:
given a client key, is this request allowed right now? They differ in how they
count, not in what they return, so they share one interface and one result
type.

Every implementation runs its counting logic as a Redis Lua script. This is
not decoration. A limiter written as GET, decide, then SET is broken under
concurrency: two requests arriving together both read the same counter, both
conclude they are under the limit, and both proceed. Redis executes a Lua
script atomically, so the read, the decision and the write cannot interleave
with another request's.
"""

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import redis.asyncio as redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "ratelimit"


@dataclass(frozen=True)
class RateLimitResult:
    """The verdict on one request.

    `remaining` and `reset_after` are here because they go straight into the
    X-RateLimit response headers. A limiter that only says yes or no forces
    clients to discover their limit by hitting it.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_after: float
    retry_after: float | None = None

    @property
    def rejected(self) -> bool:
        return not self.allowed


def client_key(identifier: str, route_prefix: str, algorithm: str) -> str:
    """Build the Redis key for one client on one route.

    The route prefix is part of the key so a client's budget on /api/orders is
    separate from its budget on /api/users. The algorithm name is in there too,
    so switching algorithms on a route does not inherit counters written in a
    format the new algorithm cannot read.

    The identifier is hashed rather than embedded. It can be an email address
    from a JWT, and putting user identifiers in plain text into Redis keys
    means they show up in every SCAN, every slow log and every memory dump.
    """
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]
    return f"{KEY_PREFIX}:{algorithm}:{route_prefix}:{digest}"


class BaseRateLimiter(ABC):
    """Common surface for every algorithm.

    Subclasses register their Lua script once at construction. redis-py's
    `register_script` sends the body on first use and then addresses it by SHA
    on every later call, so the script is not re-uploaded per request.
    """

    #: Overridden by each subclass, used in the Redis key and in metrics.
    name: str = "base"

    def __init__(self, client: redis.Redis, limit: int, window_seconds: int) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self._redis = client
        self._limit = limit
        self._window = window_seconds
        self._script = self._redis.register_script(self.lua_script())

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return self._window

    @staticmethod
    @abstractmethod
    def lua_script() -> str:
        """The atomic counting logic, executed server side by Redis."""

    @abstractmethod
    async def check(self, identifier: str, route_prefix: str) -> RateLimitResult:
        """Consume one request's worth of budget and report the verdict."""

    async def reset(self, identifier: str, route_prefix: str) -> None:
        """Clear a client's counters. Used by tests and by support tooling."""
        await self._redis.delete(client_key(identifier, route_prefix, self.name))

    def _key(self, identifier: str, route_prefix: str) -> str:
        return client_key(identifier, route_prefix, self.name)

    def _allow_on_redis_failure(self, exc: Exception) -> RateLimitResult:
        """Fail open when Redis is unreachable.

        Same reasoning as the token blacklist: rejecting every request because
        the counter store is down turns a Redis outage into a total gateway
        outage. An unmetered window is the lesser failure.
        """
        logger.error(
            "Rate limiter %s could not reach Redis, allowing request: %s", self.name, exc
        )
        return RateLimitResult(
            allowed=True, limit=self._limit, remaining=self._limit, reset_after=0.0
        )
