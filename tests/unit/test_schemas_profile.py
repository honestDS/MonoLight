from app.schemas.profile import ProfileConfig


def test_profile_config_standardization():
    # 测试旧版平铺数据自动泵入嵌套结构
    old_data = {
        "model_id": "gpt-3.5-turbo",
        "temperature": 0.5,
        "shell_timeout": 45.0,
        "context_window_k": 8,
        "audit_threshold": 3,
    }
    cfg = ProfileConfig.model_validate(old_data)
    assert cfg.provider.model_id == "gpt-3.5-turbo"
    assert cfg.provider.temperature == 0.5
    assert cfg.tool.shell_timeout == 45.0
    assert cfg.other.context_window_k == 8
    assert cfg.security.audit_threshold == 3


def test_profile_config_defaults():
    # 测试默认值生成
    data = {"provider": {"model_id": "test"}}
    cfg = ProfileConfig.model_validate(data)
    assert cfg.provider.temperature == 0.7
    assert cfg.security.audit_threshold == 5
    assert cfg.tool.shell_timeout == 30.0
