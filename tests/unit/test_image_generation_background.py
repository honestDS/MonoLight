import json
from types import SimpleNamespace

import pytest

from app.core.tools import tool_runs_in_background, tool_schema_has_parameter
from app.core.tools.image_generation import IMAGE_GENERATION_TOOL_SCHEMA
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.models.profile import Profile, ProfileConfig


def test_image_generation_schema_does_not_accept_background_parameter():
    properties = IMAGE_GENERATION_TOOL_SCHEMA["function"]["parameters"]["properties"]

    assert "run_in_background" not in properties
    assert not tool_schema_has_parameter("generate_image", "run_in_background")
    assert tool_runs_in_background("generate_image")


@pytest.mark.asyncio
async def test_image_generation_is_submitted_in_background_without_parameter(monkeypatch):
    submitted = {}

    async def fake_audit_tool_call(*_args, **_kwargs):
        return None

    async def fake_submit(_db, **kwargs):
        submitted.update(kwargs)
        return SimpleNamespace(id=42)

    monkeypatch.setattr(
        process_single_tool_module,
        "audit_tool_call",
        fake_audit_tool_call,
    )

    from app.core.background_tasks.manager import background_task_manager

    monkeypatch.setattr(background_task_manager, "submit", fake_submit)

    cfg = ProfileConfig.model_validate(
        {
            "tool": {
                "enabled_tools": ["generate_image"],
            }
        }
    )
    profile = Profile(
        id=3,
        uid="user-1",
        name="profile",
        configs=cfg.model_dump(mode="json"),
    )
    tool_call = SimpleNamespace(
        id="call-1",
        name="generate_image",
        arguments={
            "prompt": "a cat",
        },
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
    assert payload["status"] == "queued"
    assert payload["task_id"] == 42
    assert submitted["tool_name"] == "generate_image"
    assert submitted["arguments"] == {"prompt": "a cat"}
