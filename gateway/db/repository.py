"""Data access.

Query construction lives here rather than in request handlers, so the handlers
stay about HTTP and the queries stay testable on their own.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.models import AuditLog, RefreshToken, User, utcnow


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, email: str, password_hash: str, roles: str = "user") -> User:
        user = User(email=email.lower().strip(), password_hash=password_hash, roles=roles)
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.email == email.lower().strip())
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: str) -> User | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def email_exists(self, email: str) -> bool:
        return await self.get_by_email(email) is not None


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def store(self, user_id: str, jti: str, expires_at: datetime) -> RefreshToken:
        token = RefreshToken(user_id=user_id, jti=jti, expires_at=expires_at)
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        return result.scalar_one_or_none()

    async def revoke(self, jti: str) -> bool:
        """Mark one refresh token unusable. Returns False if it was not found."""
        token = await self.get_by_jti(jti)
        if token is None:
            return False
        if token.revoked_at is None:
            token.revoked_at = utcnow()
            await self._session.flush()
        return True

    async def revoke_all_for_user(self, user_id: str) -> int:
        """Used on logout-everywhere and after a password change."""
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        await self._session.flush()
        return result.rowcount or 0


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        event: str,
        user_id: str | None = None,
        detail: str | None = None,
        request_id: str | None = None,
        ip_address: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            event=event,
            user_id=user_id,
            detail=detail,
            request_id=request_id,
            ip_address=ip_address,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def recent_for_user(self, user_id: str, limit: int = 50) -> list[AuditLog]:
        result = await self._session.execute(
            select(AuditLog)
            .where(AuditLog.user_id == user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
