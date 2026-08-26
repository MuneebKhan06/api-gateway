"""Local cache in front of the token blacklist.

Every authenticated request does a Redis lookup to check whether its token has
been revoked. That is one network round trip on the hot path of every single
request, and worse, every gateway instance is reading the same key space, so
at scale the blacklist becomes a hot spot in Redis.

The fix is a small in-process cache with a short TTL. A client sending 100
requests in five seconds with the same token does one Redis lookup instead of
100.

The cost is precision, and it is worth being explicit about it. A token
revoked in Redis can still be accepted by an instance holding a cached "not
blacklisted" answer, for up to the cache TTL. Five seconds is short enough
that it is a much smaller window than the 15 minute access token lifetime the
blacklist is already bounded by, and long enough to absorb the repeated checks
that make this worth doing.

Two things keep the tradeoff one-sided:

- Only negative answers are cached with a TTL. A token found to be
  blacklisted is remembered until its own expiry, because revocation does not
  get undone.
- The cache is bounded. An unbounded dict keyed by token id is a memory leak
  that any client can drive by sending fresh tokens.
"""

import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 5.0
DEFAULT_MAX_ENTRIES = 10_000


class BlacklistCache:
    """Bounded TTL cache for blacklist answers.

    Ordered by insertion so the oldest entry can be evicted when full, which
    makes this an LRU by write rather than by read. That is the right eviction
    order here: entries expire on a timer anyway, so recency of use matters
    less than age.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")

        self._ttl = ttl_seconds
        self._max_entries = max_entries
        # jti -> (is_blacklisted, expires_at)
        self._entries: OrderedDict[str, tuple[bool, float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, jti: str, now: float | None = None) -> bool | None:
        """Cached answer, or None if there is not a usable one."""
        now = now if now is not None else time.monotonic()

        entry = self._entries.get(jti)
        if entry is None:
            self._misses += 1
            return None

        is_blacklisted, expires_at = entry
        if expires_at <= now:
            del self._entries[jti]
            self._misses += 1
            return None

        self._hits += 1
        return is_blacklisted

    def put(self, jti: str, is_blacklisted: bool, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()

        if is_blacklisted:
            # Revocation is permanent, so this answer never needs rechecking.
            # A long TTL here is safe in the direction that matters: the worst
            # case is refusing a token that was already revoked.
            expires_at = now + max(self._ttl, 300.0)
        else:
            expires_at = now + self._ttl

        # Re-inserting moves the key to the end, keeping eviction order sane.
        self._entries.pop(jti, None)
        self._entries[jti] = (is_blacklisted, expires_at)

        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            logger.debug("Evicted blacklist cache entry %s", evicted)

    def invalidate(self, jti: str) -> None:
        """Drop one entry, so a fresh revocation takes effect immediately on
        the instance that performed it."""
        self._entries.pop(jti, None)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def stats(self) -> dict[str, int | float]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "size": self.size,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
        }
