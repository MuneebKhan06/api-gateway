"""Redis-backed breaker state.

State lives in Redis rather than in the process for one reason: there is more
than one gateway instance. With in-memory state, three instances each keep
their own view, so a dead upstream has to fail five times per instance before
anyone stops calling it, and an instance that restarts forgets everything it
learned. Shared state means one instance discovering the outage protects all
of them.

Every transition runs as a Lua script. The same concurrency argument as the
rate limiters applies, and it bites harder here: two requests failing at once
would both read four failures, both write five, and the breaker would open on
what is really the sixth failure. Worse, in HALF_OPEN, a read-then-write would
let several requests all believe they are the single trial request, which is
exactly what HALF_OPEN exists to prevent.
"""

import logging

import redis.asyncio as redis

from gateway.circuit_breaker.breaker import (
    BreakerConfig,
    BreakerSnapshot,
    BreakerState,
)

logger = logging.getLogger(__name__)

KEY_PREFIX = "breaker"

# Shared preamble: load a breaker hash into locals, resolving an expired OPEN
# into HALF_OPEN the same way effective_state() does in Python.
_LOAD = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local failure_threshold = tonumber(ARGV[2])
local failure_window = tonumber(ARGV[3])
local recovery_timeout = tonumber(ARGV[4])
local success_threshold = tonumber(ARGV[5])

local raw = redis.call('HMGET', key, 'state', 'failures', 'successes',
                       'opened_at', 'first_failure_at')
local state = raw[1] or 'closed'
local failures = tonumber(raw[2]) or 0
local successes = tonumber(raw[3]) or 0
local opened_at = tonumber(raw[4])
local first_failure_at = tonumber(raw[5])

if state == 'open' and opened_at ~= nil and (now - opened_at) >= recovery_timeout then
    state = 'half_open'
end
"""

_SAVE = """
local function save(s, f, su, oa, ffa)
    redis.call('HSET', key,
        'state', s,
        'failures', f,
        'successes', su,
        'opened_at', oa or '',
        'first_failure_at', ffa or '')
    -- Expire idle breakers. A service nobody calls should not hold state
    -- forever, and a closed breaker with no counters says nothing anyway.
    redis.call('EXPIRE', key, math.ceil(math.max(failure_window, recovery_timeout) * 10))
    return {s, f, su, tostring(oa or ''), tostring(ffa or '')}
end
"""

# Read current state without changing it.
READ_LUA = _LOAD + """
return {state, failures, successes, tostring(opened_at or ''), tostring(first_failure_at or '')}
"""

# Ask permission to make a request.
#
# In HALF_OPEN this must admit exactly one caller. It does that by writing the
# state back as 'half_open_pending' for the trial's duration, so a second
# request arriving in the same instant sees pending and is refused. Without
# this, every request that arrives the moment the recovery timeout lapses
# would be let through at once, which is a thundering herd aimed at a service
# that just proved it was unwell.
ALLOW_LUA = _LOAD + _SAVE + """
if state == 'closed' then
    return {1, 'closed'}
end

if state == 'open' then
    return {0, 'open'}
end

if state == 'half_open' then
    save('half_open_pending', failures, successes, opened_at, first_failure_at)
    return {1, 'half_open'}
end

-- half_open_pending: a trial is already in flight.
if state == 'half_open_pending' then
    -- Guard against a trial that never reported back (process died mid
    -- request). After the recovery timeout again, allow a fresh trial.
    if opened_at ~= nil and (now - opened_at) >= (recovery_timeout * 2) then
        save('half_open_pending', failures, successes, now, first_failure_at)
        return {1, 'half_open'}
    end
    return {0, 'open'}
end

return {1, state}
"""

RECORD_SUCCESS_LUA = _LOAD + _SAVE + """
if state == 'half_open' or state == 'half_open_pending' then
    local next_successes = successes + 1
    if next_successes >= success_threshold then
        redis.call('DEL', key)
        return {'closed', 0, 0}
    end
    save('half_open', 0, next_successes, opened_at, first_failure_at)
    return {'half_open', 0, next_successes}
end

-- A success in CLOSED clears any accumulated failures.
redis.call('DEL', key)
return {'closed', 0, 0}
"""

RECORD_FAILURE_LUA = _LOAD + _SAVE + """
if state == 'half_open' or state == 'half_open_pending' then
    -- The trial failed, so reopen and restart the recovery timer from now.
    save('open', 0, 0, now, '')
    return {'open', 0, 0}
end

if state == 'open' then
    return {'open', failures, successes}
end

-- CLOSED: count within the window.
local window_start = first_failure_at
local next_failures
if window_start == nil or (now - window_start) > failure_window then
    next_failures = 1
    window_start = now
else
    next_failures = failures + 1
end

if next_failures >= failure_threshold then
    save('open', 0, 0, now, '')
    return {'open', 0, 0}
end

save('closed', next_failures, 0, '', window_start)
return {'closed', next_failures, 0}
"""


def _to_float(value) -> float | None:
    if value in (None, "", b""):
        return None
    return float(value)


class BreakerStore:
    """Reads and writes one breaker's state in Redis."""

    def __init__(self, client: redis.Redis, config: BreakerConfig) -> None:
        self._redis = client
        self._config = config
        self._read = client.register_script(READ_LUA)
        self._allow = client.register_script(ALLOW_LUA)
        self._record_success = client.register_script(RECORD_SUCCESS_LUA)
        self._record_failure = client.register_script(RECORD_FAILURE_LUA)

    @property
    def config(self) -> BreakerConfig:
        return self._config

    @staticmethod
    def key(upstream: str) -> str:
        return f"{KEY_PREFIX}:{upstream}"

    def _args(self, now: float) -> list:
        return [
            now,
            self._config.failure_threshold,
            self._config.failure_window_seconds,
            self._config.recovery_timeout_seconds,
            self._config.success_threshold,
        ]

    async def now(self) -> float:
        """Redis clock, so every gateway instance agrees on elapsed time."""
        seconds, microseconds = await self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000

    async def snapshot(self, upstream: str) -> BreakerSnapshot:
        now = await self.now()
        state, failures, successes, opened_at, first_failure_at = await self._read(
            keys=[self.key(upstream)], args=self._args(now)
        )
        return BreakerSnapshot(
            state=BreakerState(_normalise(state)),
            failures=int(failures),
            successes=int(successes),
            opened_at=_to_float(opened_at),
            first_failure_at=_to_float(first_failure_at),
        )

    async def allow_request(self, upstream: str) -> tuple[bool, BreakerState]:
        """Atomically decide whether to attempt a call, and claim the trial
        slot if this is the one request HALF_OPEN permits."""
        now = await self.now()
        allowed, state = await self._allow(keys=[self.key(upstream)], args=self._args(now))
        return bool(allowed), BreakerState(_normalise(state))

    async def record_success(self, upstream: str) -> BreakerState:
        now = await self.now()
        state, _failures, _successes = await self._record_success(
            keys=[self.key(upstream)], args=self._args(now)
        )
        return BreakerState(_normalise(state))

    async def record_failure(self, upstream: str) -> BreakerState:
        now = await self.now()
        state, _failures, _successes = await self._record_failure(
            keys=[self.key(upstream)], args=self._args(now)
        )
        return BreakerState(_normalise(state))

    async def reset(self, upstream: str) -> None:
        """Force a breaker closed. For operator intervention when an upstream
        is known to be healthy and waiting out the timeout is not wanted."""
        await self._redis.delete(self.key(upstream))
        logger.info("Circuit breaker for %s reset to closed", upstream)

    async def trip(self, upstream: str) -> None:
        """Force a breaker open, for taking an upstream out of rotation."""
        now = await self.now()
        await self._redis.hset(
            self.key(upstream),
            mapping={
                "state": BreakerState.OPEN.value,
                "failures": 0,
                "successes": 0,
                "opened_at": now,
                "first_failure_at": "",
            },
        )
        logger.warning("Circuit breaker for %s tripped open manually", upstream)


def _normalise(state) -> str:
    """`half_open_pending` is an internal bookkeeping state, not one of the
    three the state machine talks about. Callers see it as half_open."""
    value = state.decode() if isinstance(state, bytes) else str(state)
    return "half_open" if value == "half_open_pending" else value
