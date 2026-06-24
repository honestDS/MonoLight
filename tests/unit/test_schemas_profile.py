from app.core.i18n import t
from app.core.i18n.context import reset_current_log_locale, set_current_log_locale
from app.core.i18n.locale import DEFAULT_LOCALE, get_available_locales
from app.core.log import profile_log_locale, reset_profile_log_locale, set_profile_log_locale
from app.models.profile import Profile, ProfileConfig


def test_profile_config_standardization():
    old_data = {
        "chat_channel": {
            "chat_timeout": 45.0,
            "rules": [
                {"channel_id": 1, "model_id": "gpt-4o", "priority": 1, "weight": 1},
            ],
        },
        "tool_timeout": 45.0,
        "enabled_tools": ["send_file_to_user"],
        "allowed_file_send_dirs": ["D:/safe"],
        "file_send_max_count": 3,
        "file_send_max_single_size_mb": 8,
        "file_send_max_total_size_mb": 16,
        "file_send_blocked_extensions": [".pem"],
        "audit_threshold": 3,
    }
    cfg = ProfileConfig.model_validate(old_data)
    assert cfg.channel.chat_channel.chat_timeout == 45.0
    assert cfg.channel.chat_channel.rules[0].model_id == "gpt-4o"
    assert cfg.tool.tool_timeout == 45.0
    assert cfg.tool.enabled_tools == ["send_file_to_user"]
    assert cfg.tool.allowed_file_send_dirs == ["D:/safe"]
    assert cfg.tool.file_send_max_count == 3
    assert cfg.tool.file_send_max_single_size_mb == 8
    assert cfg.tool.file_send_max_total_size_mb == 16
    assert cfg.tool.file_send_blocked_extensions == [".pem"]
    assert cfg.security.audit_threshold == 3


def test_profile_config_defaults():
    data = {"channel": {"chat_channel": {"rules": []}}}
    cfg = ProfileConfig.model_validate(data)
    assert cfg.channel.chat_channel.chat_timeout == 60.0
    assert cfg.security.audit_threshold == 5
    assert cfg.tool.tool_timeout == 30.0
    assert "send_file_to_user" in cfg.tool.enabled_tools
    assert "query_knowledge_base" in cfg.tool.enabled_tools
    assert cfg.tool.allowed_file_send_dirs == []
    assert cfg.tool.file_send_max_count == 10
    assert cfg.tool.file_send_max_single_size_mb == 50
    assert cfg.tool.file_send_max_total_size_mb == 100
    assert cfg.tool.file_send_blocked_extensions == []
    assert cfg.other.log_locale == DEFAULT_LOCALE


def test_profile_config_log_locale_normalization():
    cfg = ProfileConfig.model_validate({"channel": {"chat_channel": {"rules": []}}, "other": {"log_locale": "en-US"}})
    assert cfg.other.log_locale == "en"

    fallback_cfg = ProfileConfig.model_validate({"channel": {"chat_channel": {"rules": []}}, "other": {"log_locale": "missing"}})
    assert fallback_cfg.other.log_locale == DEFAULT_LOCALE


def test_available_locales_from_backend_i18n_directories():
    locales = get_available_locales()
    assert "zh" in locales
    assert "en" in locales


def test_log_translation_uses_log_locale_context():
    token = set_current_log_locale("en")
    try:
        assert t("LOG_DISPATCHER_ERROR") == "Dispatcher error"
    finally:
        reset_current_log_locale(token)


def test_profile_log_locale_context_restores_previous_locale():
    profile = Profile(name="test", configs={"channel": {}, "security": {}, "tool": {}, "other": {"log_locale": "en"}})
    token = set_current_log_locale("zh")
    try:
        with profile_log_locale(profile):
            assert t("LOG_DISPATCHER_ERROR") == "Dispatcher error"
        assert t("LOG_DISPATCHER_ERROR") == "调度器错误"
    finally:
        reset_current_log_locale(token)


def test_set_profile_log_locale_requires_explicit_reset():
    profile = Profile(name="test", configs={"channel": {}, "security": {}, "tool": {}, "other": {"log_locale": "en"}})
    token = set_current_log_locale("zh")
    try:
        profile_token = set_profile_log_locale(profile)
        try:
            assert t("LOG_DISPATCHER_ERROR") == "Dispatcher error"
        finally:
            reset_profile_log_locale(profile_token)

        assert t("LOG_DISPATCHER_ERROR") == "调度器错误"
    finally:
        reset_current_log_locale(token)
