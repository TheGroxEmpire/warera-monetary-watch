from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from warera_monetary_watch.config import get_settings


def create_sessionmaker(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(database_url, future=True, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@lru_cache(maxsize=1)
def get_default_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(get_settings().database_url)


def SessionLocal(**kwargs: Any) -> AsyncSession:
    return get_default_sessionmaker()(**kwargs)


def create_session_dependency(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[[], AsyncIterator[AsyncSession]]:
    async def session_dependency() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    return session_dependency


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
