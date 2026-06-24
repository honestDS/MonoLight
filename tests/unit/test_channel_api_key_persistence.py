import pytest
from sqlmodel import select

from app.api.v1.channels import ChannelImageGenerationTestRequest, create_channel, update_channel
from app.api.v1.channels import test_channel_image_generation as run_channel_image_generation_test
from app.core import constants
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.models.channel import ChannelCreate, ChannelType, ChannelUpdate, ModelChannel


@pytest.mark.asyncio
async def test_create_channel_stores_encrypted_api_key(db_session, monkeypatch):
    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)

    response = await create_channel(
        ChannelCreate(
            name="HUB_free",
            channel_type=ChannelType.OPENAI,
            api_key="plain-create-key",
            base_url="https://hub.oaifree.com/v1/",
            is_active=True,
            model_ids=[
                {
                    "model_id": "mimo-v2.5",
                    "usage": "CHAT",
                    "image_understanding": True,
                    "audio_understanding": False,
                    "video_understanding": False,
                    "context_window_k": 800,
                    "temperature": 0.7,
                    "top_p": 1,
                    "max_tokens": 2048,
                    "embedding_dimensions": None,
                    "description": "",
                }
            ],
        ),
        db_session,
        admin={},
    )

    assert response.code == 200

    result = await db_session.execute(select(ModelChannel).where(ModelChannel.name == "HUB_free"))
    channel = result.scalars().one()
    assert channel.api_key.startswith("enc:v1:")
    assert channel.api_key != "plain-create-key"
    assert channel.get_decrypted_api_key() == "plain-create-key"


@pytest.mark.asyncio
async def test_update_channel_stores_new_encrypted_api_key(db_session, monkeypatch):
    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)

    channel = ModelChannel(
        name="HUB_free",
        channel_type=ChannelType.OPENAI,
        api_key="placeholder",
        base_url="https://hub.oaifree.com/v1/",
        is_active=True,
        model_ids=[{"model_id": "mimo-v2.5", "usage": "CHAT"}],
    )
    channel.set_api_key_plaintext("old-key")
    db_session.add(channel)
    await db_session.commit()
    await db_session.refresh(channel)

    response = await update_channel(
        channel.id,
        ChannelUpdate(
            name="HUB_free",
            channel_type=ChannelType.OPENAI,
            api_key="plain-update-key",
            base_url="https://hub.oaifree.com/v1/",
            is_active=True,
            model_ids=[
                {
                    "model_id": "mimo-v2.5",
                    "usage": "CHAT",
                    "image_understanding": True,
                    "audio_understanding": False,
                    "video_understanding": False,
                    "context_window_k": 800,
                    "temperature": 0.7,
                    "top_p": 1,
                    "max_tokens": 2048,
                    "embedding_dimensions": None,
                    "description": "",
                }
            ],
        ),
        db_session,
        admin={},
    )

    assert response.code == 200

    result = await db_session.execute(select(ModelChannel).where(ModelChannel.id == channel.id))
    updated_channel = result.scalars().one()
    assert updated_channel.api_key.startswith("enc:v1:")
    assert updated_channel.api_key != "plain-update-key"
    assert updated_channel.get_decrypted_api_key() == "plain-update-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "model_entry"),
    [
        ("CHAT", {"model_id": "gpt-4o", "usage": "CHAT"}),
        ("EMBEDDING", {"model_id": "text-embedding-3-small", "usage": "EMBEDDING"}),
        ("RERANK", {"model_id": "rerank-model", "usage": "RERANK"}),
        (
            "IMAGE_GENERATION",
            {
                "model_id": "gpt-image-1",
                "usage": "IMAGE_GENERATION",
                "size": "1024x1024",
                "quality": "auto",
            },
        ),
    ],
)
async def test_create_channel_rejects_model_entries_without_base_url(db_session, usage, model_entry):
    response = await create_channel(
        ChannelCreate(
            name=f"{usage.lower()}-channel",
            channel_type=ChannelType.OPENAI,
            api_key="plain-create-key",
            base_url=None,
            is_active=True,
            model_ids=[model_entry],
        ),
        db_session,
        admin={},
    )

    assert response.code == 422
    assert response.message == t(constants.ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)


@pytest.mark.asyncio
async def test_channel_image_generation_test_returns_generated_image(monkeypatch):
    async def mock_generate_image(**kwargs):
        return {
            "model": kwargs["model_id"],
            "data": [{"url": "https://example.com/generated.png"}],
        }

    monkeypatch.setattr("app.api.v1.channels.ImageGenerationClient.generate_image", mock_generate_image)

    response = await run_channel_image_generation_test(
        ChannelImageGenerationTestRequest(
            channel_type=ChannelType.OPENAI,
            api_key="fake-key",
            base_url="https://api.example.com/v1",
            model_id="gpt-image-1",
            size="1024x1024",
            quality="auto",
        ),
        _admin={},
    )

    assert response.code == 200
    assert response.data == {
        "model": "gpt-image-1",
        "image": {"url": "https://example.com/generated.png"},
    }


@pytest.mark.asyncio
async def test_channel_image_generation_test_rejects_empty_image_response(monkeypatch):
    async def mock_generate_image(**kwargs):
        return {"model": kwargs["model_id"], "data": []}

    monkeypatch.setattr("app.api.v1.channels.ImageGenerationClient.generate_image", mock_generate_image)

    with pytest.raises(ParameterException) as exc_info:
        await run_channel_image_generation_test(
            ChannelImageGenerationTestRequest(
                channel_type=ChannelType.OPENAI,
                api_key="fake-key",
                base_url="https://api.example.com/v1",
                model_id="gpt-image-1",
                size="1024x1024",
                quality="auto",
            ),
            _admin={},
        )

    assert exc_info.value.message == constants.ERR_CHANNEL_IMAGE_GENERATION_TEST_EMPTY_RESPONSE


@pytest.mark.asyncio
async def test_update_channel_rejects_clearing_base_url_when_models_exist(db_session, monkeypatch):
    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)

    channel = ModelChannel(
        name="image-channel",
        channel_type=ChannelType.OPENAI,
        api_key="placeholder",
        base_url="https://api.example.com/v1",
        is_active=True,
        model_ids=[
            {
                "model_id": "gpt-image-1",
                "usage": "IMAGE_GENERATION",
                "size": "1024x1024",
                "quality": "auto",
            }
        ],
    )
    channel.set_api_key_plaintext("old-key")
    db_session.add(channel)
    await db_session.commit()
    await db_session.refresh(channel)

    response = await update_channel(
        channel.id,
        ChannelUpdate(base_url=None),
        db_session,
        admin={},
    )

    assert response.code == 422
    assert response.message == t(constants.ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)
