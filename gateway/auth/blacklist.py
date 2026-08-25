"""Access token blacklist.

A JWT is valid until it expires. That is the design, and it means logout is
cosmetic unless something on the server side says otherwise: deleting the
token from the client does not stop anyone who copied it.

The blacklist is that something. Logout writes the token's jti to Redis and
every request checks it, so a revoked token stops working immediately.

Each entry expires when the token itself would have. Keeping a blacklist
entry past the token's own expiry is pointless, because the signature check
rejects it anyway, and letting Redis expire the keys means the blacklist
cannot grow without bound.

The cost is one Redis GET on the hot path of every authenticated request.
That is the price of real logout, and it is a deliberate trade.
"""

import logging
from datetime import datetime, timezone

import redis.asyncio as redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "blacklist:token:"


class TokenBlacklist:
    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    @staticmethod
    def _key(jti: str) -> str:
        return f"{KEY_PREFIX}{jti}"

    async def add(self, jti: str, expires_at: datetime, reason: str = "logout") -> bool:
        """Blacklist a token until its own expiry.

        Returns False for a token that has already expired, since blacklisting
        it would be a no-op with no TTL to set.
        """
        ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
        if ttl <= 0:
            logger.debug("Not blacklisting %s, it has already expired", jti)
            return False

        await self._redis.setex(self._key(jti), ttl, reason)
        logger.info("Blacklisted token %s for %ds (%s)", jti, ttl, reason)
        return True

    async def contains(self, jti: str) -> bool:
        """Fail open on a Redis outage.

        If Redis is unreachable the choice is to reject every authenticated
        request or to accept tokens that are still signature-valid and not yet
        expired. Rejecting everything turns a Redis blip into a full gateway
        outage, so this accepts them and logs loudly. The exposure is bounded
        by the access token TTL.
        """
        try:
            return await self._redis.exists(self._key(jti)) == 1
        except Exception as exc:
            logger.error("Blacklist check failed, allowing request: %s", exc)
            return False

    async def reason(self, jti: str) -> str | None:
        return await self._redis.get(self._key(jti))

    async def remove(self, jti: str) -> bool:
        return await self._redis.delete(self._key(jti)) == 1
