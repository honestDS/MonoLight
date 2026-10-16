import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.api.v1.message_platforms import get_message_platform_types
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.i18n.locale import DEFAULT_LOCALE, SUPPORTED_LOCALES
from app.models.message_platform import MessagePlatform, MessagePlatformCreate, MessagePlatformResponse, MessagePlatformUpdate


@pytest.mark.parametrize("model", [MessagePlatformCreate, MessagePlatformUpdate])
@pytest.mark.parametrize("value", ("fr", "", "EN", "en-US", 123))
def test_message_platform_models_reject_unsupported_languages(model, value):
    with pytest.raises(ValidationError) as exc_info:
        model(name="platform", language=value)

    assert any("language" in error["loc"] for error in exc_info.value.errors())


@pytest.mark.parametrize("model", [MessagePlatformCreate, MessagePlatformUpdate, MessagePlatformResponse])
def test_message_platform_language_schema_exposes_supported_locales(model):
    schema = model.model_json_schema()
    language_schema = schema["properties"]["language"]

    if model is MessagePlatformCreate:
        assert language_schema["default"] == DEFAULT_LOCALE

    if "anyOf" in language_schema:
        language_schema = next(branch for branch in language_schema["anyOf"] if branch.get("type") != "null")
    if "$ref" in language_schema:
        definition_name = language_schema["$ref"].rsplit("/", 1)[-1]
        language_schema = schema["$defs"][definition_name]

    assert language_schema["type"] == "string"
    assert set(language_schema["enum"]) == set(SUPPORTED_LOCALES)


@pytest.mark.asyncio
async def test_message_platform_types_returns_supported_languages():
    response = await get_message_platform_types()

    assert response.data["languages"] == ["zh", "en"]
    assert set(response.data["languages"]) == set(SUPPORTED_LOCALES)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", SUPPORTED_LOCALES)
async def test_message_platform_language_orm_stores_lowercase_value(database_factory, language):
    async with database_factory(tables=[MessagePlatform.__table__]) as session_factory:
        engine = session_factory.kw["bind"]
        async with session_factory() as session:
            platform = await message_platform_crud.create(
                session,
                obj_in=MessagePlatformCreate(name=f"{language}-platform", language=language),
            )
            platform_id = platform.id

        async with engine.connect() as connection:
            stored_language = (await connection.execute(text("SELECT language FROM message_platform WHERE id = :id"), {"id": platform_id})).scalar_one()

        assert stored_language == language

        async with session_factory() as session:
            reloaded_platform = await session.get(MessagePlatform, platform_id)
            assert reloaded_platform is not None
            assert reloaded_platform.language == language

            updated = await message_platform_crud.update(
                session,
                db_obj=reloaded_platform,
                obj_in=MessagePlatformUpdate(name=f"{language}-renamed-platform"),
            )
            assert updated.language == language
            assert MessagePlatformResponse.model_validate(updated).model_dump(mode="json")["language"] == language

        async with session_factory() as session:
            persisted_platform = await session.get(MessagePlatform, platform_id)

        assert persisted_platform is not None
        assert persisted_platform.name == f"{language}-renamed-platform"
        assert persisted_platform.language == language


@pytest.mark.asyncio
async def test_message_platform_language_defaults_to_default_locale(database_factory):
    async with database_factory(tables=[MessagePlatform.__table__]) as session_factory:
        engine = session_factory.kw["bind"]
        async with session_factory() as session:
            platform = await message_platform_crud.create(
                session,
                obj_in=MessagePlatformCreate(name="application-default"),
            )
            assert platform.language == DEFAULT_LOCALE

        async with engine.connect() as connection:
            await connection.execute(
                text("INSERT INTO message_platform (name, platform_type, is_enabled, use_stream_dispatch, status) VALUES (:name, :platform_type, :is_enabled, :use_stream_dispatch, :status)"),
                {
                    "name": "server-default",
                    "platform_type": "WEIXIN_OPENCLAW",
                    "is_enabled": 0,
                    "use_stream_dispatch": 0,
                    "status": "DISCONNECTED",
                },
            )
            await connection.commit()

        async with session_factory() as session:
            stored_language = (
                await session.execute(
                    text("SELECT language FROM message_platform WHERE name = :name"),
                    {"name": "server-default"},
                )
            ).scalar_one()
            assert stored_language == DEFAULT_LOCALE
