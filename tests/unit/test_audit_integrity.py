import hashlib
import json
import os

import pytest

from app.core.audit.integrity import build_tool_call_integrity_snapshot, canonical_json_dumps, create_file_integrity_snapshot, verify_file_integrity_snapshot
from app.core.utils.dispatcher.save_message import _to_storable_content
from app.models.message import InternalMessage, InternalToolCall, MessageRole, MessageType


def test_tool_call_message_persistence_preserves_complete_arguments():
    command = "  python script.py --name '测试值'\n"
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": command})],
    )

    stored = _to_storable_content(message, MessageType.TOOL_CALL)
    payload = json.loads(stored)

    assert payload["tool_calls"][0]["arguments"]["command"] == command
    assert stored == canonical_json_dumps(message.model_dump(mode="python", exclude_none=True))


def test_tool_call_message_persistence_rejects_invalid_numbers():
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"value": float("nan")})],
    )

    with pytest.raises(ValueError, match="非法 JSON 数值"):
        _to_storable_content(message, MessageType.TOOL_CALL)


def test_tool_call_integrity_preserves_full_arguments_and_command(tmp_path):
    arguments = {
        "command": "  python script.py --name '测试值'\n",
        "options": {"retry": 2, "enabled": True},
    }

    snapshot = build_tool_call_integrity_snapshot(
        tool_call_id="call-1",
        turn_index=0,
        tool_name="execute_shell",
        arguments=arguments,
        uid="u1",
        session_id="session-1",
        working_directory=tmp_path,
    )

    assert snapshot.arguments == arguments
    assert snapshot.arguments["command"] == arguments["command"]
    assert snapshot.arguments_json == canonical_json_dumps(arguments)
    assert snapshot.arguments_sha256 == hashlib.sha256(snapshot.arguments_json.encode("utf-8")).hexdigest()


def test_tool_call_integrity_is_stable_for_argument_key_order(tmp_path):
    common = {
        "tool_call_id": "call-1",
        "turn_index": 1,
        "tool_name": "write_file",
        "uid": "u1",
        "session_id": "session-1",
        "working_directory": tmp_path,
    }

    first = build_tool_call_integrity_snapshot(arguments={"content": "value", "file_path": "a.txt"}, **common)
    second = build_tool_call_integrity_snapshot(arguments={"file_path": "a.txt", "content": "value"}, **common)

    assert first.arguments_sha256 == second.arguments_sha256
    assert first.call_sha256 == second.call_sha256


def test_tool_call_integrity_binds_identity_and_working_directory(tmp_path):
    common = {
        "tool_call_id": "call-1",
        "turn_index": 1,
        "tool_name": "execute_shell",
        "arguments": {"command": "echo value"},
        "uid": "u1",
        "working_directory": tmp_path,
    }

    first = build_tool_call_integrity_snapshot(session_id="session-1", **common)
    second = build_tool_call_integrity_snapshot(session_id="session-2", **common)

    assert first.arguments_sha256 == second.arguments_sha256
    assert first.call_sha256 != second.call_sha256


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_invalid_numbers(invalid_value):
    with pytest.raises(ValueError, match="非法 JSON 数值"):
        canonical_json_dumps({"value": invalid_value})


def test_file_integrity_records_absolute_path_resolved_path_size_and_hash(tmp_path):
    source_file = tmp_path / "scripts" / "entry.py"
    source_file.parent.mkdir()
    content = b"print('audit')\n"
    source_file.write_bytes(content)

    snapshot = create_file_integrity_snapshot("scripts/entry.py", working_directory=tmp_path)

    assert snapshot.original_path == "scripts/entry.py"
    assert snapshot.absolute_path == str(source_file.absolute())
    assert snapshot.resolved_path == str(source_file.resolve())
    assert snapshot.exists is True
    assert snapshot.file_type == "regular_file"
    assert snapshot.size == len(content)
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()


def test_file_integrity_records_missing_target_without_empty_hash(tmp_path):
    missing_file = tmp_path / "missing.txt"

    missing = create_file_integrity_snapshot(missing_file, working_directory=tmp_path)

    assert missing.absolute_path == str(missing_file)
    assert missing.exists is False
    assert missing.file_type == "missing"
    assert missing.size is None
    assert missing.sha256 is None

    empty_file = tmp_path / "empty.txt"
    empty_file.touch()
    empty = create_file_integrity_snapshot(empty_file, working_directory=tmp_path)

    assert empty.exists is True
    assert empty.size == 0
    assert empty.sha256 == hashlib.sha256(b"").hexdigest()
    assert empty.sha256


def test_file_integrity_recheck_accepts_missing_target_only_when_still_missing(tmp_path):
    target = tmp_path / "append.txt"
    expected = create_file_integrity_snapshot(target, working_directory=tmp_path).to_dict()

    assert verify_file_integrity_snapshot(expected, create_file_integrity_snapshot(target, working_directory=tmp_path))

    target.write_text("created", encoding="utf-8")
    assert not verify_file_integrity_snapshot(expected, create_file_integrity_snapshot(target, working_directory=tmp_path))


@pytest.mark.parametrize("object_kind", ["directory", "symlink"])
def test_file_integrity_recheck_rejects_created_non_file_objects(tmp_path, object_kind):
    target = tmp_path / "append-target"
    expected = create_file_integrity_snapshot(target, working_directory=tmp_path).to_dict()
    if object_kind == "directory":
        target.mkdir()
    else:
        link_target = tmp_path / "link-target.txt"
        link_target.write_text("target", encoding="utf-8")
        try:
            os.symlink(link_target, target)
        except OSError as exc:
            pytest.skip(f"当前系统不允许创建测试链接: {exc}")

    assert not verify_file_integrity_snapshot(expected, create_file_integrity_snapshot(target, working_directory=tmp_path))


def test_file_integrity_recheck_rejects_changed_and_replaced_existing_target(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    expected = create_file_integrity_snapshot(target, working_directory=tmp_path).to_dict()

    target.write_text("after", encoding="utf-8")
    assert not verify_file_integrity_snapshot(expected, create_file_integrity_snapshot(target, working_directory=tmp_path))

    replacement = tmp_path / "replacement.txt"
    replacement.write_text("replacement", encoding="utf-8")
    target.unlink()
    try:
        os.symlink(replacement, target)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")
    assert not verify_file_integrity_snapshot(expected, create_file_integrity_snapshot(target, working_directory=tmp_path))
