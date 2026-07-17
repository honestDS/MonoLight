import json
import os

import pytest

from app.core.tools.file_writer import FileWriterExecutor


@pytest.mark.asyncio
async def test_write_file_writes_inside_user_workspace(tmp_path):
    executor = FileWriterExecutor(project_root=str(tmp_path), uid="u1")

    result = json.loads(await executor.execute(file_path="nested/result.txt", content="content"))

    assert result["status"] == "success"
    assert (tmp_path / "temp" / "temp_u1" / "nested" / "result.txt").read_text(encoding="utf-8") == "content"


@pytest.mark.asyncio
@pytest.mark.parametrize("file_path", ["../outside.txt", "nested/../../outside.txt", "/outside.txt", "C:\\outside.txt", "\\\\server\\share\\outside.txt", ""])
async def test_write_file_rejects_paths_outside_user_workspace(tmp_path, file_path):
    executor = FileWriterExecutor(project_root=str(tmp_path), uid="u1")

    result = json.loads(await executor.execute(file_path=file_path, content="blocked"))

    assert "error" in result
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.asyncio
async def test_write_file_rejects_symlink_escape(tmp_path):
    executor = FileWriterExecutor(project_root=str(tmp_path), uid="u1")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    link_path = executor.user_temp_dir / "linked"
    try:
        os.symlink(outside_dir, link_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")

    result = json.loads(await executor.execute(file_path="linked/result.txt", content="blocked"))

    assert "error" in result
    assert not (outside_dir / "result.txt").exists()
