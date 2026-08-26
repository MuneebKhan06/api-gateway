"""Sliding window log.

Stores a timestamp for every request in a sorted set. On each request the
script drops entries older than the window, counts what is left, and allows
the request if that count is under the limit.

This is the precise one. There is no boundary artifact, because the window is
genuinely the last N seconds measured from right now rather than a calendar
bucket. A client limited to 100 per minute can never send 101 requests in any
60 second span, whichever second you start measuring from.

The cost is memory. Fixed window stores one integer per client and token
bucket stores two numbers; this stores one sorted set entry per request in the
window. A client sending 1000 requests a minute holds 1000 entries. Multiply
by every active client and it is a real amount of Redis memory, which is the
reason it is not the default despite being the most accurate.
"""

import logging
import uuid

from gateway.rate_limit.base import BaseRateLimiter, RateLimitResult

logger = logging.getLogger(__name__)

# KEYS[1] sorted set of request timestamps
# ARGV[1] limit, ARGV[2] window seconds, ARGV[3] now, ARGV[4] unique member id
#
# The member id has to be unique per request. Scoring by timestamp alone means
# two requests in the same millisecond collide on the same member and the
# second silently overwrites the first, so the client gets a free request.
LUA = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local member = ARGV[4]

local cutoff = now - window

-- Drop everything that has aged out of the window.
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)

local count = redis.call('ZCARD', KEYS[1])

local allowed = 0
if count < limit then
    redis.call('ZADD', KEYS[1], now, member)
    count = count + 1
    allowed = 1
end

-- Keep the key alive only as long as the newest entry could matter.
redis.call('EXPIRE', KEYS[1], math.ceil(window) + 1)

local remaining = limit - count
if remaining < 0 then remaining = 0 end

-- When the oldest entry ages out, one slot frees up. That is the earliest a
-- rejected client could retry.
local retry_after = 0
if allowed == 0 then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    if oldest[2] then
        retry_after = (tonumber(oldest[2]) + window) - now
        if retry_after < 0 then retry_after = 0 end
    end
end

return {allowed, remaining, tostring(retry_after)}
"""


class SlidingWindowLimiter(BaseRateLimiter):
    name = "sliding_window"

    @staticmethod
    def lua_script() -> str:
        return LUA

    async def check(self, identifier: str, route_prefix: str) -> RateLimitResult:
        key = self._key(identifier, route_prefix)
        now = await self._now()
        # Unique per request, so two requests in the same instant cannot
        # collide on one sorted set member.
        member = f"{now}:{uuid.uuid4().hex[:12]}"

        try:
            allowed, remaining, retry_after = await self._script(
                keys=[key], args=[self._limit, self._window, now, member]
            )
        except Exception as exc:
            return self._allow_on_redis_failure(exc)

        return RateLimitResult(
            allowed=bool(allowed),
            limit=self._limit,
            remaining=int(remaining),
            reset_after=float(self._window),
            retry_after=float(retry_after) if not allowed else None,
        )

    async def entry_count(self, identifier: str, route_prefix: str) -> int:
        """How many requests are currently held for this client.

        Exposed because the memory cost of this algorithm is its defining
        tradeoff, and the benchmark script measures exactly this.
        """
        return await self._redis.zcard(self._key(identifier, route_prefix))

    async def _now(self) -> float:
        """Redis clock, not the local one, so multiple gateway instances agree."""
        seconds, microseconds = await self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000
