"""Repository tests against a real database engine.

SQLite in async mode, not Postgres. The models avoid Postgres specific column
types precisely so this works, and it keeps the test suite runnable without a
container. The integration suite covers the Postgres path.
"""

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from gateway.db.connection import Database
from gateway.db.models import utcnow
from gateway.db.repository import AuditRepository, RefreshTokenRepository, UserRepository


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.startup()
    await database.create_all()
    yield database
    await database.shutdown()


class TestUserRepository:
    async def test_create_and_fetch_by_email(self, db):
        async with db.session() as session:
            users = UserRepository(session)
            created = await users.create("Test@Example.com", "hashed")
            assert created.id

            found = await users.get_by_email("test@example.com")
            assert found is not None
            assert found.email == "test@example.com"

    async def test_email_is_normalised(self, db):
        async with db.session() as session:
            users = UserRepository(session)
            await users.create("  MiXeD@Example.COM  ", "hashed")
            # Lookup by any casing finds the same row.
            assert await users.get_by_email("mixed@example.com") is not None

    async def test_missing_email_returns_none(self, db):
        async with db.session() as session:
            assert await UserRepository(session).get_by_email("nobody@example.com") is None

    async def test_roles_default_and_parse(self, db):
        async with db.session() as session:
            users = UserRepository(session)
            plain = await users.create("a@example.com", "h")
            admin = await users.create("b@example.com", "h", roles="user,admin")
            assert plain.role_list == ["user"]
            assert admin.role_list == ["user", "admin"]

    async def test_duplicate_email_is_rejected(self, db):
        async with db.session() as session:
            await UserRepository(session).create("dup@example.com", "h")

        # The unique index is what enforces this, so the database is expected
        # to raise rather than the repository checking first and racing.
        with pytest.raises(IntegrityError):
            async with db.session() as session:
                await UserRepository(session).create("dup@example.com", "h")


class TestRefreshTokenRepository:
    async def test_store_and_fetch(self, db):
        async with db.session() as session:
            user = await UserRepository(session).create("t@example.com", "h")
            tokens = RefreshTokenRepository(session)
            await tokens.store(user.id, "jti-1", utcnow() + timedelta(days=7))

            stored = await tokens.get_by_jti("jti-1")
            assert stored is not None
            assert stored.is_usable()

    async def test_revoked_token_is_not_usable(self, db):
        async with db.session() as session:
            user = await UserRepository(session).create("t@example.com", "h")
            tokens = RefreshTokenRepository(session)
            await tokens.store(user.id, "jti-1", utcnow() + timedelta(days=7))

            assert await tokens.revoke("jti-1") is True
            assert (await tokens.get_by_jti("jti-1")).is_usable() is False

    async def test_expired_token_is_not_usable(self, db):
        async with db.session() as session:
            user = await UserRepository(session).create("t@example.com", "h")
            tokens = RefreshTokenRepository(session)
            await tokens.store(user.id, "old", utcnow() - timedelta(seconds=1))
            assert (await tokens.get_by_jti("old")).is_usable() is False

    async def test_revoking_unknown_jti_returns_false(self, db):
        async with db.session() as session:
            assert await RefreshTokenRepository(session).revoke("nope") is False

    async def test_revoke_all_for_user(self, db):
        async with db.session() as session:
            users = UserRepository(session)
            target = await users.create("target@example.com", "h")
            other = await users.create("other@example.com", "h")

            tokens = RefreshTokenRepository(session)
            expiry = utcnow() + timedelta(days=7)
            await tokens.store(target.id, "a", expiry)
            await tokens.store(target.id, "b", expiry)
            await tokens.store(other.id, "c", expiry)

            assert await tokens.revoke_all_for_user(target.id) == 2
            # The other user's session is untouched.
            assert (await tokens.get_by_jti("c")).is_usable() is True


class TestAuditRepository:
    async def test_record_and_read_back(self, db):
        async with db.session() as session:
            user = await UserRepository(session).create("t@example.com", "h")
            audit = AuditRepository(session)
            await audit.record("login", user_id=user.id, request_id="req-1")

            entries = await audit.recent_for_user(user.id)
            assert len(entries) == 1
            assert entries[0].event == "login"
            assert entries[0].request_id == "req-1"

    async def test_event_without_a_user_is_allowed(self, db):
        # A failed login has no authenticated user to attribute it to.
        async with db.session() as session:
            entry = await AuditRepository(session).record(
                "login_failed", detail="unknown@example.com"
            )
            assert entry.user_id is None
