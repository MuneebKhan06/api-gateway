"""Redis connection handling.

Redis backs three separate things in this gateway: the token blacklist, the
rate limit counters, and the circuit breaker state. They all share one
connection pool, created at startup and closed at shutdown.
"""

import logging

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self, url: str, max_connections: int = 50) -> None:
        self._url = url
        self._max_connections = max_connections
        self._client: redis.Redis | None = None

    async def startup(self) -> None:
        self._client = redis.from_url(
            self._url,
            max_connections=self._max_connections,
            # Everything stored here is text, so decoding centrally saves a
            # .decode() at every call site.
            decode_responses=True,
        )
        logger.info("Redis client ready (max_connections=%d)", self._max_connections)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Redis client closed")

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            raise RuntimeError("RedisClient.startup() was never awaited")
        return self._client

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception as exc:
            logger.warning("Redis ping failed: %s", exc)
            return False
