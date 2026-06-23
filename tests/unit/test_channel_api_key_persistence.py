import pytest
from sqlmodel import select

from app.api.v1.channels import create_channel, update_channel
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
