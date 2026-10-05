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


@pytest.mark.asyncio
async def test_chat_route_round_robin_applies_rule_reasoning_effort_without_mutating_model_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(
        id=1,
        name="weighted-reasoning-channel",
        is_active=True,
        model_ids=[
            {"model_id": "model-a", "usage": "CHAT", "is_enabled": True},
            {"model_id": "model-b", "usage": "CHAT", "is_enabled": True},
            {"model_id": "fallback-model", "usage": "CHAT", "is_enabled": True},
        ],
    )

    async def get_channel(_db, _channel_id):
        return channel

    cursor = 0

    async def next_index(_cursor_key, length):
        nonlocal cursor
        index = cursor % length
        cursor = (cursor + 1) % length
        return index

    monkeypatch.setattr(channel_router.channel_crud, "get", get_channel)
    monkeypatch.setattr(channel_router.channel_cursor_crud, "next_index", next_index)

    channel_config = ChannelConfig.model_validate(
        {
            "rules": [
                {
                    "channel_id": 1,
                    "model_id": "model-a",
                    "priority": 1,
                    "weight": 1,
                    "reasoning_effort": "high",
                    "is_enabled": True,
                },
                {
                    "channel_id": 1,
                    "model_id": "model-b",
                    "priority": 1,
                    "weight": 2,
                    "reasoning_effort": "low",
                    "is_enabled": True,
                },
                {
                    "channel_id": 1,
                    "model_id": "fallback-model",
                    "priority": 2,
                    "weight": 1,
                    "is_enabled": True,
                },
            ]
        }
    )

    selections = []
    for _ in range(6):
        selected = await channel_router.select_channel(
            object(),
            channel_config,
            "CHAT",
            cursor_key="fixed-cursor",
            log_selection=False,
        )
        assert selected is not None
        selections.append((selected[1]["model_id"], selected[1]["reasoning_effort"]))

    assert selections == [
        ("model-a", "high"),
        ("model-b", "low"),
        ("model-b", "low"),
        ("model-a", "high"),
        ("model-b", "low"),
        ("model-b", "low"),
    ]
    assert all("reasoning_effort" not in entry for entry in channel.model_ids)

    fallback = await channel_router.select_channel(
        object(),
        channel_config,
        "CHAT",
        excluded_priorities={1},
        cursor_key="fixed-cursor",
        log_selection=False,
    )
    assert fallback is not None
    assert fallback[1]["model_id"] == "fallback-model"
    assert fallback[1]["reasoning_effort"] is None
    assert all("reasoning_effort" not in entry for entry in channel.model_ids)
