import hashlib
import json

import pytest

from app.core.audit.integrity import build_tool_call_integrity_snapshot, canonical_json_dumps, create_file_integrity_snapshot
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
    assert snapshot.size == len(content)
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
