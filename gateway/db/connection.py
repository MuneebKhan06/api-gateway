"""Async database engine and session handling."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gateway.db.models import Base

logger = logging.getLogger(__name__)


class Database:
    """Owns the engine and session factory for the process.

    The engine holds a connection pool, so it is created once at startup and
    disposed at shutdown rather than per request.
    """

    def __init__(self, url: str, echo: bool = False) -> None:
        self._url = url
        self._echo = echo
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    async def startup(self) -> None:
        self._engine = create_async_engine(self._url, echo=self._echo, pool_pre_ping=True)
        self._sessionmaker = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            # Objects stay usable after commit, which avoids a surprise
            # lazy-load against a closed session in the response layer.
            expire_on_commit=False,
        )
        logger.info("Database engine ready")

    async def shutdown(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
            logger.info("Database engine disposed")

    async def create_all(self) -> None:
        """Create tables directly from the models.

        Used by tests and local throwaway setups. Real deployments go through
        Alembic so schema changes are versioned.
        """
        if self._engine is None:
            raise RuntimeError("Database.startup() was never awaited")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session scope that commits on success and rolls back on failure."""
        if self._sessionmaker is None:
            raise RuntimeError("Database.startup() was never awaited")

        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> bool:
        """Cheap liveness probe for the health endpoint."""
        if self._engine is None:
            return False
        try:
            from sqlalchemy import text

            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("Database ping failed: %s", exc)
            return False
