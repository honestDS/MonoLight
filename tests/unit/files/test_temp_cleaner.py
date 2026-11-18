import os
from pathlib import Path

import pytest

from app import tasks
from app.core.tools import send_file_to_user


@pytest.fixture(autouse=True)
def _isolate_file_download_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(send_file_to_user, "FILE_TOKEN_DIR", tmp_path / "file_download_tokens")


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


def test_cleanup_temp_dir_by_size_skips_replaced_snapshot_and_cleans_other_user_file(tmp_path, monkeypatch):
    temp_dir = tmp_path / "temp"
    replacement_content = b"replacement" * 10
    replaced_path = _create_file(temp_dir, "temp_user-a/old.bin", len(replacement_content), 100).resolve()
    other_old_path = _create_file(temp_dir, "temp_user-b/old.bin", 101, 200).resolve()
    replaced_user_dir = (temp_dir / "temp_user-a").resolve()
    original_collect = tasks._collect_temp_dir_files
    replacement_seen = False

    def collect_and_replace_oldest(path):
        nonlocal replacement_seen
        result = original_collect(path)
        if path == replaced_user_dir:
            _, file_heap, _ = result
            oldest_path = min(file_heap, key=lambda candidate: candidate[0])[3]
            assert oldest_path == replaced_path
            oldest_path.unlink()
            oldest_path.write_bytes(replacement_content)
            os.utime(oldest_path, None)
            replacement_seen = True
        return result

    monkeypatch.setattr(tasks, "TEMP_DIR", temp_dir)
    monkeypatch.setattr(tasks, "_collect_temp_dir_files", collect_and_replace_oldest)

    deleted_count, largest_current_size = tasks._cleanup_temp_dir_by_size(100)

    assert replacement_seen
    assert deleted_count == 1
    assert largest_current_size == len(replacement_content)
    assert replaced_path.read_bytes() == replacement_content
    assert not other_old_path.exists()


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
