import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.message_platforms import get_message_platform_types
from app.models.message_platform import MessagePlatform, MessagePlatformCreate, MessagePlatformLanguage, MessagePlatformType


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
