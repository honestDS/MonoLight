import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.core.constants import (
    ERR_FILE_EXTENSION_BLOCKED,
    ERR_FILE_NOT_FOUND,
    ERR_FILE_PATH_NOT_ABSOLUTE,
    ERR_FILE_SENSITIVE_NOT_ALLOWED,
    ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED,
    ERR_FILE_TOOL_NOT_REGULAR,
    ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED,
    ERR_LLM_IMAGE_OUTPUT_INVALID,
    ERR_TOOL_IMAGE_EMPTY_RESPONSE,
    ERR_TOOL_IMAGE_REFERENCE_INVALID,
    ERR_TOOL_OPERATION_DIRS_UNCONFIGURED,
    ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS,
    ERR_TOOL_UNSUPPORTED_ARGUMENTS,
    IMAGE_GENERATION_MAX_REFERENCE_IMAGES,
)
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.tools import image_generation as image_generation_module
from app.core.tools import tool_runs_in_background, tool_schema_has_parameter
from app.core.tools.image_generation import (
    IMAGE_GENERATION_TOOL_SCHEMA,
    ImageGenerationExecutor,
)
from app.core.utils.dispatcher import (
    process_single_tool as process_single_tool_module,
)
from app.models.profile import Profile, ProfileConfig


def _image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (3, 2), color=(12, 34, 56))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _cfg_with_tool_updates(cfg: ProfileConfig, **updates: object) -> ProfileConfig:
    return cfg.model_copy(update={"tool": cfg.tool.model_copy(update=updates)})


def _assert_failed(result: str, error_key: str) -> None:
    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert payload["error"] == t(error_key)


def _install_fallback_clients(
    image_generation_executor,
    monkeypatch,
    primary_action,
    secondary_action,
    *,
    after_primary=None,
):
    channel = SimpleNamespace(
        base_url="https://example.invalid",
        get_decrypted_api_key=lambda: "secret",
    )
    primary_model = {
        "model_id": "primary-model",
        "usage": "IMAGE_GENERATION",
        "protocol": "OPENAI_IMAGE",
        "size": "1024x1024",
        "quality": "auto",
    }
    secondary_model = {
        "model_id": "secondary-model",
        "usage": "IMAGE_GENERATION",
        "protocol": "OPENAI_IMAGE",
        "size": "1536x1024",
        "quality": "low",
    }

    async def fake_select_channel(*args, **kwargs):
        image_generation_executor.channel_calls.append((args, kwargs))
        excluded_priorities = kwargs["excluded_priorities"]
        if 1 not in excluded_priorities:
            return channel, primary_model, SimpleNamespace(priority=1)
        if 2 not in excluded_priorities:
            return channel, secondary_model, SimpleNamespace(priority=2)
        return None

    async def fake_generate_image(**kwargs):
        image_generation_executor.remote_calls.append(kwargs)
        if kwargs["model_id"] == "primary-model":
            if after_primary is not None:
                after_primary()
            return primary_action(kwargs)
        return secondary_action(kwargs)

    monkeypatch.setattr(image_generation_module, "select_channel", fake_select_channel)
    monkeypatch.setattr(
        image_generation_module.ImageGenerationClient,
        "generate_image",
        fake_generate_image,
    )


@pytest.fixture
def image_generation_executor(tmp_path, monkeypatch):
    cfg = ProfileConfig.model_validate(
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
            },
            "tool": {"allowed_operation_dirs": [str(tmp_path)]},
        }
    )
    profile = Profile(
        id=3,
        uid="user-1",
        name="profile",
        configs=cfg.model_dump(mode="json"),
    )
    channel_calls = []
    remote_calls = []
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

    async def fake_select_channel(*args, **kwargs):
        channel_calls.append((args, kwargs))
        return channel, model_entry, SimpleNamespace(priority=1)

    generated_bytes = _image_bytes("PNG")

    async def fake_generate_image(**kwargs):
        remote_calls.append(kwargs)
        return {
            "model": "image-model",
            "data": [{"b64_json": base64.b64encode(generated_bytes).decode("ascii")}],
        }

    async def commit():
        return None

    executor = ImageGenerationExecutor(project_root=str(tmp_path), uid="user-1")
    executor.set_config(cfg)
    executor.set_runtime_context(
        db=SimpleNamespace(commit=commit),
        profile=profile,
        session_id="session-1",
    )
    monkeypatch.setattr(image_generation_module, "select_channel", fake_select_channel)
    monkeypatch.setattr(
        image_generation_module.ImageGenerationClient,
        "generate_image",
        fake_generate_image,
    )
    monkeypatch.setattr(
        image_generation_module,
        "_encode_token",
        lambda _payload: "test-token",
    )

    return SimpleNamespace(
        cfg=cfg,
        executor=executor,
        channel_calls=channel_calls,
        remote_calls=remote_calls,
    )


def test_image_generation_schema_does_not_accept_background_parameter():
    properties = IMAGE_GENERATION_TOOL_SCHEMA["function"]["parameters"]["properties"]

    assert "run_in_background" not in properties
    assert not tool_schema_has_parameter("generate_image", "run_in_background")
    assert tool_runs_in_background("generate_image")


@pytest.mark.asyncio
async def test_image_generation_is_submitted_in_background_without_parameter(
    monkeypatch,
):
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
async def test_image_generation_releases_database_connection_before_remote_call(
    monkeypatch,
):
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
    monkeypatch.setattr(
        image_generation_module.ImageGenerationClient,
        "generate_image",
        generate_image,
    )
    monkeypatch.setattr(executor, "_save_base64_image", save_base64_image)

    result = json.loads(await executor.execute(prompt="a cat"))

    assert result["status"] == "success"
    assert commits == ["commit"]
    assert generate_calls == [["commit"]]
    assert generated_protocols == ["openai_image"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_name", "image_format", "expected_name", "expected_mime"),
    [
        ("reference.png", "PNG", "reference.png", "image/png"),
        ("reference.jpeg", "JPEG", "reference.jpg", "image/jpeg"),
        ("reference.webp", "WEBP", "reference.webp", "image/webp"),
        ("reference.jpg", "PNG", "reference.png", "image/png"),
    ],
)
async def test_image_generation_forwards_valid_reference_bytes(
    image_generation_executor,
    tmp_path,
    source_name,
    image_format,
    expected_name,
    expected_mime,
):
    image_bytes = _image_bytes(image_format)
    source_path = tmp_path / source_name
    source_path.write_bytes(image_bytes)

    result = await image_generation_executor.executor.execute(
        prompt="a cat",
        reference_images=[str(source_path)],
    )

    payload = json.loads(result)
    assert payload["status"] == "success"
    assert len(image_generation_executor.remote_calls) == 1
    assert image_generation_executor.remote_calls[0]["reference_images"] == [
        (expected_name, image_bytes, expected_mime),
    ]
    assert "mask_path" not in image_generation_executor.remote_calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("references", [None, []], ids=["none", "empty"])
async def test_image_generation_without_references_keeps_text_only_call(
    image_generation_executor,
    references,
):
    result = await image_generation_executor.executor.execute(
        prompt="a cat",
        reference_images=references,
    )

    payload = json.loads(result)
    assert payload["status"] == "success"
    assert len(image_generation_executor.remote_calls) == 1
    assert image_generation_executor.remote_calls[0]["prompt"] == "a cat"
    assert "reference_images" not in image_generation_executor.remote_calls[0]
    assert "mask_path" not in image_generation_executor.remote_calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "references",
    [
        "reference.png",
        {"path": "reference.png"},
        [1],
        [None],
        [""],
        ["reference.png", 1],
    ],
    ids=["string", "dict", "non-string", "none-item", "empty-item", "mixed-items"],
)
async def test_image_generation_rejects_invalid_reference_arguments_before_remote(
    image_generation_executor,
    references,
):
    result = await image_generation_executor.executor.execute(
        prompt="a cat",
        reference_images=references,
    )

    _assert_failed(result, ERR_TOOL_IMAGE_REFERENCE_INVALID)
    assert image_generation_executor.channel_calls == []
    assert image_generation_executor.remote_calls == []


@pytest.mark.asyncio
async def test_image_generation_rejects_too_many_references_before_remote(
    image_generation_executor,
    tmp_path,
):
    references = [str(tmp_path / f"reference-{index}.png") for index in range(IMAGE_GENERATION_MAX_REFERENCE_IMAGES + 1)]

    result = await image_generation_executor.executor.execute(
        prompt="a cat",
        reference_images=references,
    )

    _assert_failed(result, ERR_TOOL_IMAGE_REFERENCE_INVALID)
    assert image_generation_executor.channel_calls == []
    assert image_generation_executor.remote_calls == []


@pytest.mark.asyncio
async def test_image_generation_rejects_invalid_reference_paths_before_remote(
    image_generation_executor,
    tmp_path,
):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir(exist_ok=True)
    outside_path = outside_dir / "outside.png"
    outside_path.write_bytes(_image_bytes("PNG"))

    allowed_path = tmp_path / "allowed.png"
    allowed_path.write_bytes(_image_bytes("PNG"))
    missing_path = tmp_path / "missing.png"
    directory_path = tmp_path / "directory"
    directory_path.mkdir()
    invalid_path = tmp_path / "invalid.png"
    invalid_path.write_bytes(b"not an image")
    gif_path = tmp_path / "animated.gif"
    gif_path.write_bytes(_image_bytes("GIF"))
    bmp_path = tmp_path / "unsupported.bmp"
    bmp_path.write_bytes(_image_bytes("BMP"))
    sensitive_dir = tmp_path / "secrets"
    sensitive_dir.mkdir()
    sensitive_path = sensitive_dir / "image.png"
    sensitive_path.write_bytes(_image_bytes("PNG"))
    blocked_path = tmp_path / "blocked.png"
    blocked_path.write_bytes(_image_bytes("PNG"))

    unconfigured_cfg = _cfg_with_tool_updates(image_generation_executor.cfg, allowed_operation_dirs=[])
    blocked_cfg = _cfg_with_tool_updates(
        image_generation_executor.cfg,
        file_send_blocked_extensions=[".png"],
    )
    cases = [
        (
            str(outside_path),
            ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS,
            image_generation_executor.cfg,
        ),
        (
            str(allowed_path),
            ERR_TOOL_OPERATION_DIRS_UNCONFIGURED,
            unconfigured_cfg,
        ),
        (
            "allowed.png",
            ERR_FILE_PATH_NOT_ABSOLUTE,
            image_generation_executor.cfg,
        ),
        (str(missing_path), ERR_FILE_NOT_FOUND, image_generation_executor.cfg),
        (
            str(directory_path),
            ERR_FILE_TOOL_NOT_REGULAR,
            image_generation_executor.cfg,
        ),
        (
            str(invalid_path),
            ERR_TOOL_IMAGE_REFERENCE_INVALID,
            image_generation_executor.cfg,
        ),
        (
            str(gif_path),
            ERR_TOOL_IMAGE_REFERENCE_INVALID,
            image_generation_executor.cfg,
        ),
        (
            str(bmp_path),
            ERR_TOOL_IMAGE_REFERENCE_INVALID,
            image_generation_executor.cfg,
        ),
        (
            str(sensitive_path),
            ERR_FILE_SENSITIVE_NOT_ALLOWED,
            image_generation_executor.cfg,
        ),
        (str(blocked_path), ERR_FILE_EXTENSION_BLOCKED, blocked_cfg),
    ]

    for raw_path, error_key, cfg in cases:
        image_generation_executor.executor.set_config(cfg)
        result = await image_generation_executor.executor.execute(
            prompt="a cat",
            reference_images=[raw_path],
        )

        _assert_failed(result, error_key)
        assert image_generation_executor.channel_calls == []
        assert image_generation_executor.remote_calls == []


@pytest.mark.asyncio
async def test_image_generation_rejects_symlink_outside_allowed_directory(
    image_generation_executor,
    tmp_path,
):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-symlink-outside"
    outside_dir.mkdir(exist_ok=True)
    outside_path = outside_dir / "outside.png"
    outside_path.write_bytes(_image_bytes("PNG"))
    link_path = tmp_path / "linked.png"
    try:
        link_path.symlink_to(outside_path)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable on this platform")

    result = await image_generation_executor.executor.execute(
        prompt="a cat",
        reference_images=[str(link_path)],
    )

    _assert_failed(result, ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS)
    assert image_generation_executor.channel_calls == []
    assert image_generation_executor.remote_calls == []


@pytest.mark.asyncio
async def test_image_generation_rejects_reference_exceeding_single_size_limit(
    image_generation_executor,
    tmp_path,
):
    image_bytes = _image_bytes("PNG")
    image_path = tmp_path / "large-enough.png"
    image_path.write_bytes(image_bytes)
    single_limit_bytes = max(1, len(image_bytes) // 2)
    limited_cfg = _cfg_with_tool_updates(
        image_generation_executor.cfg,
        file_send_max_single_size_mb=single_limit_bytes / (1024 * 1024),
    )
    image_generation_executor.executor.set_config(limited_cfg)

    result = await image_generation_executor.executor.execute(
        prompt="a cat",
        reference_images=[str(image_path)],
    )

    _assert_failed(result, ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED)
    assert image_generation_executor.channel_calls == []
    assert image_generation_executor.remote_calls == []


@pytest.mark.asyncio
async def test_image_generation_rejects_references_exceeding_total_size_limit(
    image_generation_executor,
    tmp_path,
):
    image_bytes = _image_bytes("PNG")
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(image_bytes)
    second_path.write_bytes(image_bytes)
    limited_cfg = _cfg_with_tool_updates(
        image_generation_executor.cfg,
        file_send_max_single_size_mb=(len(image_bytes) + 1) / (1024 * 1024),
        file_send_max_total_size_mb=(len(image_bytes) * 2 - 1) / (1024 * 1024),
    )
    image_generation_executor.executor.set_config(limited_cfg)

    result = await image_generation_executor.executor.execute(
        prompt="a cat",
        reference_images=[str(first_path), str(second_path)],
    )

    _assert_failed(result, ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED)
    assert image_generation_executor.channel_calls == []
    assert image_generation_executor.remote_calls == []


@pytest.mark.parametrize("field", ["mask_path", "backend", "protocol", "action"])
def test_image_generation_prevalidation_rejects_undeclared_parameters(
    image_generation_executor,
    field,
):
    tool_call = SimpleNamespace(
        id=f"call-{field}",
        name="generate_image",
        arguments={
            "prompt": "a cat",
            "reference_images": [],
            field: "unsupported",
        },
    )

    errors = process_single_tool_module.prevalidate_tool_round(
        [tool_call],
        image_generation_executor.cfg,
    )
    payload = json.loads(errors[tool_call.id])

    assert payload["status"] == "failed"
    assert payload["error"] == t(
        ERR_TOOL_UNSUPPORTED_ARGUMENTS,
        tool_name="generate_image",
        fields=field,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary_failure",
    [
        "empty_data",
        "non_list_data",
        "missing_image_fields",
        "llm_exception",
        "invalid_base64",
        "non_image_base64",
    ],
)
async def test_image_generation_falls_back_after_primary_failure(
    image_generation_executor,
    monkeypatch,
    tmp_path,
    primary_failure,
):
    first_bytes = _image_bytes("PNG")
    second_bytes = _image_bytes("JPEG")
    first_path = tmp_path / "reference.png"
    second_path = tmp_path / "reference.jpg"
    first_path.write_bytes(first_bytes)
    second_path.write_bytes(second_bytes)
    generated_bytes = _image_bytes("PNG")

    def primary_action(_kwargs):
        if primary_failure == "empty_data":
            return {"model": "primary-model", "data": []}
        if primary_failure == "non_list_data":
            return {"model": "primary-model", "data": {}}
        if primary_failure == "missing_image_fields":
            return {"model": "primary-model", "data": [{}]}
        if primary_failure == "llm_exception":
            raise LLMException(ERR_TOOL_IMAGE_EMPTY_RESPONSE)
        if primary_failure == "invalid_base64":
            return {"model": "primary-model", "data": [{"b64_json": "%%%"}]}
        return {
            "model": "primary-model",
            "data": [
                {
                    "b64_json": base64.b64encode(b"not an image").decode("ascii"),
                }
            ],
        }

    def secondary_action(_kwargs):
        return {
            "model": "secondary-model",
            "data": [{"b64_json": base64.b64encode(generated_bytes).decode("ascii")}],
        }

    def rewrite_sources():
        first_path.write_bytes(_image_bytes("WEBP"))
        second_path.write_bytes(_image_bytes("PNG"))

    _install_fallback_clients(
        image_generation_executor,
        monkeypatch,
        primary_action,
        secondary_action,
        after_primary=rewrite_sources,
    )
    expected_references = [
        ("reference.png", first_bytes, "image/png"),
        ("reference.jpg", second_bytes, "image/jpeg"),
    ]

    result = await image_generation_executor.executor.execute(
        prompt="a fallback cat",
        size="1024x1536",
        quality="high",
        reference_images=[str(first_path), str(second_path)],
    )

    payload = json.loads(result)
    assert payload["status"] == "success"
    output_files = payload["send_file_to_user"]["files"]
    assert len(output_files) == 1
    output_file = output_files[0]
    assert output_file["mime_type"] == "image/png"
    assert Path(output_file["path"]).read_bytes() == generated_bytes

    assert len(image_generation_executor.remote_calls) == 2
    assert [call["protocol"] for call in image_generation_executor.remote_calls] == [
        "openai_image",
        "openai_image",
    ]
    assert [call["prompt"] for call in image_generation_executor.remote_calls] == [
        "a fallback cat",
        "a fallback cat",
    ]
    assert [call["size"] for call in image_generation_executor.remote_calls] == [
        "1024x1536",
        "1024x1536",
    ]
    assert [call["quality"] for call in image_generation_executor.remote_calls] == [
        "high",
        "high",
    ]
    assert [call["reference_images"] for call in image_generation_executor.remote_calls] == [
        expected_references,
        expected_references,
    ]


@pytest.mark.asyncio
async def test_image_generation_returns_last_localized_error_after_channels_exhausted(
    image_generation_executor,
    monkeypatch,
):
    def primary_action(_kwargs):
        return {"model": "primary-model", "data": []}

    def secondary_action(_kwargs):
        return {
            "model": "secondary-model",
            "data": [{"b64_json": "%%%"}],
        }

    _install_fallback_clients(
        image_generation_executor,
        monkeypatch,
        primary_action,
        secondary_action,
    )

    result = await image_generation_executor.executor.execute(
        prompt="an exhausted cat",
    )

    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert payload["error"] == t(ERR_LLM_IMAGE_OUTPUT_INVALID)
    assert "send_file_to_user" not in payload
    assert len(image_generation_executor.remote_calls) == 2


@pytest.mark.asyncio
async def test_image_generation_input_precheck_does_not_fallback(
    image_generation_executor,
    monkeypatch,
    tmp_path,
):
    def primary_action(_kwargs):
        return {"model": "primary-model", "data": []}

    def secondary_action(_kwargs):
        return {"model": "secondary-model", "data": []}

    _install_fallback_clients(
        image_generation_executor,
        monkeypatch,
        primary_action,
        secondary_action,
    )

    result = await image_generation_executor.executor.execute(
        prompt="a precheck cat",
        reference_images=[str(tmp_path / "missing.png")],
    )

    _assert_failed(result, ERR_FILE_NOT_FOUND)
    assert image_generation_executor.channel_calls == []
    assert image_generation_executor.remote_calls == []
