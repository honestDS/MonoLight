import json
from types import SimpleNamespace

import pytest

from app.core.tools import IMAGE_GENERATION_TOOL_SCHEMA, LIST_BACKGROUND_TASKS_TOOL_SCHEMA, SEND_FILE_TO_USER_TOOL_SCHEMA, get_tool_required_parameters
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


@pytest.mark.parametrize(
    ("tool_name", "arguments", "schema", "expected_detail"),
    [
        ("list_background_tasks", {"page": "1"}, LIST_BACKGROUND_TASKS_TOOL_SCHEMA, "must be integer"),
        ("list_background_tasks", {"size": 101}, LIST_BACKGROUND_TASKS_TOOL_SCHEMA, "must be at most 100"),
        ("generate_image", {"prompt": "image", "quality": "ultra"}, IMAGE_GENERATION_TOOL_SCHEMA, "must be one of"),
        ("send_file_to_user", {"files": [{"display_name": "x"}]}, SEND_FILE_TO_USER_TOOL_SCHEMA, "arguments.files[0].path is required"),
    ],
)
def test_prevalidate_tool_round_rejects_full_schema_violations(tool_name, arguments, schema, expected_detail):
    cfg = ProfileConfig.model_validate({"tool": {"enabled_tools": [tool_name]}})
    tool_call = SimpleNamespace(id="call-1", name=tool_name, arguments=arguments)

    errors = process_single_tool_module.prevalidate_tool_round([tool_call], cfg, tool_schemas=[schema])

    payload = json.loads(errors[tool_call.id])
    assert payload["status"] == "failed"
    assert expected_detail in payload["error"]


def test_prevalidate_tool_round_uses_runtime_dynamic_enum():
    cfg = ProfileConfig.model_validate({"tool": {"enabled_tools": ["query_knowledge_base"]}})
    dynamic_schema = {
        "type": "function",
        "function": {
            "name": "query_knowledge_base",
            "parameters": {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string", "enum": ["7"]},
                    "query": {"type": "string"},
                },
                "required": ["knowledge_base_id", "query"],
                "additionalProperties": False,
            },
        },
    }
    tool_call = SimpleNamespace(id="call-1", name="query_knowledge_base", arguments={"knowledge_base_id": "8", "query": "test"})

    errors = process_single_tool_module.prevalidate_tool_round([tool_call], cfg, tool_schemas=[dynamic_schema])

    assert "must be one of ['7']" in json.loads(errors[tool_call.id])["error"]
