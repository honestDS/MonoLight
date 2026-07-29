import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.message_platform import MessagePlatform, MessagePlatformCreate, MessagePlatformType
from scripts import migration_20260729_add_message_platform_use_stream_dispatch as migration


@pytest.mark.asyncio
async def test_message_platform_stream_dispatch_migration_upgrades_legacy_sqlite_schema(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-message-platform-stream-dispatch.db'}")
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
            use_stream_dispatch_column = await connection.run_sync(lambda sync_connection: next(column for column in inspect(sync_connection).get_columns("message_platform") if column["name"] == "use_stream_dispatch"))
            use_stream_dispatch_values = (await connection.execute(text("SELECT use_stream_dispatch FROM message_platform ORDER BY id"))).scalars().all()

        assert use_stream_dispatch_column["nullable"] is False
        assert str(use_stream_dispatch_column["default"]).lower() in {"0", "false"}
        assert use_stream_dispatch_values == [False]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_message_platform_stream_dispatch_migration_skips_missing_table(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'missing-message-platform-stream-dispatch.db'}")
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


def test_message_platform_create_defaults_and_accepts_stream_dispatch_setting():
    assert MessagePlatformCreate(name="default-platform").use_stream_dispatch is False
    assert MessagePlatformCreate(name="stream-platform", use_stream_dispatch=True).use_stream_dispatch is True


@pytest.mark.asyncio
async def test_message_platform_orm_saves_and_reads_stream_dispatch_setting(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'message-platform-stream-dispatch.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync_connection: MessagePlatform.__table__.create(sync_connection))

        async with session_factory() as session:
            default_platform = MessagePlatform(
                name="default-platform",
                platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
            )
            stream_platform = MessagePlatform(
                name="stream-platform",
                platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                use_stream_dispatch=True,
            )
            session.add_all([default_platform, stream_platform])
            await session.commit()
            platform_ids = [default_platform.id, stream_platform.id]

        async with session_factory() as session:
            saved_platforms = [await session.get(MessagePlatform, platform_id) for platform_id in platform_ids]

        assert [platform.use_stream_dispatch for platform in saved_platforms if platform is not None] == [False, True]
    finally:
        await engine.dispose()
