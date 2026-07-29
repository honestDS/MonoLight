import hashlib
import hmac
import json
import math
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.core.constants import (
    ERR_AUDIT_INVALID_JSON_VALUE,
    ERR_AUDIT_JSON_KEY_NOT_STRING,
    ERR_AUDIT_JSON_TYPE_UNSUPPORTED,
    ERR_AUDIT_TOOL_ARGUMENTS_INVALID,
    ERR_AUDIT_TOOL_CALL_ID_INVALID,
    ERR_AUDIT_TOOL_NAME_EMPTY,
    ERR_AUDIT_TOOL_ROUND_EMPTY,
)
from app.core.i18n import t

_JSON_SCALAR_TYPES = (str, int, bool, type(None))


@dataclass(frozen=True, slots=True)
class FileIntegritySnapshot:
    original_path: str
    absolute_path: str
    resolved_path: str
    exists: bool
    file_type: str
    size: int | None
    sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ToolCallIntegritySnapshot:
    tool_call_id: str
    turn_index: int
    tool_name: str
    arguments: dict[str, Any]
    arguments_json: str
    arguments_sha256: str
    call_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ToolRoundIntegritySnapshot:
    round_sha256: str
    tool_calls: tuple[ToolCallIntegritySnapshot, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_sha256": self.round_sha256,
            "tool_calls": [tool_call.to_dict() for tool_call in self.tool_calls],
        }


def _validate_json_value(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(t(ERR_AUDIT_INVALID_JSON_VALUE, path=path))
        return
    if isinstance(value, _JSON_SCALAR_TYPES):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(t(ERR_AUDIT_JSON_KEY_NOT_STRING, path=path))
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(t(ERR_AUDIT_JSON_TYPE_UNSUPPORTED, path=path, value_type=type(value).__name__))


def canonical_json_dumps(value: Any) -> str:
    _validate_json_value(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def serialize_tool_arguments(arguments: dict[str, Any]) -> str:
    _validate_json_value(arguments)
    return canonical_json_dumps(arguments)


def build_tool_call_integrity_snapshot(
    *,
    tool_call_id: str,
    turn_index: int,
    tool_name: str,
    arguments: dict[str, Any],
    uid: str,
    session_id: str,
    working_directory: str | Path,
) -> ToolCallIntegritySnapshot:
    arguments_json = canonical_json_dumps(arguments)
    arguments_sha256 = sha256_text(arguments_json)
    call_payload = {
        "arguments": arguments,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "turn_index": turn_index,
        "uid": uid,
        "working_directory": str(Path(working_directory).resolve(strict=False)),
    }
    call_sha256 = sha256_text(canonical_json_dumps(call_payload))
    return ToolCallIntegritySnapshot(
        tool_call_id=tool_call_id,
        turn_index=turn_index,
        tool_name=tool_name,
        arguments=json.loads(arguments_json),
        arguments_json=arguments_json,
        arguments_sha256=arguments_sha256,
        call_sha256=call_sha256,
    )


def build_tool_round_integrity_snapshot(
    *,
    tool_calls: list[dict[str, Any]],
    uid: str,
    session_id: str,
    working_directory: str | Path,
) -> ToolRoundIntegritySnapshot:
    if not tool_calls:
        raise ValueError(t(ERR_AUDIT_TOOL_ROUND_EMPTY))

    snapshots: list[ToolCallIntegritySnapshot] = []
    seen_ids: set[str] = set()
    for turn_index, tool_call in enumerate(tool_calls):
        tool_call_id = tool_call.get("id")
        tool_name = tool_call.get("name")
        arguments = tool_call.get("arguments")
        if not isinstance(tool_call_id, str) or not tool_call_id or tool_call_id in seen_ids:
            raise ValueError(t(ERR_AUDIT_TOOL_CALL_ID_INVALID))
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(t(ERR_AUDIT_TOOL_NAME_EMPTY))
        if not isinstance(arguments, dict):
            raise ValueError(t(ERR_AUDIT_TOOL_ARGUMENTS_INVALID))
        seen_ids.add(tool_call_id)
        snapshots.append(
            build_tool_call_integrity_snapshot(
                tool_call_id=tool_call_id,
                turn_index=turn_index,
                tool_name=tool_name,
                arguments=arguments,
                uid=uid,
                session_id=session_id,
                working_directory=working_directory,
            )
        )

    round_payload = {
        "session_id": session_id,
        "tool_calls": [
            {
                "arguments": snapshot.arguments,
                "id": snapshot.tool_call_id,
                "name": snapshot.tool_name,
                "turn_index": snapshot.turn_index,
            }
            for snapshot in snapshots
        ],
        "uid": uid,
        "working_directory": str(Path(working_directory).resolve(strict=False)),
    }
    return ToolRoundIntegritySnapshot(
        round_sha256=sha256_text(canonical_json_dumps(round_payload)),
        tool_calls=tuple(snapshots),
    )


def verify_tool_round_integrity(
    expected: ToolRoundIntegritySnapshot,
    *,
    tool_calls: list[dict[str, Any]],
    uid: str,
    session_id: str,
    working_directory: str | Path,
) -> bool:
    try:
        actual = build_tool_round_integrity_snapshot(
            tool_calls=tool_calls,
            uid=uid,
            session_id=session_id,
            working_directory=working_directory,
        )
    except ValueError:
        return False
    if not hmac.compare_digest(expected.round_sha256, actual.round_sha256):
        return False
    if len(expected.tool_calls) != len(actual.tool_calls):
        return False
    return all(
        expected_call.tool_call_id == actual_call.tool_call_id
        and expected_call.turn_index == actual_call.turn_index
        and expected_call.tool_name == actual_call.tool_name
        and hmac.compare_digest(expected_call.arguments_sha256, actual_call.arguments_sha256)
        and hmac.compare_digest(expected_call.call_sha256, actual_call.call_sha256)
        for expected_call, actual_call in zip(expected.tool_calls, actual.tool_calls, strict=True)
    )


def verify_persisted_tool_round(
    *,
    expected_round_sha256: str,
    expected_tool_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    uid: str,
    session_id: str,
    working_directory: str | Path,
) -> bool:
    try:
        actual = build_tool_round_integrity_snapshot(
            tool_calls=tool_calls,
            uid=uid,
            session_id=session_id,
            working_directory=working_directory,
        )
    except ValueError:
        return False
    if not hmac.compare_digest(expected_round_sha256, actual.round_sha256):
        return False
    if len(expected_tool_calls) != len(actual.tool_calls):
        return False

    expected_by_turn: dict[int, dict[str, Any]] = {}
    for expected_call in expected_tool_calls:
        turn_index = expected_call.get("turn_index")
        if not isinstance(turn_index, int) or turn_index in expected_by_turn:
            return False
        expected_by_turn[turn_index] = expected_call
    if set(expected_by_turn) != set(range(len(actual.tool_calls))):
        return False

    for actual_call in actual.tool_calls:
        expected_call = expected_by_turn[actual_call.turn_index]
        expected_arguments_hash = expected_call.get("arguments_hash")
        if expected_call.get("original_tool_call_id") != actual_call.tool_call_id or expected_call.get("tool_name") != actual_call.tool_name or not isinstance(expected_arguments_hash, str) or not hmac.compare_digest(expected_arguments_hash, actual_call.arguments_sha256):
            return False
    return True


def create_file_integrity_snapshot(path: str | Path, *, working_directory: str | Path) -> FileIntegritySnapshot:
    original_path = str(path)
    source_path = Path(path)
    if not source_path.is_absolute():
        source_path = Path(working_directory) / source_path

    absolute_path = Path(os.path.abspath(source_path))
    resolved_path = absolute_path.resolve(strict=False)
    try:
        path_stat = absolute_path.lstat()
    except FileNotFoundError:
        return FileIntegritySnapshot(
            original_path=original_path,
            absolute_path=str(absolute_path),
            resolved_path=str(resolved_path),
            exists=False,
            file_type="missing",
            size=None,
            sha256=None,
        )

    if stat.S_ISLNK(path_stat.st_mode):
        file_type = "symlink"
    elif stat.S_ISREG(path_stat.st_mode):
        file_type = "regular_file"
    elif stat.S_ISDIR(path_stat.st_mode):
        file_type = "directory"
    else:
        file_type = "other"

    if file_type not in {"regular_file", "symlink"} or not resolved_path.is_file():
        return FileIntegritySnapshot(
            original_path=original_path,
            absolute_path=str(absolute_path),
            resolved_path=str(resolved_path),
            exists=True,
            file_type=file_type,
            size=None,
            sha256=None,
        )

    digest = hashlib.sha256()
    try:
        size = resolved_path.stat().st_size
        with resolved_path.open("rb") as file_handle:
            while chunk := file_handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        size = None
        sha256 = None
    else:
        sha256 = digest.hexdigest()

    return FileIntegritySnapshot(
        original_path=original_path,
        absolute_path=str(absolute_path),
        resolved_path=str(resolved_path),
        exists=True,
        file_type=file_type,
        size=size,
        sha256=sha256,
    )


def verify_file_integrity_snapshot(expected: dict[str, Any], actual: FileIntegritySnapshot) -> bool:
    expected_absolute_path = expected.get("absolute_path")
    expected_resolved_path = expected.get("resolved_path")
    expected_exists = expected.get("exists")
    expected_file_type = expected.get("file_type")
    if not isinstance(expected_absolute_path, str) or not isinstance(expected_resolved_path, str) or not isinstance(expected_exists, bool) or not isinstance(expected_file_type, str):
        return False
    if expected_absolute_path != actual.absolute_path or expected_resolved_path != actual.resolved_path or expected_exists != actual.exists or expected_file_type != actual.file_type:
        return False
    if not expected_exists:
        return actual.file_type == "missing" and expected.get("size") is None and expected.get("sha256") is None

    expected_size = expected.get("size")
    expected_sha256 = expected.get("sha256")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or not isinstance(expected_sha256, str) or not expected_sha256:
        return False
    return actual.size == expected_size and isinstance(actual.sha256, str) and hmac.compare_digest(actual.sha256, expected_sha256)
