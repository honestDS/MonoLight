import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.message_platform import MessagePlatform, MessagePlatformCreate, MessagePlatformType


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
