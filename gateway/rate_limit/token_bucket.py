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
# The clock is read inside the script with TIME rather than passed in.
#
# The original version called TIME from Python first and passed the result as
# an argument, on the reasoning that a script calling TIME is
# non-deterministic and so unsafe to replicate. That was true under Redis 4
# and earlier, which replicated the script itself to replicas and needed it to
# produce identical results there. Redis 5 replicates a script's effects
# instead, so a script may call TIME.
#
# It cost a full extra round trip per rate limit check. The benchmark made
# that plain: this limiter ran at roughly half the throughput of fixed window,
# which does its work in one call, and the gap was entirely the second trip.
LUA = """
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])

-- ARGV[3] is an explicit clock reading, or empty to use Redis's own. Tests
-- pass one so refill can be driven without sleeping; production leaves it
-- empty so the whole check is a single round trip.
local now
if ARGV[3] ~= '' then
    now = tonumber(ARGV[3])
else
    -- Redis TIME returns seconds and microseconds as separate strings.
    local clock = redis.call('TIME')
    now = tonumber(clock[1]) + (tonumber(clock[2]) / 1000000)
end

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

    def __init__(self, client, limit: int, window_seconds: int, clock=None) -> None:
        super().__init__(client, limit, window_seconds)
        # Tokens per second. A limit of 100 per 60s refills at ~1.67/s.
        self._refill_rate = limit / window_seconds
        # An awaitable returning a unix timestamp. Left as None in normal
        # operation so the script reads the clock itself in the same round
        # trip; supplied by tests that need to move time deliberately.
        self._clock = clock

    @property
    def refill_rate(self) -> float:
        return self._refill_rate

    @staticmethod
    def lua_script() -> str:
        return LUA

    async def check(self, identifier: str, route_prefix: str) -> RateLimitResult:
        key = self._key(identifier, route_prefix)
        now_arg = "" if self._clock is None else str(await self._clock())

        try:
            allowed, tokens, retry_after, reset_after = await self._script(
                keys=[key], args=[self._limit, self._refill_rate, now_arg]
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
        """Redis clock, as a separate call.

        The script reads the clock itself, so this is not on the hot path any
        more. It stays because the tests drive time through it, and because
        anything reasoning about a bucket outside a check still needs the same
        clock the script uses. Every gateway instance shares Redis, so this is
        the one clock they all agree on regardless of their own skew.
        """
        seconds, microseconds = await self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000
