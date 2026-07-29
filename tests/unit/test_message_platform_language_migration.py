import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.message_platforms import get_message_platform_types
from app.models.message_platform import MessagePlatform, MessagePlatformCreate, MessagePlatformLanguage, MessagePlatformType
from scripts import migration_20260729_add_message_platform_language as migration


@pytest.mark.asyncio
async def test_message_platform_language_migration_upgrades_legacy_sqlite_schema(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-message-platform.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE message_platform (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL)"))
            await connection.execute(text("INSERT INTO message_platform (id, name) VALUES (1, 'legacy-platform')"))

        async with session_factory() as session:
            await migration.migrate(session)
            await session.commit()
            await migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            language_column = await connection.run_sync(lambda sync_connection: next(column for column in inspect(sync_connection).get_columns("message_platform") if column["name"] == "language"))
            language_values = (await connection.execute(text("SELECT language FROM message_platform ORDER BY id"))).scalars().all()

        assert language_column["nullable"] is False
        assert str(language_column["default"]).strip("'") == "zh"
        assert language_values == ["zh"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_message_platform_language_migration_skips_missing_table(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'missing-message-platform.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            table_names = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))

        assert "message_platform" not in table_names
    finally:
        await engine.dispose()


def test_message_platform_create_rejects_unsupported_language():
    with pytest.raises(ValidationError):
        MessagePlatformCreate(name="platform", language="fr")


@pytest.mark.asyncio
async def test_message_platform_types_returns_supported_languages():
    response = await get_message_platform_types()

    assert response.data["languages"] == ["zh", "en"]


@pytest.mark.asyncio
async def test_message_platform_language_orm_stores_lowercase_enum_value(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'message-platform-language.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync_connection: MessagePlatform.__table__.create(sync_connection))

        async with session_factory() as session:
            platform = MessagePlatform(
                name="english-platform",
                platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                language=MessagePlatformLanguage.EN,
            )
            session.add(platform)
            await session.commit()
            platform_id = platform.id

        async with engine.connect() as connection:
            stored_language = (await connection.execute(text("SELECT language FROM message_platform WHERE id = :id"), {"id": platform_id})).scalar_one()

        async with session_factory() as session:
            reloaded_platform = await session.get(MessagePlatform, platform_id)

        assert stored_language == "en"
        assert reloaded_platform is not None
        assert isinstance(reloaded_platform.language, MessagePlatformLanguage)
        assert reloaded_platform.language == MessagePlatformLanguage.EN
    finally:
        await engine.dispose()
