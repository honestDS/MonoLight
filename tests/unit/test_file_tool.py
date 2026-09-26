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
    assert function["parameters"]["properties"]["operation"]["enum"] == ["read", "write", "edit", "patch", "grep"]
    assert function["parameters"]["required"] == ["operation", "path"]
    assert "literal" in function["parameters"]["properties"]["new_text"]["description"].lower()
    assert "\\n" in function["parameters"]["properties"]["new_text"]["description"]
    assert "unified-diff" in function["parameters"]["properties"]["patch"]["description"].lower()


def test_profile_config_migrates_legacy_write_file_enablement():
    cfg = ProfileConfig.model_validate({"tool": {"enabled_tools": ["execute_shell", "write_file", "file_tool"]}})

    assert cfg.tool.enabled_tools == ["execute_shell", "file_tool"]


@pytest.mark.asyncio
async def test_file_tool_read_defaults_to_first_page_and_returns_pagination_metadata(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    executor = _executor(tmp_path)

    default_page = json.loads(await executor.execute(operation="read", path=str(source)))
    result = json.loads(await executor.execute(operation="read", path=str(source), start_line=2, end_line=3))

    assert default_page["status"] == "success"
    assert default_page["start_line"] == 1
    assert default_page["end_line"] == 4
    assert default_page["total_lines"] == 4
    assert default_page["truncated"] is False
    assert default_page["next_start_line"] is None
    assert result == {
        "status": "success",
        "operation": "read",
        "path": str(source.resolve()),
        "start_line": 2,
        "end_line": 3,
        "total_lines": 4,
        "truncated": True,
        "next_start_line": 4,
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
async def test_file_tool_edit_requires_unique_literal_target_and_does_not_write_on_mismatch(tmp_path):
    target = tmp_path / "edit.txt"
    target.write_text("alpha = 1\nalpha = 2\n", encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="edit",
            path=str(target),
            old_text="alpha",
            new_text="beta",
        )
    )

    assert result["status"] == "failed"
    assert result["matched"] == 2
    assert target.read_text(encoding="utf-8") == "alpha = 1\nalpha = 2\n"


@pytest.mark.asyncio
async def test_file_tool_edit_treats_backslash_sequences_as_literal_and_preserves_crlf(tmp_path):
    target = tmp_path / "edit.py"
    target.write_bytes(b'header\r\nvalue = "old"\r\ntail\r\n')
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="edit",
            path=str(target),
            old_text='value = "old"',
            new_text='value = "line1\\nline2"',
        )
    )

    assert result["status"] == "success"
    assert result["replaced"] == 1
    assert result["targets"] == [{"start_line": 2, "end_line": 2, "content": '2: value = "line1\\nline2"'}]
    assert target.read_bytes() == b'header\r\nvalue = "line1\\nline2"\r\ntail\r\n'


@pytest.mark.asyncio
async def test_file_tool_edit_replace_all_updates_every_literal_match(tmp_path):
    target = tmp_path / "edit.txt"
    target.write_text("alpha = 1\nalpha = 2\n", encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="edit",
            path=str(target),
            old_text="alpha",
            new_text="beta",
            replace_all=True,
        )
    )

    assert result["status"] == "success"
    assert result["replaced"] == 2
    assert target.read_text(encoding="utf-8") == "beta = 1\nbeta = 2\n"


@pytest.mark.asyncio
async def test_file_tool_patch_applies_multiple_hunks_and_keeps_backslashes_literal(tmp_path):
    target = tmp_path / "patch.py"
    target.write_text('def first():\n    return "old"\n\ndef second():\n    return "old"\n', encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="patch",
            path=str(target),
            patch="""@@
 def first():
-    return "old"
+    return "line1\\nline2"
@@
 def second():
-    return "old"
+    return "new"
""",
        )
    )

    assert result["status"] == "success"
    assert result["hunks_applied"] == 2
    assert target.read_text(encoding="utf-8") == 'def first():\n    return "line1\\nline2"\n\ndef second():\n    return "new"\n'


@pytest.mark.asyncio
async def test_file_tool_patch_is_atomic_when_later_hunk_does_not_match(tmp_path):
    target = tmp_path / "patch.py"
    original = "first old\nsecond old\n"
    target.write_text(original, encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="patch",
            path=str(target),
            patch="""@@
-first old
+first new
@@
-missing
+second new
""",
        )
    )

    assert result["status"] == "failed"
    assert result["reason"] == "hunk_not_found"
    assert result["hunk_index"] == 2
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_file_tool_patch_ignores_final_patch_line_terminator_when_matching(tmp_path):
    target = tmp_path / "patch.py"
    target.write_text("before\nold\nafter\n", encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="patch",
            path=str(target),
            patch="@@\n-old\n+new\n",
        )
    )

    assert result["status"] == "success"
    assert target.read_text(encoding="utf-8") == "before\nnew\nafter\n"


@pytest.mark.asyncio
async def test_file_tool_patch_rejects_ambiguous_hunk_without_writing(tmp_path):
    target = tmp_path / "patch.py"
    original = "old\nmiddle\nold\n"
    target.write_text(original, encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="patch",
            path=str(target),
            patch="@@\n-old\n+new",
        )
    )

    assert result["status"] == "failed"
    assert result["reason"] == "hunk_ambiguous"
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_file_tool_patch_returns_structured_parse_failure(tmp_path):
    target = tmp_path / "patch.py"
    target.write_text("old\n", encoding="utf-8")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="patch",
            path=str(target),
            patch="@@\n?old",
        )
    )

    assert result["status"] == "failed"
    assert result["reason"] == "line_prefix_invalid"
    assert result["line_number"] == 2
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_file_tool_patch_preserves_utf8_bom_and_crlf(tmp_path):
    target = tmp_path / "patch.py"
    target.write_bytes(b"\xef\xbb\xbfbefore\r\nold\r\nafter\r\n")
    executor = _executor(tmp_path)

    result = json.loads(
        await executor.execute(
            operation="patch",
            path=str(target),
            patch="@@\n old\n-after\n+changed",
        )
    )

    assert result["status"] == "success"
    assert target.read_bytes() == b"\xef\xbb\xbfbefore\r\nold\r\nchanged\r\n"


@pytest.mark.asyncio
async def test_file_tool_grep_searches_file_or_directory_with_filters_and_context(tmp_path):
    source_dir = tmp_path / "src"
    nested_dir = source_dir / "nested"
    nested_dir.mkdir(parents=True)
    one = source_dir / "one.py"
    two = nested_dir / "two.py"
    ignored = source_dir / "ignored.txt"
    one.write_text("before\nNeedle first\nafter\n", encoding="utf-8")
    two.write_text("before\nneedle second\nafter\n", encoding="utf-8")
    ignored.write_text("needle ignored\n", encoding="utf-8")
    executor = _executor(tmp_path)

    directory_result = json.loads(
        await executor.execute(
            operation="grep",
            path=str(source_dir),
            pattern=r"needle",
            include=["*.py"],
            ignore_case=True,
            context_lines=1,
            max_results=10,
        )
    )
    file_result = json.loads(
        await executor.execute(
            operation="grep",
            path=str(one),
            pattern=r"Needle",
            context_lines=0,
        )
    )

    assert directory_result["status"] == "success"
    assert directory_result["files_scanned"] == 2
    assert directory_result["matched_file_count"] == 2
    assert directory_result["match_count"] == 2
    assert directory_result["truncated"] is False
    assert all(item["context_start_line"] == 1 and item["context_end_line"] == 3 for item in directory_result["matches"])
    assert file_result["files_scanned"] == 1
    assert file_result["matched_file_count"] == 1
    assert file_result["match_count"] == 1


@pytest.mark.asyncio
async def test_file_tool_grep_allows_workspace_root_with_dot_path(tmp_path):
    executor = _executor(tmp_path, allowed_operation_dirs=[])
    target = executor.user_temp_dir / "root.py"
    target.write_text("needle\n", encoding="utf-8")

    result = json.loads(
        await executor.execute(
            operation="grep",
            path=".",
            pattern="needle",
            include=["*.py"],
        )
    )

    assert result["status"] == "success"
    assert result["path"] == str(executor.user_temp_dir.resolve())
    assert result["match_count"] == 1


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
async def test_file_tool_large_grep_result_is_truncated_by_unified_tool_dispatch(tmp_path):
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
    tool_call = SimpleNamespace(id="call-1", name="file_tool", arguments={"operation": "grep", "path": str(target), "pattern": "MATCH"})

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
