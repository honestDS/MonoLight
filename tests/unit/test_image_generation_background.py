import json
from types import SimpleNamespace

import pytest

from app.core.tools import image_generation as image_generation_module
from app.core.tools import tool_runs_in_background, tool_schema_has_parameter
from app.core.tools.image_generation import IMAGE_GENERATION_TOOL_SCHEMA, ImageGenerationExecutor
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

    async def fake_submit(_db, **kwargs):
        submitted.update(kwargs)
        return SimpleNamespace(id=42)

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


@pytest.mark.asyncio
async def test_image_generation_releases_database_connection_before_remote_call(monkeypatch):
    commits = []
    generate_calls = []
    generated_protocols = []

    class TrackingSession:
        async def commit(self):
            commits.append("commit")

    async def select_channel(db, *_args, **_kwargs):
        assert db is session
        channel = SimpleNamespace(
            base_url="https://example.invalid",
            get_decrypted_api_key=lambda: "secret",
        )
        model_entry = {
            "model_id": "image-model",
            "usage": "IMAGE_GENERATION",
            "protocol": "OPENAI_IMAGE",
            "size": "1024x1024",
            "quality": "auto",
        }
        return channel, model_entry, SimpleNamespace(priority=1)

    async def generate_image(**kwargs):
        generate_calls.append(list(commits))
        generated_protocols.append(kwargs["protocol"])
        return {
            "model": "image-model",
            "data": [{"b64_json": "aGVsbG8="}],
        }

    async def save_base64_image(_payload):
        return {
            "id": "file-1",
            "name": "image.png",
            "path": "image.png",
            "description": "Generated image",
            "mime_type": "image/png",
            "size": 5,
            "download_url": "/image.png",
            "previewable": True,
        }

    session = TrackingSession()
    executor = ImageGenerationExecutor(project_root=".", uid="user-1")
    executor.set_config(
        ProfileConfig.model_validate(
            {
                "channel": {
                    "image_generation_channel": {
                        "rules": [
                            {
                                "channel_id": 1,
                                "model_id": "image-model",
                                "priority": 1,
                                "weight": 1,
                            }
                        ]
                    }
                }
            }
        )
    )
    executor.set_runtime_context(
        db=session,
        profile=Profile(id=3, uid="user-1", name="profile", configs={}),
        session_id="session-1",
    )
    monkeypatch.setattr(image_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(image_generation_module.ImageGenerationClient, "generate_image", generate_image)
    monkeypatch.setattr(executor, "_save_base64_image", save_base64_image)

    result = json.loads(await executor.execute(prompt="a cat"))

    assert result["status"] == "success"
    assert commits == ["commit"]
    assert generate_calls == [["commit"]]
    assert generated_protocols == ["openai_image"]
