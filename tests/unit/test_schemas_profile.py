from app.models.profile import ProfileConfig


def test_profile_config_standardization():
    old_data = {
        "chat_channel": {
            "chat_timeout": 45.0,
            "rules": [
                {"channel_id": 1, "model_id": "gpt-4o", "priority": 1, "weight": 1},
            ],
        },
        "shell_timeout": 45.0,
        "audit_threshold": 3,
    }
    cfg = ProfileConfig.model_validate(old_data)
    assert cfg.channel.chat_channel.chat_timeout == 45.0
    assert cfg.channel.chat_channel.rules[0].model_id == "gpt-4o"
    assert cfg.tool.shell_timeout == 45.0
    assert cfg.security.audit_threshold == 3


def test_profile_config_defaults():
    data = {"channel": {"chat_channel": {"rules": []}}}
    cfg = ProfileConfig.model_validate(data)
    assert cfg.channel.chat_channel.chat_timeout == 60.0
    assert cfg.security.audit_threshold == 5
    assert cfg.tool.shell_timeout == 30.0
