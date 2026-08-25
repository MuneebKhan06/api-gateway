"""Database models.

Three tables:

- users: who can authenticate
- refresh_tokens: issued refresh tokens, so they can be revoked
- audit_log: a record of auth events

Access tokens are deliberately absent. They are validated by signature alone
and never stored, which is the whole point of a stateless access token. Only
refresh tokens live in the database, because revoking one has to mean
something.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone aware UTC. `datetime.utcnow()` returns a naive value, which
    compares wrongly against anything that carries a timezone."""
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


def as_aware_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime read back from the database.

    Postgres round-trips the timezone on a timestamptz column. SQLite has no
    timezone type and hands back a naive value, so comparing it against an
    aware `utcnow()` raises. Everything stored here is UTC, so assuming UTC on
    a naive value is safe and keeps the models portable across both.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Stored as a comma separated string rather than a Postgres array so the
    # same schema works against SQLite in tests.
    roles: Mapped[str] = mapped_column(String(255), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def role_list(self) -> list[str]:
        return [role for role in self.roles.split(",") if role]


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The JWT ID claim of the refresh token. Storing the jti instead of the
    # token itself means a leaked database does not hand out usable tokens.
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    __table_args__ = (Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),)

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.revoked_at is None and as_aware_utc(self.expires_at) > now


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # Nullable because a failed login has no authenticated user to attribute.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
