import json

import pytest
from PIL import Image

from app.core.tools.read_multimodal_file import (
    READ_MULTIMODAL_FILE_TOOL_SCHEMA,
    ReadMultimodalFileExecutor,
    parse_multimodal_file_read_result,
)
from app.models.profile import ProfileConfig


def _build_executor(
    tmp_path,
    *,
    allowed_operation_dirs=None,
):
    executor = ReadMultimodalFileExecutor(project_root=str(tmp_path), uid="user-1")
    executor.set_config(
        ProfileConfig.model_validate(
            {
                "tool": {
                    "allowed_operation_dirs": [str(tmp_path)] if allowed_operation_dirs is None else allowed_operation_dirs,
                }
            }
        )
    )
    return executor


def _write_image(path):
    Image.new("RGB", (2, 2), color=(20, 40, 60)).save(path)


def test_read_multimodal_file_schema_has_only_absolute_path_parameter():
    function = READ_MULTIMODAL_FILE_TOOL_SCHEMA["function"]
    parameters = function["parameters"]

    assert function["name"] == "read_multimodal_file"
    assert set(parameters["properties"]) == {"path"}
    assert parameters["required"] == ["path"]
    assert parameters["additionalProperties"] is False
    assert "absolute" in parameters["properties"]["path"]["description"].lower()


@pytest.mark.asyncio
async def test_read_multimodal_file_reads_valid_image_without_model_capability(tmp_path):
    image_path = tmp_path / "image.png"
    _write_image(image_path)

    executor = _build_executor(tmp_path)
    result = json.loads(await executor.execute(path=str(image_path)))

    assert executor.requires_audit is False
    assert result["type"] == "multimodal_file_read"
    assert result["status"] == "success"
    assert result["modality"] == "image"
    assert result["path"] == str(image_path.resolve())
    assert "不是用户的新输入" in result["message"]


@pytest.mark.asyncio
async def test_read_multimodal_file_rejects_unconfigured_operation_dirs(tmp_path):
    image_path = tmp_path / "image.png"
    _write_image(image_path)

    result = json.loads(await _build_executor(tmp_path, allowed_operation_dirs=[]).execute(path=str(image_path)))

    assert result["status"] == "failed"
    assert "未配置允许工具操作文件的目录" in result["error"]


@pytest.mark.asyncio
async def test_read_multimodal_file_rejects_path_outside_operation_dirs(tmp_path):
    image_path = tmp_path.parent / "outside-image.png"
    _write_image(image_path)

    result = json.loads(await _build_executor(tmp_path).execute(path=str(image_path)))

    assert result["status"] == "failed"
    assert "不在允许工具操作目录内" in result["error"]


@pytest.mark.asyncio
async def test_read_multimodal_file_does_not_claim_to_read_video(tmp_path):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")

    result = json.loads(await _build_executor(tmp_path).execute(path=str(video_path)))

    assert result["status"] == "failed"
    assert "尚未实现本地音频/视频读取" in result["error"]


@pytest.mark.asyncio
async def test_read_multimodal_file_does_not_claim_to_read_audio(tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")

    result = json.loads(await _build_executor(tmp_path).execute(path=str(audio_path)))

    assert result["status"] == "failed"
    assert "尚未实现本地音频/视频读取" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path_factory",
    [
        lambda tmp_path: "image.png",
        lambda tmp_path: tmp_path / "document.pdf",
    ],
)
async def test_read_multimodal_file_rejects_relative_and_unsupported_paths(tmp_path, path_factory):
    path = path_factory(tmp_path)
    if not isinstance(path, str):
        path.write_bytes(b"document")

    result = json.loads(await _build_executor(tmp_path).execute(path=str(path)))

    assert result["status"] == "failed"
    if isinstance(path, str):
        assert "绝对路径" in result["error"]
    else:
        assert "不支持" in result["error"]


@pytest.mark.asyncio
async def test_read_multimodal_file_rejects_corrupted_image(tmp_path):
    image_path = tmp_path / "broken.png"
    image_path.write_bytes(b"not an image")

    result = json.loads(await _build_executor(tmp_path).execute(path=str(image_path)))

    assert result["status"] == "failed"


def test_parse_multimodal_file_read_result_accepts_only_strict_success_absolute_path_payloads(tmp_path):
    path = str((tmp_path / "image.png").resolve())
    valid_payload = {
        "type": "multimodal_file_read",
        "status": "success",
        "modality": "image",
        "path": path,
        "message": "不是用户的新输入",
    }

    assert parse_multimodal_file_read_result(json.dumps(valid_payload, ensure_ascii=False)) == {
        "path": path,
        "modality": "image",
        "message": "不是用户的新输入",
    }

    invalid_payloads = [
        None,
        "not json",
        {**valid_payload, "type": "other"},
        {**valid_payload, "status": "failed"},
        {**valid_payload, "modality": "document"},
        {**valid_payload, "path": "image.png"},
        {key: value for key, value in valid_payload.items() if key != "message"},
        {**valid_payload, "message": 1},
    ]
    for payload in invalid_payloads:
        content = payload if isinstance(payload, str) or payload is None else json.dumps(payload)
        assert parse_multimodal_file_read_result(content) is None
