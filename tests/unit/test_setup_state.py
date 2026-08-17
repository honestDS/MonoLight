from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.constants import (
    SETUP_ADMIN_UID_KEY,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_CONFIGURING,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.core.crud.system_setting import system_setting_crud
from app.core.crud.user import user_crud
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


async def _claim_and_commit(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    async with session_factory() as session:
        claimed = await system_setting_crud.claim_setup(session)
        await session.commit()
        return claimed


@pytest.mark.asyncio
async def test_initialize_setup_state_creates_pending_state(
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with setup_session_factory() as session:
        status, admin_uid = await system_setting_crud.initialize_setup_state(session, admin_uid=None)

        assert status == SETUP_STATUS_PENDING
        assert admin_uid is None
        await session.commit()

        rows = await _get_setup_rows(session)
        assert len(rows) == 2
        assert {row.key for row in rows} == {SETUP_STATUS_KEY, SETUP_ADMIN_UID_KEY}

        values = {row.key: row.value for row in rows}
        assert values[SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
        assert values[SETUP_ADMIN_UID_KEY] == ""


@pytest.mark.asyncio
async def test_initialize_setup_state_preserves_existing_admin(
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with setup_session_factory() as session:
        status, admin_uid = await system_setting_crud.initialize_setup_state(session, admin_uid="admin-a")
        assert status == SETUP_STATUS_COMPLETED
        assert admin_uid == "admin-a"
        await session.commit()

    async with setup_session_factory() as session:
        await system_setting_crud.initialize_setup_state(session, admin_uid="admin-b")
        await session.commit()

    async with setup_session_factory() as session:
        status, admin_uid = await system_setting_crud.initialize_setup_state(session, admin_uid=None)
        assert status == SETUP_STATUS_COMPLETED
        assert admin_uid == "admin-a"
        await session.commit()

    async with setup_session_factory() as session:
        assert await system_setting_crud.get_setup_status(session) == SETUP_STATUS_COMPLETED
        assert await system_setting_crud.get_setup_admin_uid(session) == "admin-a"


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
async def test_concurrent_setup_claim_allows_only_one_winner(
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with setup_session_factory() as session:
        await system_setting_crud.initialize_setup_state(session, admin_uid=None)
        await session.commit()

    results = await asyncio.gather(
        _claim_and_commit(setup_session_factory),
        _claim_and_commit(setup_session_factory),
    )

    assert sum(results) == 1
    async with setup_session_factory() as session:
        assert await system_setting_crud.get_setup_status(session) == SETUP_STATUS_CONFIGURING


@pytest.mark.asyncio
async def test_setup_claim_rollback_leaves_pending_state_and_allows_retry(
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with setup_session_factory() as session:
        await system_setting_crud.initialize_setup_state(session, admin_uid=None)
        await session.commit()

    async with setup_session_factory() as rolled_back_session, setup_session_factory() as retry_session:
        assert await system_setting_crud.claim_setup(rolled_back_session)
        await rolled_back_session.rollback()

        assert await system_setting_crud.get_setup_status(retry_session) == SETUP_STATUS_PENDING
        assert await system_setting_crud.claim_setup(retry_session)
        await retry_session.commit()


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


@pytest.mark.asyncio
async def test_get_superuser_ignores_username_and_orders_by_id(
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with setup_session_factory() as session:
        session.add_all(
            [
                User(id=1, uid="uid-admin", username="admin", is_superuser=False),
                User(id=30, uid="uid-super-high", username="super-high", is_superuser=True),
                User(id=10, uid="uid-super-low", username="super-low", is_superuser=True),
            ]
        )
        await session.commit()

        superuser = await user_crud.get_superuser(session)

        assert superuser is not None
        assert superuser.id == 10
        assert superuser.uid == "uid-super-low"
        assert superuser.username == "super-low"
        assert superuser.is_superuser
