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
