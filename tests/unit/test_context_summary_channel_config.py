from types import SimpleNamespace

import pytest

from app.api.v1 import profile as profile_api
from app.core import constants
from app.core.exceptions import ParameterException
from app.core.utils import channel_profile_sync as channel_profile_sync_module
from app.core.utils.context_summary import service as summary_service_module
from app.models.channel import ModelUsage
from app.models.profile import ProfileConfig


def _rule(channel_id: int, model_id: str) -> dict:
    return {
        "channel_id": channel_id,
        "model_id": model_id,
        "priority": 1,
        "weight": 100,
        "is_enabled": True,
    }


def test_old_profile_context_summary_channel_falls_back_to_independent_chat_copy():
    cfg = ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "chat_timeout": 45,
                    "rules": [_rule(1, "chat-model")],
                }
            }
        }
    )

    assert cfg.channel.context_summary_channel == cfg.channel.chat_channel
    assert cfg.channel.context_summary_channel is not cfg.channel.chat_channel
    assert cfg.channel.context_summary_channel.rules is not cfg.channel.chat_channel.rules

    cfg.channel.context_summary_channel.rules[0].model_id = "summary-model"

    assert cfg.channel.chat_channel.rules[0].model_id == "chat-model"


def test_explicit_context_summary_channel_remains_independent():
    cfg = ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [_rule(1, "chat-model")],
                },
                "context_summary_channel": {
                    "rules": [_rule(2, "summary-model")],
                },
            }
        }
    )

    assert cfg.channel.chat_channel.rules[0].model_id == "chat-model"
    assert cfg.channel.context_summary_channel.rules[0].model_id == "summary-model"


def test_flat_old_profile_context_summary_channel_falls_back_to_chat_channel():
    cfg = ProfileConfig.model_validate(
        {
            "chat_channel": {
                "rules": [_rule(1, "flat-chat-model")],
            }
        }
    )

    assert cfg.channel.context_summary_channel == cfg.channel.chat_channel
    assert cfg.channel.context_summary_channel.rules[0].model_id == "flat-chat-model"


@pytest.mark.asyncio
async def test_context_summary_generation_selects_independent_summary_channel(monkeypatch):
    chat_channel = object()
    summary_channel = object()
    selected_channel_configs = []

    async def select_model(_db, *, channel_config, **_kwargs):
        selected_channel_configs.append(channel_config)
        return None

    monkeypatch.setattr(summary_service_module, "select_context_summary_model", select_model)

    result = await summary_service_module.generate_summary_text(
        object(),
        profile=SimpleNamespace(id=9),
        cfg=SimpleNamespace(
            channel=SimpleNamespace(
                chat_channel=chat_channel,
                context_summary_channel=summary_channel,
            )
        ),
        prompt="history",
        safety_margin_tokens=256,
        uid="user-1",
        session_id="session-1",
    )

    assert result is None
    assert selected_channel_configs == [summary_channel]


@pytest.mark.asyncio
async def test_context_summary_channel_rules_are_validated_as_chat_models(monkeypatch):
    channel = SimpleNamespace(
        model_ids=[
            {
                "model_id": "summary-model",
                "usage": ModelUsage.CHAT.value,
            }
        ]
    )

    async def get_channel(_db, channel_id):
        assert channel_id == 7
        return channel

    monkeypatch.setattr(profile_api.channel_crud, "get", get_channel)

    await profile_api.validate_channel_configs(
        object(),
        {
            "context_summary_channel": {
                "rules": [_rule(7, "summary-model")],
            }
        },
    )


@pytest.mark.asyncio
async def test_context_summary_channel_rejects_non_chat_model(monkeypatch):
    channel = SimpleNamespace(
        model_ids=[
            {
                "model_id": "rerank-model",
                "usage": ModelUsage.RERANK.value,
            }
        ]
    )

    async def get_channel(_db, channel_id):
        assert channel_id == 7
        return channel

    monkeypatch.setattr(profile_api.channel_crud, "get", get_channel)

    with pytest.raises(ParameterException) as exc_info:
        await profile_api.validate_channel_configs(
            object(),
            {
                "context_summary_channel": {
                    "rules": [_rule(7, "rerank-model")],
                }
            },
        )

    assert exc_info.value.message == constants.ERR_CHANNEL_USAGE_MISMATCH


def test_channel_model_cleanup_includes_context_summary_rules():
    configs = {
        "channel": {
            "chat_channel": {
                "rules": [_rule(7, "chat-model")],
            },
            "context_summary_channel": {
                "rules": [
                    _rule(7, "removed-summary-model"),
                    _rule(8, "other-summary-model"),
                ],
            },
        }
    }

    removed_count = channel_profile_sync_module._clean_channel_rules_from_configs(
        configs,
        channel_id=7,
        model_ids=[
            {
                "model_id": "chat-model",
                "usage": ModelUsage.CHAT.value,
            }
        ],
    )

    assert removed_count == 1
    assert configs["channel"]["chat_channel"]["rules"][0]["model_id"] == "chat-model"
    assert [rule["model_id"] for rule in configs["channel"]["context_summary_channel"]["rules"]] == ["other-summary-model"]


def test_channel_model_rename_includes_context_summary_rules():
    configs = {
        "channel": {
            "context_summary_channel": {
                "rules": [_rule(7, "old-summary-model")],
            }
        }
    }

    referenced = channel_profile_sync_module._collect_channel_rule_model_ids(configs, channel_id=7)
    updated_count = channel_profile_sync_module._apply_model_id_renames_to_configs(
        configs,
        channel_id=7,
        renames={
            ModelUsage.CHAT.value: {
                "old-summary-model": "new-summary-model",
            }
        },
    )

    assert referenced[ModelUsage.CHAT.value] == {"old-summary-model"}
    assert updated_count == 1
    assert configs["channel"]["context_summary_channel"]["rules"][0]["model_id"] == "new-summary-model"
