from types import SimpleNamespace

import pytest

from app.core import channel_router
from app.models.channel import ChannelConfig


@pytest.mark.asyncio
async def test_chat_route_preserves_legacy_model_reasoning_default_until_rule_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = SimpleNamespace(
        id=1,
        name="legacy-reasoning-channel",
        is_active=True,
        model_ids=[
            {
                "model_id": "legacy-model",
                "usage": "CHAT",
                "is_enabled": True,
                "reasoning_effort": "high",
            }
        ],
    )

    async def get_channel(_db, _channel_id):
        return channel

    monkeypatch.setattr(channel_router.channel_crud, "get", get_channel)

    inherited_config = ChannelConfig.model_validate(
        {
            "rules": [
                {
                    "channel_id": 1,
                    "model_id": "legacy-model",
                    "priority": 1,
                    "weight": 1,
                    "is_enabled": True,
                }
            ]
        }
    )
    inherited = await channel_router.select_channel(object(), inherited_config, "CHAT", log_selection=False)
    assert inherited is not None
    assert inherited[1]["reasoning_effort"] == "high"

    overridden_config = ChannelConfig.model_validate(
        {
            "rules": [
                {
                    "channel_id": 1,
                    "model_id": "legacy-model",
                    "reasoning_effort": "low",
                    "priority": 1,
                    "weight": 1,
                    "is_enabled": True,
                }
            ]
        }
    )
    overridden = await channel_router.select_channel(object(), overridden_config, "CHAT", log_selection=False)
    assert overridden is not None
    assert overridden[1]["reasoning_effort"] == "low"
