"""Integration tests against real Redis and real Postgres.

The rest of the suite runs against fakeredis and SQLite, which is fast and
covers the logic. It cannot cover the places where a fake and the real server
quietly disagree, and there are two that matter here:

- Lua. Every rate limiter and every breaker transition is a Redis script.
  fakeredis executes Lua through lupa, which is close but not the same
  interpreter Redis embeds.
- Postgres types and constraints. SQLite has no real timezone type and a much
  looser idea of what a unique index is.

These are marked `integration` and skipped when the services are not running,
so the default `pytest` still works on a laptop with nothing installed.

    docker compose -f docker-compose.test.yml up -d
    pytest tests/ -m integration
"""

import os
from datetime import timedelta

import pytest
import redis.asyncio as redis
from sqlalchemy.exc import IntegrityError

from gateway.auth.blacklist import TokenBlacklist
from gateway.circuit_breaker.breaker import BreakerConfig, BreakerState
from gateway.circuit_breaker.store import BreakerStore
from gateway.db.connection import Database
from gateway.db.models import utcnow
from gateway.db.repository import RefreshTokenRepository, UserRepository
from gateway.rate_limit.fixed_window import FixedWindowLimiter
from gateway.rate_limit.sliding_window import SlidingWindowLimiter
from gateway.rate_limit.token_bucket import TokenBucketLimiter

pytestmark = pytest.mark.integration

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6399/0")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5439/gateway"
)

ROUTE = "/api/orders"
CLIENT = "integration-client"


@pytest.fixture
async def real_redis():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.skip(f"Redis is not reachable at {REDIS_URL}: {exc}")

    # Start from nothing so a previous run cannot change the answer.
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def real_database():
    database = Database(DATABASE_URL)
    await database.startup()
    try:
        if not await database.ping():
            pytest.skip(f"Postgres is not reachable at {DATABASE_URL}")
        await database.create_all()
    except Exception as exc:
        await database.shutdown()
        pytest.skip(f"Postgres is not usable: {exc}")

    yield database

    from sqlalchemy import text

    async with database.session() as session:
        await session.execute(text("TRUNCATE users, refresh_tokens, audit_log CASCADE"))
    await database.shutdown()


class TestLuaOnRealRedis:
    """The scripts run on the interpreter Redis actually embeds."""

    async def test_fixed_window_enforces_its_limit(self, real_redis):
        limiter = FixedWindowLimiter(real_redis, limit=5, window_seconds=60)
        verdicts = [(await limiter.check(CLIENT, ROUTE)).allowed for _ in range(7)]
        assert verdicts == [True, True, True, True, True, False, False]

    async def test_fixed_window_sets_a_real_ttl(self, real_redis):
        limiter = FixedWindowLimiter(real_redis, limit=5, window_seconds=60)
        await limiter.check(CLIENT, ROUTE)
        assert 0 < await real_redis.ttl(limiter._key(CLIENT, ROUTE)) <= 60

    async def test_token_bucket_allows_a_burst_then_throttles(self, real_redis):
        limiter = TokenBucketLimiter(real_redis, limit=5, window_seconds=60)
        burst = [(await limiter.check(CLIENT, ROUTE)).allowed for _ in range(5)]
        assert all(burst)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

    async def test_token_bucket_refills_against_the_redis_clock(self, real_redis):
        # One token per second, so a short real sleep buys exactly one.
        limiter = TokenBucketLimiter(real_redis, limit=2, window_seconds=2)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True
        assert (await limiter.check(CLIENT, ROUTE)).allowed is False

        import asyncio

        await asyncio.sleep(1.1)
        assert (await limiter.check(CLIENT, ROUTE)).allowed is True

    async def test_sliding_window_enforces_its_limit(self, real_redis):
        limiter = SlidingWindowLimiter(real_redis, limit=5, window_seconds=60)
        verdicts = [(await limiter.check(CLIENT, ROUTE)).allowed for _ in range(7)]
        assert verdicts.count(True) == 5

    async def test_sliding_window_stores_one_entry_per_request(self, real_redis):
        limiter = SlidingWindowLimiter(real_redis, limit=10, window_seconds=60)
        for _ in range(6):
            await limiter.check(CLIENT, ROUTE)
        # The memory tradeoff, confirmed against a real sorted set.
        assert await limiter.stored_entry_count(CLIENT, ROUTE) == 6

    async def test_concurrent_requests_cannot_exceed_the_limit(self, real_redis):
        """The reason every limiter is a Lua script.

        Fired concurrently, a read-then-write limiter would let several
        requests all read the same count and all decide they were under it.
        """
        import asyncio

        limiter = FixedWindowLimiter(real_redis, limit=10, window_seconds=60)
        results = await asyncio.gather(
            *(limiter.check(CLIENT, ROUTE) for _ in range(50))
        )
        assert sum(1 for r in results if r.allowed) == 10


class TestBreakerOnRealRedis:
    @pytest.fixture
    def store(self, real_redis):
        return BreakerStore(
            real_redis,
            BreakerConfig(
                failure_threshold=3,
                failure_window_seconds=60,
                recovery_timeout_seconds=1,
                success_threshold=2,
            ),
        )

    async def test_failures_open_the_breaker(self, store):
        for _ in range(3):
            await store.record_failure("service-a")
        assert (await store.snapshot("service-a")).state is BreakerState.OPEN

    async def test_open_breaker_refuses(self, store):
        for _ in range(3):
            await store.record_failure("service-a")
        allowed, state = await store.allow_request("service-a")
        assert allowed is False
        assert state is BreakerState.OPEN

    async def test_recovery_after_the_real_timeout(self, store):
        import asyncio

        for _ in range(3):
            await store.record_failure("service-a")

        await asyncio.sleep(1.2)
        allowed, state = await store.allow_request("service-a")
        assert allowed is True
        assert state is BreakerState.HALF_OPEN

    async def test_only_one_trial_gets_through_concurrently(self, store):
        """The half-open guard, tested with genuine concurrency rather than
        sequential calls."""
        import asyncio

        for _ in range(3):
            await store.record_failure("service-a")
        await asyncio.sleep(1.2)

        verdicts = await asyncio.gather(
            *(store.allow_request("service-a") for _ in range(20))
        )
        assert sum(1 for allowed, _ in verdicts if allowed) == 1

    async def test_two_stores_share_state(self, real_redis, store):
        other = BreakerStore(real_redis, store.config)
        for _ in range(3):
            await store.record_failure("service-a")
        assert (await other.allow_request("service-a"))[0] is False


class TestBlacklistOnRealRedis:
    async def test_blacklisted_token_is_found(self, real_redis):
        blacklist = TokenBlacklist(real_redis)
        await blacklist.add("jti-1", utcnow() + timedelta(minutes=15))
        assert await blacklist.contains("jti-1") is True

    async def test_entry_expires_with_the_token(self, real_redis):
        blacklist = TokenBlacklist(real_redis)
        await blacklist.add("jti-1", utcnow() + timedelta(minutes=15))
        ttl = await real_redis.ttl("blacklist:token:jti-1")
        assert 880 <= ttl <= 900


class TestPostgres:
    async def test_user_round_trips(self, real_database):
        async with real_database.session() as session:
            users = UserRepository(session)
            created = await users.create("integration@example.com", "hashed")
            assert created.id

        async with real_database.session() as session:
            found = await UserRepository(session).get_by_email("integration@example.com")
            assert found is not None

    async def test_unique_index_is_enforced(self, real_database):
        """SQLite has a looser idea of a unique index than Postgres does."""
        async with real_database.session() as session:
            await UserRepository(session).create("dupe@example.com", "hashed")

        with pytest.raises(IntegrityError):
            async with real_database.session() as session:
                await UserRepository(session).create("dupe@example.com", "hashed")

    async def test_timestamps_come_back_timezone_aware(self, real_database):
        """The bug SQLite hid: it has no timezone type and returns naive
        datetimes, so this comparison is only meaningful against Postgres."""
        async with real_database.session() as session:
            user = await UserRepository(session).create("tz@example.com", "hashed")
            tokens = RefreshTokenRepository(session)
            expiry = utcnow() + timedelta(days=7)
            await tokens.store(user.id, "jti-tz", expiry)

        async with real_database.session() as session:
            stored = await RefreshTokenRepository(session).get_by_jti("jti-tz")
            assert stored.expires_at.tzinfo is not None
            assert stored.is_usable() is True

    async def test_cascade_delete_removes_refresh_tokens(self, real_database):
        from sqlalchemy import delete, select

        from gateway.db.models import RefreshToken, User

        async with real_database.session() as session:
            user = await UserRepository(session).create("cascade@example.com", "hashed")
            await RefreshTokenRepository(session).store(
                user.id, "jti-cascade", utcnow() + timedelta(days=7)
            )
            user_id = user.id

        async with real_database.session() as session:
            await session.execute(delete(User).where(User.id == user_id))

        async with real_database.session() as session:
            result = await session.execute(
                select(RefreshToken).where(RefreshToken.user_id == user_id)
            )
            assert result.scalars().all() == []
