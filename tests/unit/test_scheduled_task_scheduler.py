import pytest
from pydantic import ValidationError

from app.models.profile import ProfileConfig
from app.models.system_setting import SystemRuntimeSettings


def test_task_concurrency_defaults_are_applied_to_existing_profile_configs():
    cfg = ProfileConfig.model_validate({"tool": {}})

    assert cfg.tool.background_task_max_concurrency == 2
    assert cfg.tool.scheduled_task_max_concurrency == 4


def test_session_reply_global_concurrency_default():
    settings = SystemRuntimeSettings()

    assert settings.session_reply_max_concurrency == 4


@pytest.mark.parametrize("value", [0, 101])
def test_session_reply_global_concurrency_range(value):
    with pytest.raises(ValidationError):
        SystemRuntimeSettings(session_reply_max_concurrency=value)
