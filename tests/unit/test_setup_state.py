from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.constants import (
    ERR_SETUP_STATUS_NOT_INITIALIZED,
    ERR_SYSTEM_SETTING_NOT_FOUND_AFTER_INSERT,
    SETUP_ADMIN_UID_KEY,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.core.crud.system.setting import system_setting_crud
from app.core.i18n import t
from app.models.system_setting import SystemSetting
from app.models.user import User


@pytest_asyncio.fixture
async def setup_session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "setup-state.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )
    tables = [SystemSetting.__table__, User.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=tables))

    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _get_setup_rows(session: AsyncSession) -> list[SystemSetting]:
    result = await session.execute(select(SystemSetting).order_by(SystemSetting.id.asc()))
    return list(result.scalars().all())


async def _initialize_and_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str | None]:
    async with session_factory() as session:
        result = await system_setting_crud.initialize_setup_state(session, admin_uid=None)
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_insert_if_missing_raises_when_setting_is_not_found_after_insert(
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_setting_crud, "get_by_key", AsyncMock(return_value=None))

    async with setup_session_factory() as session:
        with pytest.raises(RuntimeError) as exc_info:
            await system_setting_crud._insert_if_missing(session, key=SETUP_STATUS_KEY, value=SETUP_STATUS_PENDING)

        assert str(exc_info.value) == t(
            ERR_SYSTEM_SETTING_NOT_FOUND_AFTER_INSERT,
            setting_key=SETUP_STATUS_KEY,
        )


@pytest.mark.asyncio
async def test_initialize_setup_state_raises_when_setup_status_is_not_initialized(
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_setting_crud, "get_setup_status", AsyncMock(return_value=None))

    async with setup_session_factory() as session:
        with pytest.raises(RuntimeError) as exc_info:
            await system_setting_crud.initialize_setup_state(session, admin_uid=None)

        assert str(exc_info.value) == t(ERR_SETUP_STATUS_NOT_INITIALIZED)


@pytest.mark.asyncio
async def test_concurrent_first_initialization_is_idempotent(
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    results = await asyncio.gather(
        _initialize_and_commit(setup_session_factory),
        _initialize_and_commit(setup_session_factory),
    )

    assert results == [(SETUP_STATUS_PENDING, None), (SETUP_STATUS_PENDING, None)]

    async with setup_session_factory() as session:
        rows = await _get_setup_rows(session)
        assert len(rows) == 2
        assert {row.key for row in rows} == {SETUP_STATUS_KEY, SETUP_ADMIN_UID_KEY}


@pytest.mark.asyncio
async def test_setup_completion_requires_claim_and_is_terminal(
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with setup_session_factory() as session:
        await system_setting_crud.initialize_setup_state(session, admin_uid=None)
        await session.commit()
        assert not await system_setting_crud.complete_setup(session)

        assert await system_setting_crud.claim_setup(session)
        assert await system_setting_crud.set_setup_admin_uid(session, admin_uid="admin-final")
        assert await system_setting_crud.complete_setup(session)
        await session.commit()

    async with setup_session_factory() as session:
        assert await system_setting_crud.get_setup_status(session) == SETUP_STATUS_COMPLETED
        assert await system_setting_crud.get_setup_admin_uid(session) == "admin-final"
        assert not await system_setting_crud.claim_setup(session)
        assert not await system_setting_crud.complete_setup(session)
