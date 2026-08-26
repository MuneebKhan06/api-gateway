"""Blacklist cache tests.

The cache trades a small window of staleness for a large reduction in Redis
traffic, so these check both halves: that it actually absorbs lookups, and
that the staleness stays inside the bounds claimed for it.
"""

from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

from gateway.auth.blacklist import TokenBlacklist
from gateway.auth.blacklist_cache import BlacklistCache


def in_minutes(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class TestCacheBasics:
    def test_rejects_a_non_positive_ttl(self):
        with pytest.raises(ValueError):
            BlacklistCache(ttl_seconds=0)

    def test_rejects_a_non_positive_size(self):
        with pytest.raises(ValueError):
            BlacklistCache(max_entries=0)

    def test_unknown_key_is_a_miss(self):
        assert BlacklistCache().get("nope") is None

    def test_stored_answer_is_returned(self):
        cache = BlacklistCache()
        cache.put("jti-1", False)
        assert cache.get("jti-1") is False

    def test_positive_answer_is_returned(self):
        cache = BlacklistCache()
        cache.put("jti-1", True)
        assert cache.get("jti-1") is True

    def test_invalidate_removes_an_entry(self):
        cache = BlacklistCache()
        cache.put("jti-1", False)
        cache.invalidate("jti-1")
        assert cache.get("jti-1") is None


class TestExpiry:
    def test_negative_answer_expires_after_the_ttl(self):
        cache = BlacklistCache(ttl_seconds=5)
        cache.put("jti-1", False, now=1000.0)

        assert cache.get("jti-1", now=1004.0) is False
        assert cache.get("jti-1", now=1006.0) is None

    def test_positive_answer_is_held_much_longer(self):
        """Revocation does not get undone, so there is nothing to recheck."""
        cache = BlacklistCache(ttl_seconds=5)
        cache.put("jti-1", True, now=1000.0)
        assert cache.get("jti-1", now=1200.0) is True

    def test_expired_entry_is_dropped_not_just_hidden(self):
        cache = BlacklistCache(ttl_seconds=5)
        cache.put("jti-1", False, now=1000.0)
        cache.get("jti-1", now=1006.0)
        assert cache.size == 0


class TestBounding:
    def test_size_is_capped(self):
        """An unbounded cache keyed by token id is a memory leak any client
        can drive by sending fresh tokens."""
        cache = BlacklistCache(max_entries=10)
        for i in range(100):
            cache.put(f"jti-{i}", False)
        assert cache.size == 10

    def test_oldest_entries_are_evicted_first(self):
        cache = BlacklistCache(max_entries=3)
        for i in range(4):
            cache.put(f"jti-{i}", False)

        assert cache.get("jti-0") is None
        assert cache.get("jti-3") is False

    def test_rewriting_a_key_does_not_grow_the_cache(self):
        cache = BlacklistCache(max_entries=10)
        for _ in range(50):
            cache.put("jti-1", False)
        assert cache.size == 1


class TestStats:
    def test_hits_and_misses_are_counted(self):
        cache = BlacklistCache()
        cache.get("absent")
        cache.put("jti-1", False)
        cache.get("jti-1")

        stats = cache.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5


class TestBlacklistIntegration:
    async def test_repeated_checks_hit_redis_once(self, redis_client):
        """The whole point: 100 requests with one token should not be 100
        Redis lookups."""
        cache = BlacklistCache(ttl_seconds=60)
        blacklist = TokenBlacklist(redis_client, cache=cache)

        calls = {"count": 0}
        original = redis_client.exists

        async def counting_exists(*args, **kwargs):
            calls["count"] += 1
            return await original(*args, **kwargs)

        redis_client.exists = counting_exists

        for _ in range(50):
            assert await blacklist.contains("jti-1") is False

        assert calls["count"] == 1
        assert cache.stats["hits"] == 49

    async def test_revoking_is_visible_immediately_on_the_same_instance(self, redis_client):
        """The instance that performs the revocation must not keep serving its
        own stale negative answer."""
        cache = BlacklistCache(ttl_seconds=60)
        blacklist = TokenBlacklist(redis_client, cache=cache)

        assert await blacklist.contains("jti-1") is False
        await blacklist.add("jti-1", in_minutes(15))
        assert await blacklist.contains("jti-1") is True

    async def test_without_a_cache_every_check_reaches_redis(self, redis_client):
        blacklist = TokenBlacklist(redis_client)

        calls = {"count": 0}
        original = redis_client.exists

        async def counting_exists(*args, **kwargs):
            calls["count"] += 1
            return await original(*args, **kwargs)

        redis_client.exists = counting_exists

        for _ in range(5):
            await blacklist.contains("jti-1")
        assert calls["count"] == 5

    async def test_redis_failure_is_not_cached(self, redis_client):
        """Caching a failure would turn a brief blip into several seconds of
        unchecked tokens on every instance."""
        cache = BlacklistCache(ttl_seconds=60)
        blacklist = TokenBlacklist(redis_client, cache=cache)

        class BrokenRedis:
            async def exists(self, *args):
                raise ConnectionError("redis is gone")

        blacklist._redis = BrokenRedis()
        assert await blacklist.contains("jti-1") is False
        assert cache.get("jti-1") is None

    async def test_remove_clears_the_cached_answer(self, redis_client):
        cache = BlacklistCache(ttl_seconds=60)
        blacklist = TokenBlacklist(redis_client, cache=cache)

        await blacklist.add("jti-1", in_minutes(15))
        assert await blacklist.contains("jti-1") is True

        await blacklist.remove("jti-1")
        assert await blacklist.contains("jti-1") is False


class TestEndToEnd:
    def test_logout_still_takes_effect_immediately(self, gateway):
        """The cache must not reintroduce the problem the blacklist solves."""
        credentials = {"email": "muneeb@example.com", "password": "password123"}
        gateway.post("/auth/register", json=credentials)
        tokens = gateway.post("/auth/login", json=credentials).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        assert gateway.get("/api/protected", headers=headers).status_code == 200

        gateway.post("/auth/logout", headers=headers, json={"refresh_token": tokens["refresh_token"]})

        response = gateway.get("/api/protected", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"] == "token_revoked"

    def test_repeated_authenticated_requests_use_the_cache(self, gateway):
        credentials = {"email": "muneeb@example.com", "password": "password123"}
        gateway.post("/auth/register", json=credentials)
        tokens = gateway.post("/auth/login", json=credentials).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        for _ in range(5):
            gateway.get("/api/protected", headers=headers)

        assert gateway.app.state.blacklist_cache.stats["hits"] >= 4
