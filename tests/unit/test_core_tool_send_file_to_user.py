import json
from types import SimpleNamespace

import pytest

from app.core.tools import get_tools_for_profile
from app.core.tools.send_file_to_user import SendFileToUserExecutor, resolve_file_token, sanitize_files_to_user_result


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)


def _set_tool_config(executor: SendFileToUserExecutor, **overrides) -> None:
    config = {
        "allowed_file_send_dirs": [],
        "file_send_max_count": 10,
        "file_send_max_single_size_mb": 50,
        "file_send_max_total_size_mb": 100,
        "file_send_blocked_extensions": [],
    }
    config.update(overrides)
    executor.set_config(SimpleNamespace(tool=SimpleNamespace(**config)))


def _set_allowed_dirs(executor: SendFileToUserExecutor, allowed_dirs: list[str]) -> None:
    _set_tool_config(executor, allowed_file_send_dirs=allowed_dirs)


@pytest.mark.asyncio
async def test_send_file_to_user_accepts_absolute_path(tmp_path, encryption_key):
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_allowed_dirs(executor, [str(tmp_path)])

    result_json = await executor.execute(files=[{"path": str(file_path), "display_name": "报告.txt", "description": "测试文件"}])
    result = json.loads(result_json)

    assert result["type"] == "files_to_user"
    assert result["status"] == "success"
    assert "message" not in result
    assert result["allowed_file_send_dirs"] == [str(tmp_path)]
    assert result["errors"] == []
    assert len(result["files"]) == 1
    sent_file = result["files"][0]
    assert sent_file["name"] == "报告.txt"
    assert sent_file["description"] == "测试文件"
    assert sent_file["mime_type"] == "text/plain"
    assert sent_file["size"] == 5
    assert sent_file["previewable"] is True
    assert str(file_path) not in sent_file["download_url"]
    assert resolve_file_token(sent_file["id"]) == file_path.resolve()


@pytest.mark.asyncio
async def test_send_file_to_user_rejects_relative_path(tmp_path, encryption_key):
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_allowed_dirs(executor, [str(tmp_path)])

    result_json = await executor.execute(files=[{"path": "report.txt"}])
    result = json.loads(result_json)

    assert result["status"] == "failed"
    assert result["files"] == []
    assert result["errors"][0]["error"] == "File path must be absolute"


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [None, "D:/safe/report.txt", [{"path": "D:/safe/report.txt"}, "bad"]])
async def test_send_file_to_user_rejects_invalid_files_argument(tmp_path, encryption_key, files):
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_allowed_dirs(executor, [str(tmp_path)])

    result_json = await executor.execute(files=files)
    result = json.loads(result_json)

    assert result == {
        "type": "files_to_user",
        "files": [],
        "status": "failed",
        "errors": [{"path": "", "error": "Invalid files argument. Expected an object or an array of objects."}],
        "allowed_file_send_dirs": [str(tmp_path)],
    }


@pytest.mark.asyncio
async def test_send_file_to_user_rejects_sensitive_file(tmp_path, encryption_key):
    file_path = tmp_path / ".env"
    file_path.write_text("SECRET=1", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_allowed_dirs(executor, [str(tmp_path)])

    result_json = await executor.execute(files=[{"path": str(file_path)}])
    result = json.loads(result_json)

    assert result["status"] == "failed"
    assert result["files"] == []
    assert result["errors"][0]["error"] == "Sensitive file is not allowed"


@pytest.mark.asyncio
async def test_send_file_to_user_partial_success(tmp_path, encryption_key):
    valid_file = tmp_path / "ok.txt"
    valid_file.write_text("ok", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_allowed_dirs(executor, [str(tmp_path)])

    result_json = await executor.execute(files=[{"path": str(valid_file)}, {"path": "bad.txt"}])
    result = json.loads(result_json)

    assert result["status"] == "partial_success"
    assert len(result["files"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["error"] == "File path must be absolute"


@pytest.mark.asyncio
async def test_send_file_to_user_uses_configured_allowed_dirs_as_whitelist(tmp_path, encryption_key):
    allowed_dir = tmp_path / "allowed"
    blocked_dir = tmp_path / "blocked"
    allowed_dir.mkdir()
    blocked_dir.mkdir()
    allowed_file = allowed_dir / "custom.txt"
    blocked_file = blocked_dir / "blocked.txt"
    allowed_file.write_text("custom", encoding="utf-8")
    blocked_file.write_text("blocked", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_allowed_dirs(executor, [str(allowed_dir)])

    result_json = await executor.execute(files=[{"path": str(allowed_file)}, {"path": str(blocked_file)}])
    result = json.loads(result_json)

    assert result["status"] == "partial_success"
    assert len(result["files"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == str(blocked_file)
    assert result["errors"][0]["error"] == "File path is outside allowed directories"
    assert result["allowed_file_send_dirs"] == [str(allowed_dir)]


@pytest.mark.asyncio
async def test_send_file_to_user_fails_when_allowed_dirs_empty(tmp_path, encryption_key):
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")

    result_json = await executor.execute(files=[{"path": str(file_path)}])
    result = json.loads(result_json)

    assert result["status"] == "failed"
    assert result["files"] == []
    assert result["allowed_file_send_dirs"] == []
    assert "No allowed file sending directories are configured" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_send_file_to_user_blocks_configured_extension(tmp_path, encryption_key):
    file_path = tmp_path / "public.pem"
    file_path.write_text("public content", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_tool_config(executor, allowed_file_send_dirs=[str(tmp_path)], file_send_blocked_extensions=["pem"])

    result_json = await executor.execute(files=[{"path": str(file_path)}])
    result = json.loads(result_json)

    assert result["status"] == "failed"
    assert result["files"] == []
    assert result["errors"][0]["error"] == "File extension is blocked"


@pytest.mark.asyncio
async def test_send_file_to_user_uses_configured_file_count_limit(tmp_path, encryption_key):
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    first_file.write_text("ok", encoding="utf-8")
    second_file.write_text("ok", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_tool_config(executor, allowed_file_send_dirs=[str(tmp_path)], file_send_max_count=1, file_send_max_single_size_mb=1, file_send_max_total_size_mb=1)

    result_json = await executor.execute(files=[{"path": str(first_file)}, {"path": str(second_file)}])
    result = json.loads(result_json)

    assert result["status"] == "partial_success"
    assert len(result["files"]) == 1
    assert result["errors"][0]["error"] == "Only the first 1 files are processed"


@pytest.mark.asyncio
async def test_send_file_to_user_uses_configured_single_file_size_limit(tmp_path, encryption_key):
    file_path = tmp_path / "large.txt"
    file_path.write_text("large", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_tool_config(executor, allowed_file_send_dirs=[str(tmp_path)], file_send_max_single_size_mb=1 / 1024 / 1024)

    result_json = await executor.execute(files=[{"path": str(file_path)}])
    result = json.loads(result_json)

    assert result["status"] == "failed"
    assert result["files"] == []
    assert result["errors"][0]["error"] == "File exceeds the single file size limit"


@pytest.mark.asyncio
async def test_send_file_to_user_uses_configured_total_file_size_limit(tmp_path, encryption_key):
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    first_file.write_text("a", encoding="utf-8")
    second_file.write_text("bb", encoding="utf-8")
    executor = SendFileToUserExecutor(project_root=str(tmp_path), uid="test_user")
    _set_tool_config(executor, allowed_file_send_dirs=[str(tmp_path)], file_send_max_single_size_mb=1, file_send_max_total_size_mb=1 / 1024 / 1024)

    result_json = await executor.execute(files=[{"path": str(first_file)}, {"path": str(second_file)}])
    result = json.loads(result_json)

    assert result["status"] == "partial_success"
    assert len(result["files"]) == 1
    assert result["errors"][0]["error"] == "Files exceed the total size limit"


def test_sanitize_files_to_user_result_returns_only_failure_summary():
    content = json.dumps(
        {
            "type": "files_to_user",
            "files": [],
            "status": "failed",
            "errors": [{"path": "D:/safe/missing.txt", "error": "File not found"}],
            "allowed_file_send_dirs": ["D:/safe"],
        },
        ensure_ascii=False,
    )

    sanitized = json.loads(sanitize_files_to_user_result(content))

    assert sanitized == {
        "type": "files_to_user_result",
        "status": "failed",
        "message": "File sending failed. Ask the user to check the file paths and profile file sending whitelist before retrying.",
    }


def test_sanitize_files_to_user_result_returns_only_success_summary():
    content = json.dumps(
        {
            "type": "files_to_user",
            "files": [{"id": "token", "name": "report.txt", "download_url": "/api/v1/download-sent?token=token"}],
            "status": "success",
            "errors": [],
            "allowed_file_send_dirs": ["D:/safe"],
        },
        ensure_ascii=False,
    )

    sanitized = json.loads(sanitize_files_to_user_result(content))

    assert sanitized["type"] == "files_to_user_result"
    assert sanitized["status"] == "success"
    assert "automatically appended after your assistant reply" in sanitized["message"]
    assert "download_url" not in sanitized
    assert "allowed_file_send_dirs" not in sanitized


@pytest.mark.asyncio
async def test_get_tools_for_profile_respects_enabled_tools():
    profile = SimpleNamespace(id=1, configs={"tool": {"enabled_tools": ["send_file_to_user"]}})

    tools, whitelist = await get_tools_for_profile(None, profile, embedding_profile_available=False)

    assert [tool["function"]["name"] for tool in tools] == ["send_file_to_user"]
    assert whitelist == []


@pytest.mark.asyncio
async def test_get_tools_for_profile_allows_disabling_all_tools():
    profile = SimpleNamespace(id=1, configs={"tool": {"enabled_tools": []}})

    tools, whitelist = await get_tools_for_profile(None, profile, embedding_profile_available=False)

    assert tools == []
    assert whitelist == []
