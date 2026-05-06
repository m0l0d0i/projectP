from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    SessionMaker = async_sessionmaker[AsyncSession]
else:
    SessionMaker = async_sessionmaker


def create_engine_and_sessionmaker(database_url: str) -> tuple[AsyncEngine, SessionMaker]:
    engine = create_async_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
        pool_use_lifo=True,
        echo=False,
    )
    sessionmaker = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
        autoflush=False,
    )
    return engine, sessionmaker


async def init_models(engine: AsyncEngine) -> None:  # pragma: no cover
    raise RuntimeError('Автосоздание схемы отключено. Используйте alembic upgrade head')


@asynccontextmanager
async def session_scope(sessionmaker: SessionMaker) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise