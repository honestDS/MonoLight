from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.constants import (
    SETUP_ADMIN_UID_KEY,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.models.channel import ModelChannel
from app.models.message import Message
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.system_setting import SystemSetting
from app.models.user import User


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[Message.__table__, ChatSession.__table__],
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def setup_session_factory(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "setup-api.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: SQLModel.metadata.create_all(
                    sync_connection,
                    tables=[
                        SystemSetting.__table__,
                        User.__table__,
                        ModelChannel.__table__,
                        PromptLibrary.__table__,
                        Profile.__table__,
                    ],
                )
            )

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add_all(
                [
                    SystemSetting(key=SETUP_STATUS_KEY, value=SETUP_STATUS_PENDING),
                    SystemSetting(key=SETUP_ADMIN_UID_KEY, value=""),
                ]
            )
            await session.commit()

        yield session_factory
    finally:
        await engine.dispose()
