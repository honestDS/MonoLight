import json
import os
from types import SimpleNamespace

import pytest

from app.core.tools.file_tool import FILE_TOOL_SCHEMA, FileToolExecutor
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.models.profile import Profile, ProfileConfig


def _executor(tmp_path, *, allowed_operation_dirs=None):
    executor = FileToolExecutor(project_root=str(tmp_path), uid="u1")
    executor.set_config(
        SimpleNamespace(
            tool=SimpleNamespace(
                allowed_operation_dirs=[str(tmp_path)] if allowed_operation_dirs is None else allowed_operation_dirs,
            )
        )
    )
    return executor


def test_file_tool_schema_exposes_general_file_operations():
    function = FILE_TOOL_SCHEMA["function"]

    assert function["name"] == "file_tool"
    assert function["parameters"]["properties"]["operation"]["enum"] == ["read", "write", "replace", "find"]
    assert function["parameters"]["required"] == ["operation", "path"]


def test_profile_config_migrates_legacy_write_file_enablement():
    cfg = ProfileConfig.model_validate({"tool": {"enabled_tools": ["execute_shell", "write_file", "file_tool"]}})

    assert cfg.tool.enabled_tools == ["execute_shell", "file_tool"]


@pytest.mark.asyncio
async def test_file_tool_read_requires_explicit_line_range_and_returns_requested_page(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    executor = _executor(tmp_path)

    missing_range = json.loads(await executor.execute(operation="read", path=str(source)))
    result = json.loads(await executor.execute(operation="read", path=str(source), start_line=2, end_line=3))

    assert missing_range["status"] == "failed"
    assert "start_line" in missing_range["error"]
    assert "end_line" in missing_range["error"]
    assert result == {
        "status": "success",
        "operation": "read",
        "path": str(source.resolve()),
        "start_line": 2,
        "end_line": 3,
        "total_lines": 4,
        "content": "2: two\n3: three",
    }


@pytest.mark.asyncio
async def test_file_tool_write_is_full_overwrite_and_preserves_relative_workspace_compatibility(tmp_path):
    executor = _executor(tmp_path, allowed_operation_dirs=[])
    target = executor.user_temp_dir / "nested" / "result.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old content", encoding="utf-8")

    result = json.loads(await executor.execute(operation="write", path="nested/result.txt", content="new content"))

    assert result["status"] == "success"
    assert result["operation"] == "write"
    assert target.read_text(encoding="utf-8") == "new content"


@pytest.mark.asyncio
async def test_file_tool_replace_requires_exact_target_count_and_does_not_write_on_mismatch(tmp_path):
    target = tmp_path / "replace.txt"
    target.write_text("alpha = 1\nalpha = 2\n", encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="replace",
            path=str(target),
            pattern=r"alpha = \d",
            replacement="beta = 3",
            expected_replacements=1,
        )
    )

    assert result["status"] == "failed"
    assert result["matched"] == 2
    assert target.read_text(encoding="utf-8") == "alpha = 1\nalpha = 2\n"


@pytest.mark.asyncio
async def test_file_tool_replace_reports_replaced_target_lines(tmp_path):
    target = tmp_path / "replace.txt"
    target.write_text("header\nvalue = old\ntail\n", encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="replace",
            path=str(target),
            pattern=r"value = (?P<value>old)",
            replacement=r"value = new-\g<value>",
            expected_replacements=1,
        )
    )

    assert result["status"] == "success"
    assert result["replaced"] == 1
    assert result["targets"] == [{"start_line": 2, "end_line": 2, "content": "2: value = new-old"}]
    assert target.read_text(encoding="utf-8") == "header\nvalue = new-old\ntail\n"


@pytest.mark.asyncio
async def test_file_tool_find_returns_each_regex_match_with_five_lines_of_context(tmp_path):
    target = tmp_path / "find.txt"
    lines = [f"line {index}" for index in range(1, 21)]
    lines[6] = "MATCH first"
    lines[15] = "MATCH second"
    target.write_text("\n".join(lines), encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(await executor.execute(operation="find", path=str(target), pattern=r"MATCH \w+"))

    assert result["status"] == "success"
    assert result["match_count"] == 2
    assert [(item["start_line"], item["context_start_line"], item["context_end_line"]) for item in result["matches"]] == [
        (7, 2, 12),
        (16, 11, 20),
    ]
    assert "7: MATCH first" in result["matches"][0]["content"]
    assert "16: MATCH second" in result["matches"][1]["content"]


@pytest.mark.asyncio
async def test_file_tool_rejects_absolute_paths_outside_allowed_directories(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside.txt"
    allowed.mkdir()
    outside.write_text("secret", encoding="utf-8")
    executor = _executor(tmp_path, allowed_operation_dirs=[str(allowed)])

    result = json.loads(await executor.execute(operation="read", path=str(outside), start_line=1, end_line=1))

    assert result["status"] == "failed"
    assert "allowed" in result["error"].lower() or "允许" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.txt", "nested/../../outside.txt", "C:\\outside.txt", "\\\\server\\share\\outside.txt"])
async def test_file_tool_rejects_paths_that_escape_relative_workspace(tmp_path, path):
    executor = _executor(tmp_path, allowed_operation_dirs=[])

    result = json.loads(await executor.execute(operation="write", path=path, content="blocked"))

    assert result["status"] == "failed"
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.asyncio
async def test_file_tool_rejects_relative_symlink_escape(tmp_path):
    executor = _executor(tmp_path, allowed_operation_dirs=[])
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "data.txt"
    outside_file.write_text("blocked", encoding="utf-8")
    link_path = executor.user_temp_dir / "linked"
    try:
        os.symlink(outside_dir, link_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")

    result = json.loads(await executor.execute(operation="read", path="linked/data.txt", start_line=1, end_line=1))

    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_file_tool_large_find_result_is_truncated_by_unified_tool_dispatch(tmp_path):
    target = tmp_path / "large.txt"
    target.write_text("\n".join(f"MATCH {index} " + "x" * 120 for index in range(300)), encoding="utf-8")
    cfg = ProfileConfig.model_validate(
        {
            "tool": {
                "enabled_tools": ["file_tool"],
                "allowed_operation_dirs": [str(tmp_path)],
            }
        }
    )
    profile = Profile(id=1, uid="user-1", name="profile", configs=cfg.model_dump(mode="json"))
    tool_call = SimpleNamespace(id="call-1", name="file_tool", arguments={"operation": "find", "path": str(target), "pattern": "MATCH"})

    direct_executor = FileToolExecutor(project_root=str(tmp_path), uid="user-1")
    direct_executor.set_config(cfg)
    direct_result = await direct_executor.execute(**tool_call.arguments)
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
        context_window_k=4,
        tool_result_round_budget_tokens=120,
    )

    assert len(direct_result) > len(result.content or "")
    assert result.content
