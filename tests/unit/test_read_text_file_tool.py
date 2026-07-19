import hashlib
import os

import pytest

from app.core.tools import get_registered_tool_names
from app.core.tools.read_text_file import READ_TEXT_FILE_TOOL_SCHEMA, read_text_file


def test_read_text_file_accepts_absolute_and_working_directory_relative_paths(tmp_path):
    source = tmp_path / "nested" / "evidence.txt"
    source.parent.mkdir()
    source.write_text("audit evidence", encoding="utf-8")

    absolute = read_text_file(source, working_directory=tmp_path / "other", max_bytes=100)
    relative = read_text_file("nested/evidence.txt", working_directory=tmp_path, max_bytes=100)

    assert absolute.status == "ok"
    assert absolute.content == "audit evidence"
    assert relative.absolute_path == str(source)
    assert relative.resolved_path == str(source.resolve())
    assert relative.sha256 == hashlib.sha256(b"audit evidence").hexdigest()


def test_read_text_file_resolves_symlink_to_a_regular_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("linked evidence", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    result = read_text_file(link, working_directory=tmp_path, max_bytes=100)

    assert result.status == "ok"
    assert result.file_type == "symlink"
    assert result.content == "linked evidence"
    assert result.resolved_path == str(target.resolve())


def test_read_text_file_rejects_directories(tmp_path):
    result = read_text_file(tmp_path, working_directory=tmp_path, max_bytes=100)

    assert result.status == "not_regular"
    assert result.file_type == "directory"
    assert result.content is None


def test_read_text_file_rejects_symlink_to_non_regular_target(tmp_path):
    link = tmp_path / "directory-link"
    try:
        os.symlink(tmp_path, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    result = read_text_file(link, working_directory=tmp_path, max_bytes=100)

    assert result.status == "not_regular"
    assert result.file_type == "symlink"
    assert result.content is None


def test_read_text_file_reports_truncation_and_keeps_full_snapshot_digest(tmp_path):
    source = tmp_path / "large.txt"
    source.write_text("0123456789", encoding="utf-8")

    result = read_text_file(source, working_directory=tmp_path, max_bytes=4)

    assert result.status == "ok"
    assert result.content == "0123"
    assert result.bytes_read == 4
    assert result.truncated is True
    assert result.size == 10
    assert result.sha256 == hashlib.sha256(b"0123456789").hexdigest()


def test_read_text_file_is_not_registered_for_normal_models():
    assert READ_TEXT_FILE_TOOL_SCHEMA["function"]["name"] == "read_text_file"
    assert "read_text_file" not in get_registered_tool_names()
