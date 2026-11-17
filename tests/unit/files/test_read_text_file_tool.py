import hashlib
import os

import pytest

from app.core.tools import get_registered_tool_names
from app.core.tools.read_text_file import READ_TEXT_FILE_TOOL_SCHEMA, read_text_file
from app.core.utils.tokenizer import estimate_tokens


def test_read_text_file_accepts_absolute_and_working_directory_relative_paths(tmp_path):
    source = tmp_path / "nested" / "evidence.txt"
    source.parent.mkdir()
    source.write_text("audit evidence", encoding="utf-8")

    absolute = read_text_file(source, working_directory=tmp_path / "other", max_tokens=100)
    relative = read_text_file("nested/evidence.txt", working_directory=tmp_path, max_tokens=100)

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

    result = read_text_file(link, working_directory=tmp_path, max_tokens=100)

    assert result.status == "ok"
    assert result.file_type == "symlink"
    assert result.content == "linked evidence"
    assert result.resolved_path == str(target.resolve())


def test_read_text_file_rejects_directories(tmp_path):
    result = read_text_file(tmp_path, working_directory=tmp_path, max_tokens=100)

    assert result.status == "not_regular"
    assert result.file_type == "directory"
    assert result.content is None


def test_read_text_file_rejects_symlink_to_non_regular_target(tmp_path):
    link = tmp_path / "directory-link"
    try:
        os.symlink(tmp_path, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    result = read_text_file(link, working_directory=tmp_path, max_tokens=100)

    assert result.status == "not_regular"
    assert result.file_type == "symlink"
    assert result.content is None


def test_read_text_file_reports_truncation_and_keeps_full_snapshot_digest(tmp_path):
    source = tmp_path / "large.txt"
    content = "0123456789"
    source.write_text(content, encoding="utf-8")

    result = read_text_file(source, working_directory=tmp_path, max_tokens=1)

    assert result.status == "ok"
    assert content.startswith(result.content)
    assert estimate_tokens(result.content) <= 1
    assert result.bytes_read == len(result.content.encode("utf-8"))
    assert result.truncated is True
    assert result.size == len(content.encode("utf-8"))
    assert result.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_read_text_file_rejects_exhausted_token_budget(tmp_path):
    source = tmp_path / "evidence.txt"
    source.write_text("audit evidence", encoding="utf-8")

    result = read_text_file(source, working_directory=tmp_path, max_tokens=0)

    assert result.status == "limit_exceeded"
    assert result.content is None
    assert result.bytes_read == 0
    assert result.truncated is True


def test_read_text_file_is_not_registered_for_normal_models():
    assert READ_TEXT_FILE_TOOL_SCHEMA["function"]["name"] == "read_text_file"
    assert "read_text_file" not in get_registered_tool_names()
