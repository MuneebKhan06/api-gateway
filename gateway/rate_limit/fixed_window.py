"""Fixed window counter.

The simplest of the three. Time is chopped into fixed buckets (00:00:00 to
00:00:59, then 00:01:00 to 00:01:59) and each client gets one counter per
bucket. Increment on every request, reject once the counter passes the limit,
let the key expire when the window ends.

It is cheap: one integer per client, one INCR per request.

It is also wrong at the boundary, and deliberately so, because knowing exactly
how it is wrong is the reason it is in here. A client with a 100 per minute
limit can send 100 requests at 00:00:59 and 100 more at 00:01:00. Both windows
are individually within limit, and the client has just sent 200 requests in
about a second. That burst is what token bucket and sliding window exist to
prevent.
"""

import logging
import time

from gateway.rate_limit.base import BaseRateLimiter, RateLimitResult

logger = logging.getLogger(__name__)

# KEYS[1] counter key
# ARGV[1] limit, ARGV[2] window seconds
#
# The TTL is set only when the counter is created (count == 1). Refreshing it
# on every increment would slide the window forward for an active client and
# the counter would never reset.
LUA = """
local count = redis.call('INCR', KEYS[1])
local ttl

if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    ttl = tonumber(ARGV[2])
else
    ttl = redis.call('TTL', KEYS[1])
    -- A key with no TTL would count forever. Should not happen, but a
    -- limiter that locks a client out permanently is worth guarding against.
    if ttl < 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[2])
        ttl = tonumber(ARGV[2])
    end
end

local limit = tonumber(ARGV[1])
local allowed = count <= limit and 1 or 0
local remaining = limit - count
if remaining < 0 then remaining = 0 end

return {allowed, remaining, ttl}
"""


class FixedWindowLimiter(BaseRateLimiter):
    name = "fixed_window"

    @staticmethod
    def lua_script() -> str:
        return LUA

    async def check(self, identifier: str, route_prefix: str) -> RateLimitResult:
        key = self._key(identifier, route_prefix)

        try:
            allowed, remaining, ttl = await self._script(
                keys=[key], args=[self._limit, self._window]
            )
        except Exception as exc:
            return self._allow_on_redis_failure(exc)

        reset_after = float(ttl)
        return RateLimitResult(
            allowed=bool(allowed),
            limit=self._limit,
            remaining=int(remaining),
            reset_after=reset_after,
            retry_after=None if allowed else reset_after,
        )

    def current_window_start(self, now: float | None = None) -> float:
        """Where the current bucket began. Useful when reasoning about the
        boundary problem in tests and in the benchmark script."""
        now = now if now is not None else time.time()
        return now - (now % self._window)
