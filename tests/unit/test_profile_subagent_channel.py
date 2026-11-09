from app.models.channel import ChannelRule
from app.models.profile import ProfileConfig


def _rule(*, model_id: str, reasoning_effort: str | None = None) -> dict:
    rule = {
        "channel_id": 1,
        "model_id": model_id,
        "priority": 1,
        "weight": 100,
    }
    if reasoning_effort is not None:
        rule["reasoning_effort"] = reasoning_effort
    return rule


def test_subagent_channel_defaults_fallback():
    configs = {
        "channel": {
            "chat_channel": {
                "rules": [_rule(model_id="chat-model", reasoning_effort="high")],
            },
        },
    }

    config = ProfileConfig.model_validate(configs)

    assert config.channel.subagent_channel is not config.channel.chat_channel
    assert config.channel.subagent_channel.rules == config.channel.chat_channel.rules
    assert config.channel.subagent_channel.rules[0].reasoning_effort == "high"


def test_subagent_channel_resolve():
    configured = ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {"rules": [_rule(model_id="chat-model")]},
                "subagent_channel": {"rules": [_rule(model_id="subagent-model")]},
            },
        }
    )

    assert configured.channel.resolve_subagent_channel() is configured.channel.subagent_channel

    fallback = ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {"rules": [_rule(model_id="chat-model")]},
                "subagent_channel": {},
            },
        }
    )

    assert fallback.channel.resolve_subagent_channel() is fallback.channel.chat_channel
    fallback.channel.chat_channel.rules.append(ChannelRule.model_validate(_rule(model_id="updated-chat-model")))
    assert fallback.channel.resolve_subagent_channel() is fallback.channel.chat_channel
    assert len(fallback.channel.resolve_subagent_channel().rules) == 2
    assert fallback.channel.resolve_subagent_channel().rules[-1].model_id == "updated-chat-model"
