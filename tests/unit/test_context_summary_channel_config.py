from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.api.v1 import profile as profile_api
from app.core import profile_validation as profile_validation_module
from app.core.constants import ERR_CHANNEL_MODEL_NOT_FOUND, ERR_CHANNEL_USAGE_MISMATCH
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
                "protocol": "OPENAI",
            }
        ]
    )

    async def get_channel(_db, channel_id):
        assert channel_id == 7
        return channel

    monkeypatch.setattr(profile_validation_module.channel_crud, "get", get_channel)

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
                "protocol": "COHERE_RERANK",
            }
        ]
    )

    async def get_channel(_db, channel_id):
        assert channel_id == 7
        return channel

    monkeypatch.setattr(profile_validation_module.channel_crud, "get", get_channel)

    with pytest.raises(ParameterException) as exc_info:
        await profile_api.validate_channel_configs(
            object(),
            {
                "context_summary_channel": {
                    "rules": [_rule(7, "rerank-model")],
                }
            },
        )

    assert exc_info.value.message == ERR_CHANNEL_USAGE_MISMATCH


@pytest.mark.asyncio
async def test_rerank_channel_ignores_pending_same_id_chat_model(monkeypatch):
    channel = SimpleNamespace(
        model_ids=[
            {
                "model_id": "shared",
                "usage": ModelUsage.CHAT.value,
                "lifecycle_status": "pending_delete",
                "is_enabled": False,
            },
            {
                "model_id": "shared",
                "usage": ModelUsage.RERANK.value,
                "lifecycle_status": "active",
                "is_enabled": True,
            },
        ]
    )

    async def get_channel(_db, channel_id):
        assert channel_id == 7
        return channel

    monkeypatch.setattr(profile_validation_module.channel_crud, "get", get_channel)

    await profile_api.validate_channel_configs(
        object(),
        {
            "rerank_channel": {
                "rules": [_rule(7, "shared")],
            }
        },
    )


@pytest.mark.asyncio
async def test_rerank_channel_rejects_pending_exact_usage_with_same_id_chat_model(monkeypatch):
    channel = SimpleNamespace(
        model_ids=[
            {
                "model_id": "shared",
                "usage": ModelUsage.CHAT.value,
                "lifecycle_status": "active",
                "is_enabled": True,
            },
            {
                "model_id": "shared",
                "usage": ModelUsage.RERANK.value,
                "lifecycle_status": "pending_delete",
                "is_enabled": False,
            },
        ]
    )

    async def get_channel(_db, channel_id):
        assert channel_id == 7
        return channel

    monkeypatch.setattr(profile_validation_module.channel_crud, "get", get_channel)

    with pytest.raises(ParameterException) as exc_info:
        await profile_api.validate_channel_configs(
            object(),
            {
                "rerank_channel": {
                    "rules": [_rule(7, "shared")],
                }
            },
        )

    assert exc_info.value.message == ERR_CHANNEL_MODEL_NOT_FOUND


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
                "protocol": "OPENAI",
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


@pytest.mark.asyncio
async def test_preview_channel_model_update_impacts_applies_renames_before_cleanup():
    class FakeScalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class FakeResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return FakeScalars(self.values)

    class FakeDB:
        def __init__(self, results):
            self.results = iter(results)

        async def execute(self, _statement):
            return next(self.results)

    profile = SimpleNamespace(
        id=1,
        configs={
            "channel": {
                "chat_channel": {
                    "rules": [_rule(7, "old-chat-model")],
                }
            },
            "security": {
                "audit_channel_id": 7,
                "audit_model_id": "old-chat-model",
            },
        },
    )
    original_configs = deepcopy(profile.configs)
    old_model_ids = [
        {
            "model_id": "old-chat-model",
            "usage": ModelUsage.CHAT.value,
            "protocol": "OPENAI",
            "context_window_k": 128,
            "max_tokens": 64,
        }
    ]
    new_model_ids = [
        {
            "model_id": "new-chat-model",
            "usage": ModelUsage.CHAT.value,
            "protocol": "OPENAI",
            "context_window_k": 128,
            "max_tokens": 64,
        }
    ]

    impacts = await channel_profile_sync_module._preview_channel_model_update_impacts(
        FakeDB([FakeResult([profile]), FakeResult([])]),
        7,
        old_model_ids,
        new_model_ids,
    )

    assert impacts == {
        "synced_profile_rules": 1,
        "removed_profile_rules": 0,
        "synced_audit_refs": 1,
        "cleared_audit_refs": 0,
    }
    assert profile.configs == original_configs
