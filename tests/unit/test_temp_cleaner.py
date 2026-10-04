import os
from pathlib import Path

import pytest

from app import tasks


def _create_file(root: Path, relative_path: str, size: int, mtime: float) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize(
    ("files", "max_size_bytes", "expected_deleted", "expected_largest_size", "expected_project_size"),
    [
        pytest.param(
            (
                ("temp_user-a/root.txt", 40, 300),
                ("temp_user-a/generated_images/image.png", 40, 200),
                ("temp_user-a/tool_results/deep/result.json", 40, 100),
                ("temp_user-b/keep.bin", 80, 400),
            ),
            100,
            1,
            80,
            160,
            id="aggregates-recursive-user-files",
        ),
        pytest.param(
            (
                ("temp_user-a/root.txt", 100, 100),
                ("temp_user-b/root.txt", 100, 200),
            ),
            100,
            0,
            100,
            200,
            id="exact-capacity-boundary",
        ),
    ],
)
def test_cleanup_temp_dir_by_size_uses_each_user_directory_capacity(
    tmp_path,
    monkeypatch,
    files,
    max_size_bytes,
    expected_deleted,
    expected_largest_size,
    expected_project_size,
):
    temp_dir = tmp_path / "temp"
    paths = {relative_path: _create_file(temp_dir, relative_path, size, mtime) for relative_path, size, mtime in files}
    monkeypatch.setattr(tasks, "TEMP_DIR", temp_dir)

    deleted_count, largest_current_size = tasks._cleanup_temp_dir_by_size(max_size_bytes)

    assert deleted_count == expected_deleted
    assert largest_current_size == expected_largest_size

    oldest_path = temp_dir / "temp_user-a/tool_results/deep/result.json"
    if expected_deleted:
        assert not oldest_path.exists()

    retained_size = 0
    for relative_path, size, _ in files:
        path = paths[relative_path]
        if expected_deleted and path == oldest_path:
            continue
        assert path.read_bytes() == b"x" * size
        retained_size += size
    assert retained_size == expected_project_size
    assert retained_size > max_size_bytes


@pytest.mark.parametrize("max_size_bytes", [0, -1])
def test_cleanup_temp_dir_by_size_skips_nonpositive_limits(tmp_path, monkeypatch, max_size_bytes):
    temp_dir = tmp_path / "temp"
    file_path = _create_file(temp_dir, "temp_user/file.bin", 40, 100)
    monkeypatch.setattr(tasks, "TEMP_DIR", temp_dir)

    assert tasks._cleanup_temp_dir_by_size(max_size_bytes) == (0, 0)
    assert file_path.read_bytes() == b"x" * 40


def test_cleanup_temp_dir_by_size_returns_zero_for_missing_temp_root(tmp_path, monkeypatch):
    temp_dir = tmp_path / "missing-temp"
    monkeypatch.setattr(tasks, "TEMP_DIR", temp_dir)

    assert tasks._cleanup_temp_dir_by_size(100) == (0, 0)
