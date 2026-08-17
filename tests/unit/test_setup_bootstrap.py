from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.providers.database.bootstrap as bootstrap
from app.core.constants import (
    SETUP_ADMIN_UID_KEY,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.core.crud.system_setting import DEFAULT_SYSTEM_SETTINGS
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.system_setting import SystemSetting
from app.models.user import User

BOOTSTRAP_TABLES = [
    SystemSetting.__table__,
    User.__table__,
    PromptLibrary.__table__,
    Profile.__table__,
]


@pytest_asyncio.fixture
async def bootstrap_database(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "setup-bootstrap.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=BOOTSTRAP_TABLES,
            )
        )

    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _async_noop(_session: AsyncSession) -> None:
    return None


async def _read_settings(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(SystemSetting))
    return {setting.key: setting.value for setting in result.scalars().all()}


@pytest.mark.asyncio
async def test_init_system_data_creates_pending_setup_without_default_profile(bootstrap_database, monkeypatch):
    monkeypatch.setattr(bootstrap, "init_database_schema", _async_noop)

    async with bootstrap_database() as session:
        await bootstrap.init_system_data(session)

    async with bootstrap_database() as session:
        settings = await _read_settings(session)
        prompt_result = await session.execute(select(PromptLibrary).where(PromptLibrary.name == "default"))
        prompts = list(prompt_result.scalars().all())
        profile_result = await session.execute(select(Profile))
        profiles = list(profile_result.scalars().all())

    assert settings[SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
    assert settings[SETUP_ADMIN_UID_KEY] == ""
    for key, value in DEFAULT_SYSTEM_SETTINGS.items():
        assert settings[key] == value
    assert len(prompts) == 1
    assert prompts[0].uid is None
    assert profiles == []


@pytest.mark.asyncio
async def test_init_system_data_uses_first_superuser_and_is_idempotent(bootstrap_database, monkeypatch):
    monkeypatch.setattr(bootstrap, "init_database_schema", _async_noop)

    first_superuser_uid = "first-superuser"
    second_superuser_uid = "second-superuser"
    async with bootstrap_database() as session:
        legacy_admin = User(uid="legacy-admin", username="admin", is_superuser=False)
        first_superuser = User(uid=first_superuser_uid, username="operator-one", is_superuser=True)
        second_superuser = User(uid=second_superuser_uid, username="operator-two", is_superuser=True)
        session.add(legacy_admin)
        await session.flush()
        session.add(first_superuser)
        await session.flush()
        session.add(second_superuser)
        await session.commit()
        assert legacy_admin.id < first_superuser.id < second_superuser.id

        await bootstrap.init_system_data(session)
        settings_after_first_run = await _read_settings(session)

        await bootstrap.init_system_data(session)
        settings_after_second_run = await _read_settings(session)
        prompt_result = await session.execute(select(PromptLibrary).where(PromptLibrary.name == "default"))
        prompts = list(prompt_result.scalars().all())
        profile_result = await session.execute(select(Profile).where(Profile.name == "default"))
        profiles = list(profile_result.scalars().all())

    assert settings_after_first_run[SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert settings_after_first_run[SETUP_ADMIN_UID_KEY] == first_superuser_uid
    assert settings_after_second_run[SETUP_STATUS_KEY] == settings_after_first_run[SETUP_STATUS_KEY]
    assert settings_after_second_run[SETUP_ADMIN_UID_KEY] == settings_after_first_run[SETUP_ADMIN_UID_KEY]
    assert len(prompts) == 1
    assert len(profiles) == 1
    assert profiles[0].uid == first_superuser_uid
    assert profiles[0].is_default is True
    assert profiles[0].prompt_id == prompts[0].id
    assert {profile.uid for profile in profiles} == {first_superuser_uid}
    assert second_superuser_uid not in {profile.uid for profile in profiles}


@pytest.mark.asyncio
async def test_init_system_data_preserves_non_superuser_setup_admin_uid(bootstrap_database, monkeypatch):
    monkeypatch.setattr(bootstrap, "init_database_schema", _async_noop)

    protected_admin_uid = "configured-user"
    async with bootstrap_database() as session:
        session.add_all(
            [
                User(uid=protected_admin_uid, username="configured-user", is_superuser=False),
                User(uid="actual-superuser", username="actual-superuser", is_superuser=True),
                SystemSetting(key=SETUP_STATUS_KEY, value=SETUP_STATUS_COMPLETED),
                SystemSetting(key=SETUP_ADMIN_UID_KEY, value=protected_admin_uid),
            ]
        )
        await session.commit()

        await bootstrap.init_system_data(session)

        settings = await _read_settings(session)
        profile_result = await session.execute(select(Profile))
        profiles = list(profile_result.scalars().all())

    assert settings[SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert settings[SETUP_ADMIN_UID_KEY] == protected_admin_uid
    assert profiles == []
