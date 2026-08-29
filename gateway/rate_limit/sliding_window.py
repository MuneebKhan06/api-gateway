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
# ARGV[1] limit, ARGV[2] window seconds, ARGV[3] clock override, ARGV[4] member
#
# The member id has to be unique per request. Scoring by timestamp alone means
# two requests in the same millisecond collide on the same member and the
# second silently overwrites the first, so the client gets a free request.
#
# The clock is read here rather than passed in, for the same reason as the
# token bucket: fetching TIME from Python first cost an extra round trip per
# check, and Redis 5 replicates a script's effects rather than the script, so
# calling TIME inside one is safe. ARGV[3] overrides it for tests.
LUA = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member = ARGV[4]

local now
if ARGV[3] ~= '' then
    now = tonumber(ARGV[3])
else
    local clock = redis.call('TIME')
    now = tonumber(clock[1]) + (tonumber(clock[2]) / 1000000)
end

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

    def __init__(self, client, limit: int, window_seconds: int, clock=None) -> None:
        super().__init__(client, limit, window_seconds)
        # See the token bucket: None means the script reads the clock itself,
        # keeping the check to a single round trip.
        self._clock = clock

    @staticmethod
    def lua_script() -> str:
        return LUA

    async def check(self, identifier: str, route_prefix: str) -> RateLimitResult:
        key = self._key(identifier, route_prefix)
        now_arg = "" if self._clock is None else str(await self._clock())
        # Unique per request, so two requests in the same instant cannot
        # collide on one sorted set member. A random suffix is enough on its
        # own, so this no longer needs the timestamp the script now owns.
        member = uuid.uuid4().hex

        try:
            allowed, remaining, retry_after = await self._script(
                keys=[key], args=[self._limit, self._window, now_arg, member]
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
        """Requests inside the window right now, which is what counts against
        the limit.

        Pruning only happens inside the script, so a plain ZCARD would include
        entries that have aged out but not yet been swept. This counts by score
        instead, which gives the same answer the next check would.
        """
        now = await self._now()
        return await self._redis.zcount(
            self._key(identifier, route_prefix), now - self._window, "+inf"
        )

    async def stored_entry_count(self, identifier: str, route_prefix: str) -> int:
        """Entries actually held in Redis, aged out ones included.

        This is the number that matters for memory, since an entry occupies
        space until something sweeps it. The benchmark measures this one.
        """
        return await self._redis.zcard(self._key(identifier, route_prefix))

    async def _now(self) -> float:
        """Redis clock, for callers reasoning about the window outside a check.

        Not on the hot path any more; the script reads the clock itself.
        """
        if self._clock is not None:
            return await self._clock()
        seconds, microseconds = await self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000
