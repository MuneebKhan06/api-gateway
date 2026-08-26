"""Token bucket.

Each client holds a bucket of tokens. A request costs one token. Tokens refill
continuously at limit/window per second, up to a ceiling of `limit`. If the
bucket is empty the request is rejected.

The refill is lazy, which is the trick that makes this cheap. Nothing runs on a
timer refilling every client's bucket. Instead each bucket stores the number of
tokens it had and the timestamp it had them, and on the next request the script
works out how much time has passed and credits the tokens that would have
accumulated. A client that goes quiet for an hour costs nothing while it is
quiet.

Why this is the default:

A fixed window says 100 requests per minute and means "100 per calendar
minute", which lets a client send 200 across a boundary. A token bucket says
100 per minute and means "a sustained rate of 100 per minute, and you may
spend up to 100 of them at once". A client that sends 10 requests in a second
and then waits is behaving reasonably, and this is the algorithm that agrees.

State is two fields per client regardless of traffic, so memory does not grow
with request rate the way the sliding window log's does.
"""

import logging

from gateway.rate_limit.base import BaseRateLimiter, RateLimitResult

logger = logging.getLogger(__name__)

# KEYS[1] bucket hash
# ARGV[1] capacity, ARGV[2] refill rate per second, ARGV[3] now (seconds)
#
# Stored as a hash of two fields: tokens remaining, and when they were counted.
#
# `now` is passed in rather than read from Redis TIME because a Lua script that
# calls TIME is non-deterministic, which historically made scripts unsafe to
# replicate. Passing the caller's clock keeps the script a pure function of its
# inputs.
LUA = """
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'updated_at')
local tokens = tonumber(bucket[1])
local updated_at = tonumber(bucket[2])

-- A client that has never been seen starts with a full bucket.
if tokens == nil or updated_at == nil then
    tokens = capacity
    updated_at = now
end

-- Credit the tokens that accrued since the last request, capped at capacity
-- so an idle client cannot bank an unlimited burst.
local elapsed = now - updated_at
if elapsed < 0 then
    -- Clock went backwards. Do not credit anything, but do not punish either.
    elapsed = 0
end

tokens = math.min(capacity, tokens + (elapsed * refill_rate))

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_at', now)

-- Expire an idle bucket after the time it would take to refill completely.
-- Any longer and Redis holds state that says nothing; any shorter and an
-- active client's bucket could vanish mid-window and hand back a full one.
local ttl = math.ceil(capacity / refill_rate) + 1
redis.call('EXPIRE', KEYS[1], ttl)

-- Seconds until one more token is available, for Retry-After.
local retry_after = 0
if allowed == 0 then
    retry_after = (1 - tokens) / refill_rate
end

-- Seconds until the bucket is full again.
local reset_after = (capacity - tokens) / refill_rate

return {allowed, tostring(tokens), tostring(retry_after), tostring(reset_after)}
"""


class TokenBucketLimiter(BaseRateLimiter):
    name = "token_bucket"

    def __init__(self, client, limit: int, window_seconds: int) -> None:
        super().__init__(client, limit, window_seconds)
        # Tokens per second. A limit of 100 per 60s refills at ~1.67/s.
        self._refill_rate = limit / window_seconds

    @property
    def refill_rate(self) -> float:
        return self._refill_rate

    @staticmethod
    def lua_script() -> str:
        return LUA

    async def check(self, identifier: str, route_prefix: str) -> RateLimitResult:
        key = self._key(identifier, route_prefix)
        now = await self._now()

        try:
            allowed, tokens, retry_after, reset_after = await self._script(
                keys=[key], args=[self._limit, self._refill_rate, now]
            )
        except Exception as exc:
            return self._allow_on_redis_failure(exc)

        return RateLimitResult(
            allowed=bool(allowed),
            limit=self._limit,
            # Partial tokens are floored: half a token does not buy a request.
            remaining=int(float(tokens)),
            reset_after=float(reset_after),
            retry_after=float(retry_after) if not allowed else None,
        )

    async def _now(self) -> float:
        """Read the clock from Redis, not from the gateway process.

        With several gateway instances sharing one Redis, using each process's
        own clock means their buckets disagree by whatever their clock skew
        happens to be. Redis is the one clock they all already share.
        """
        seconds, microseconds = await self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000
