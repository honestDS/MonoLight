import json
from types import SimpleNamespace

import pytest

from app.core.tools import get_tool_required_parameters
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.models.profile import Profile, ProfileConfig


def test_get_tool_required_parameters_reads_registered_schema():
    assert get_tool_required_parameters("execute_shell") == ["command"]
    assert get_tool_required_parameters("unknown_tool") == []


@pytest.mark.asyncio
async def test_process_single_tool_returns_failure_for_missing_required_arguments(monkeypatch):
    async def unexpected_audit(*_args, **_kwargs):
        pytest.fail("缺少必填参数时不应进入工具审计")

    monkeypatch.setattr(process_single_tool_module, "audit_tool_call", unexpected_audit)

    cfg = ProfileConfig.model_validate(
        {
            "tool": {
                "enabled_tools": ["execute_shell"],
            }
        }
    )
    profile = Profile(
        id=1,
        uid="user-1",
        name="profile",
        configs=cfg.model_dump(mode="json"),
    )
    tool_call = SimpleNamespace(
        id="call-1",
        name="execute_shell",
        arguments={},
    )

    result = await process_single_tool_module.process_single_tool(
        tool_call,
        db=SimpleNamespace(),
        profile=profile,
        cfg=cfg,
        messages=[],
        username="user",
        session_id="session-1",
        turn=1,
        uid="user-1",
    )

    payload = json.loads(result.content)
    assert payload == {
        "status": "failed",
        "tool_name": "execute_shell",
        "error": "缺少必填参数: command",
        "missing_arguments": ["command"],
    }
    assert result.tool_call_id == "call-1"
