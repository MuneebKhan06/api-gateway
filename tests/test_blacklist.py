"""Blacklist tests against fakeredis.

fakeredis implements real Redis semantics in process, including TTL handling,
so these exercise the same code paths a live Redis would.
"""

from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

from gateway.auth.blacklist import KEY_PREFIX, TokenBlacklist


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def blacklist(redis_client):
    return TokenBlacklist(redis_client)


def in_minutes(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


class TestAdding:
    async def test_blacklisted_token_is_found(self, blacklist):
        await blacklist.add("jti-1", in_minutes(15))
        assert await blacklist.contains("jti-1") is True

    async def test_unknown_token_is_not_found(self, blacklist):
        assert await blacklist.contains("never-seen") is False

    async def test_add_reports_success(self, blacklist):
        assert await blacklist.add("jti-1", in_minutes(15)) is True

    async def test_already_expired_token_is_not_stored(self, blacklist):
        # Nothing to revoke: signature validation rejects it anyway.
        assert await blacklist.add("old", datetime.now(timezone.utc) - timedelta(seconds=1)) is False
        assert await blacklist.contains("old") is False

    async def test_reason_is_stored(self, blacklist):
        await blacklist.add("jti-1", in_minutes(15), reason="password_change")
        assert await blacklist.reason("jti-1") == "password_change"

    async def test_default_reason_is_logout(self, blacklist):
        await blacklist.add("jti-1", in_minutes(15))
        assert await blacklist.reason("jti-1") == "logout"

    async def test_tokens_are_independent(self, blacklist):
        await blacklist.add("jti-1", in_minutes(15))
        assert await blacklist.contains("jti-2") is False


class TestExpiry:
    async def test_entry_ttl_tracks_the_token_expiry(self, blacklist, redis_client):
        await blacklist.add("jti-1", in_minutes(15))
        ttl = await redis_client.ttl(f"{KEY_PREFIX}jti-1")
        # 15 minutes, allowing a second of drift from the round trip.
        assert 890 <= ttl <= 900

    async def test_entry_disappears_once_the_token_would_have_expired(
        self, blacklist, redis_client
    ):
        await blacklist.add("jti-1", in_minutes(15))
        # Expire the key the way Redis eventually would.
        await redis_client.delete(f"{KEY_PREFIX}jti-1")
        assert await blacklist.contains("jti-1") is False


class TestRemoval:
    async def test_remove_clears_the_entry(self, blacklist):
        await blacklist.add("jti-1", in_minutes(15))
        assert await blacklist.remove("jti-1") is True
        assert await blacklist.contains("jti-1") is False

    async def test_removing_an_absent_entry_returns_false(self, blacklist):
        assert await blacklist.remove("nope") is False


class TestRedisFailure:
    async def test_check_fails_open_when_redis_is_down(self, redis_client):
        """A Redis outage must not take the whole gateway down.

        Failing closed would reject every authenticated request. Failing open
        accepts tokens that are still signature-valid and unexpired, which is
        bounded by the access token TTL.
        """
        await redis_client.aclose()
        blacklist = TokenBlacklist(redis_client)

        class BrokenRedis:
            async def exists(self, *args):
                raise ConnectionError("redis is gone")

        blacklist._redis = BrokenRedis()
        assert await blacklist.contains("jti-1") is False
