import hashlib
import hmac
import json
import math
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261029_01_merge_terminal_tools"

_BATCH_SIZE = 500
_PROFILE_TABLE_NAME = "profile"
_BACKGROUND_TASK_TABLE_NAME = "background_task"
_WORK_TABLE_NAME = "session_reply_work_item"
_MESSAGE_TABLE_NAME = "message"
_AUDIT_RECORD_TABLE_NAME = "audit_record"
_AUDIT_TOOL_DETAIL_TABLE_NAME = "audit_tool_detail"
_AUDIT_TOOL_RESULT_VERSION_TABLE_NAME = "audit_tool_result_version"
_CHAT_SESSION_TABLE_NAME = "chat_session"
_TOOL_NAME = "terminal_tool"
_TOOL_OPERATIONS = {
    "terminal_read": "read",
    "terminal_write": "write",
    "terminal_resize": "resize",
    "terminal_close": "close",
}
_PROFILE_TOOL_RENAMES = {tool_name: _TOOL_NAME for tool_name in _TOOL_OPERATIONS}
_NON_EXECUTABLE_AUDIT_STATUSES = frozenset({"preparing", "audit_failed", "blocked", "rejected", "expired", "cancelled", "succeeded", "failed", "execution_unknown"})

_METADATA = MetaData()
_PROFILE = Table(
    _PROFILE_TABLE_NAME,
    _METADATA,
    Column("id", Integer),
    Column("configs", JSON),
)
_BACKGROUND_TASK = Table(
    _BACKGROUND_TASK_TABLE_NAME,
    _METADATA,
    Column("id", Integer),
    Column("tool_name", String(100)),
    Column("arguments", JSON),
    Column("result", JSON),
)
_WORK = Table(
    _WORK_TABLE_NAME,
    _METADATA,
    Column("id", Integer),
    Column("execution_state", JSON),
)
_MESSAGE = Table(
    _MESSAGE_TABLE_NAME,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("type", String(40)),
    Column("content", Text),
    Column("content_revision", Integer),
)
_AUDIT_RECORD = Table(
    _AUDIT_RECORD_TABLE_NAME,
    _METADATA,
    Column("id", Integer),
    Column("status", String(30)),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("source_assistant_message_id", Integer),
    Column("working_directory", Text),
    Column("round_arguments_hash", String(64)),
    Column("tool_count", Integer),
)
_AUDIT_TOOL_DETAIL = Table(
    _AUDIT_TOOL_DETAIL_TABLE_NAME,
    _METADATA,
    Column("id", Integer),
    Column("audit_record_id", Integer),
    Column("original_tool_call_id", String(100)),
    Column("turn_index", Integer),
    Column("tool_name", String(100)),
    Column("arguments_hash", String(64)),
    Column("arguments_summary", Text),
    Column("file_snapshots", JSON),
)
_AUDIT_TOOL_RESULT_VERSION = Table(
    _AUDIT_TOOL_RESULT_VERSION_TABLE_NAME,
    _METADATA,
    Column("id", Integer),
    Column("content", Text),
)
_CHAT_SESSION = Table(
    _CHAT_SESSION_TABLE_NAME,
    _METADATA,
    Column("session_id", String(100)),
    Column("uid", String(100)),
    Column("context_content_revision", Integer),
)


def _inspect_schema(sync_connection) -> tuple[set[str], dict[str, set[str]]]:
    inspector = inspect(sync_connection)
    table_names = {str(name) for name in inspector.get_table_names()}
    columns: dict[str, set[str]] = {}
    for table_name in (
        _PROFILE_TABLE_NAME,
        _BACKGROUND_TASK_TABLE_NAME,
        _WORK_TABLE_NAME,
        _MESSAGE_TABLE_NAME,
        _AUDIT_RECORD_TABLE_NAME,
        _AUDIT_TOOL_DETAIL_TABLE_NAME,
        _AUDIT_TOOL_RESULT_VERSION_TABLE_NAME,
        _CHAT_SESSION_TABLE_NAME,
    ):
        if table_name in table_names:
            columns[table_name] = {str(column["name"]) for column in inspector.get_columns(table_name)}
    return table_names, columns


def _migrate_profile_config(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    updated = dict(config)
    changed = False

    enabled_tools = config.get("enabled_tools")
    if isinstance(enabled_tools, list):
        normalized_tools: list[Any] = []
        seen_tools: set[str] = set()
        for tool_name in enabled_tools:
            if not isinstance(tool_name, str):
                normalized_tools.append(tool_name)
                continue
            normalized_name = _PROFILE_TOOL_RENAMES.get(tool_name, tool_name)
            if normalized_name not in seen_tools:
                seen_tools.add(normalized_name)
                normalized_tools.append(normalized_name)
        if normalized_tools != enabled_tools:
            updated["enabled_tools"] = normalized_tools
            changed = True

    for key in ("tool", "configs"):
        nested_config = config.get(key)
        if isinstance(nested_config, dict):
            migrated_nested, nested_changed = _migrate_profile_config(nested_config)
            if nested_changed:
                updated[key] = migrated_nested
                changed = True

    return (updated if changed else config), changed


async def _migrate_profiles(session: AsyncSession) -> None:
    last_id: int | None = None
    while True:
        statement = select(_PROFILE.c.id, _PROFILE.c.configs).order_by(_PROFILE.c.id.asc()).limit(_BATCH_SIZE)
        if last_id is not None:
            statement = statement.where(_PROFILE.c.id > last_id)
        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return
        for row in rows:
            if not isinstance(row["configs"], dict):
                continue
            migrated_configs, changed = _migrate_profile_config(row["configs"])
            if changed:
                await session.execute(update(_PROFILE).where(_PROFILE.c.id == row["id"]).values(configs=migrated_configs))
        last_id = rows[-1]["id"]


async def _migrate_background_tasks(session: AsyncSession, *, has_result: bool) -> None:
    last_id: int | None = None
    while True:
        columns = [_BACKGROUND_TASK.c.id, _BACKGROUND_TASK.c.tool_name, _BACKGROUND_TASK.c.arguments]
        if has_result:
            columns.append(_BACKGROUND_TASK.c.result)
        statement = select(*columns).order_by(_BACKGROUND_TASK.c.id.asc()).limit(_BATCH_SIZE)
        if last_id is not None:
            statement = statement.where(_BACKGROUND_TASK.c.id > last_id)
        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return
        for row in rows:
            values: dict[str, Any] = {}
            operation = _legacy_operation(row["tool_name"])
            if operation is not None:
                arguments = row["arguments"]
                if not isinstance(arguments, dict):
                    raise ValueError(f"background task {row['id']} has invalid terminal arguments")
                if "operation" in arguments and arguments["operation"] != operation:
                    raise ValueError(f"background task {row['id']} has conflicting terminal operation")
                migrated_arguments = dict(arguments)
                migrated_arguments["operation"] = operation
                values.update(tool_name=_TOOL_NAME, arguments=migrated_arguments)
            if has_result:
                migrated_result, result_changed = _migrate_background_result(row["result"])
                if result_changed:
                    values["result"] = migrated_result
            if values:
                await session.execute(update(_BACKGROUND_TASK).where(_BACKGROUND_TASK.c.id == row["id"]).values(values))
        last_id = rows[-1]["id"]


def _migrate_internal_message_list(messages: Any) -> tuple[Any, bool]:
    if not isinstance(messages, list):
        return messages, False
    updated_messages = list(messages)
    changed = False
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            converted_message, changed_call_ids = _convert_payload(message)
            message_changed = bool(changed_call_ids)
        elif message.get("role") == "tool":
            converted_message, message_changed = _migrate_tool_result_payload(message)
        else:
            continue
        if message_changed:
            updated_messages[index] = converted_message
            changed = True
    return (updated_messages if changed else messages), changed


def _migrate_waiting_slots(slots: Any) -> tuple[Any, bool]:
    if not isinstance(slots, list):
        return slots, False
    updated_slots = list(slots)
    changed = False
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict) or _legacy_operation(slot.get("tool_name")) is None:
            continue
        updated_slot = dict(slot)
        updated_slot["tool_name"] = _TOOL_NAME
        updated_slots[index] = updated_slot
        changed = True
    return (updated_slots if changed else slots), changed


def _migrate_work_execution_state(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    updated_state = dict(state)
    changed = False

    checkpoint = state.get("dispatcher_checkpoint")
    if isinstance(checkpoint, dict):
        updated_checkpoint = dict(checkpoint)
        checkpoint_changed = False
        for key in ("messages", "turn_messages"):
            migrated_messages, messages_changed = _migrate_internal_message_list(checkpoint.get(key))
            if messages_changed:
                updated_checkpoint[key] = migrated_messages
                checkpoint_changed = True
        if checkpoint_changed:
            updated_state["dispatcher_checkpoint"] = updated_checkpoint
            changed = True

    external_resume_context = state.get("external_resume_context")
    if isinstance(external_resume_context, list):
        updated_external_resume_context = list(external_resume_context)
        external_context_changed = False
        for index, item in enumerate(external_resume_context):
            if not isinstance(item, dict):
                continue
            migrated_item, item_changed = _migrate_background_tool_result_payload(item)
            if item_changed:
                updated_external_resume_context[index] = migrated_item
                external_context_changed = True
        if external_context_changed:
            updated_state["external_resume_context"] = updated_external_resume_context
            changed = True

    for key in ("pending_tools", "terminal_watch_waits"):
        migrated_slots, slots_changed = _migrate_waiting_slots(state.get(key))
        if slots_changed:
            updated_state[key] = migrated_slots
            changed = True

    response = state.get("response")
    if isinstance(response, dict):
        updated_response = dict(response)
        response_changed = False
        migrated_history, history_changed = _migrate_internal_message_list(response.get("history"))
        if history_changed:
            updated_response["history"] = migrated_history
            response_changed = True
        migrated_slots, slots_changed = _migrate_waiting_slots(response.get("pending_tools"))
        if slots_changed:
            updated_response["pending_tools"] = migrated_slots
            response_changed = True
        if response_changed:
            updated_state["response"] = updated_response
            changed = True

    return (updated_state if changed else state), changed


async def _migrate_work_items(session: AsyncSession) -> None:
    last_id: int | None = None
    while True:
        statement = select(_WORK.c.id, _WORK.c.execution_state).order_by(_WORK.c.id.asc()).limit(_BATCH_SIZE)
        if last_id is not None:
            statement = statement.where(_WORK.c.id > last_id)
        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return
        for row in rows:
            if not isinstance(row["execution_state"], dict):
                continue
            migrated_state, changed = _migrate_work_execution_state(row["execution_state"])
            if changed:
                await session.execute(update(_WORK).where(_WORK.c.id == row["id"]).values(execution_state=migrated_state))
        last_id = rows[-1]["id"]


async def _migrate_audit_tool_result_versions(session: AsyncSession) -> None:
    last_id: int | None = None
    while True:
        statement = select(_AUDIT_TOOL_RESULT_VERSION.c.id, _AUDIT_TOOL_RESULT_VERSION.c.content).order_by(_AUDIT_TOOL_RESULT_VERSION.c.id.asc()).limit(_BATCH_SIZE)
        if last_id is not None:
            statement = statement.where(_AUDIT_TOOL_RESULT_VERSION.c.id > last_id)
        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return
        for row in rows:
            migrated_content, changed = _migrate_serialized_tool_result(row["content"])
            if changed:
                await session.execute(update(_AUDIT_TOOL_RESULT_VERSION).where(_AUDIT_TOOL_RESULT_VERSION.c.id == row["id"]).values(content=migrated_content))
        last_id = rows[-1]["id"]


def _validate_json_value(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON value at {path}")
        return
    if isinstance(value, (str, int, bool, type(None))):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non-string JSON key at {path}")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"unsupported JSON value at {path}: {type(value).__name__}")


def _canonical_json_dumps(value: Any) -> str:
    _validate_json_value(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_nonstandard_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def _arguments_hash(arguments: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json_dumps(arguments))


def _round_hash(*, tool_calls: list[dict[str, Any]], uid: str, session_id: str, working_directory: str) -> str:
    payload = {
        "session_id": session_id,
        "tool_calls": [
            {
                "arguments": dict(tool_call["arguments"]),
                "id": tool_call["id"],
                "name": tool_call["name"],
                "turn_index": turn_index,
            }
            for turn_index, tool_call in enumerate(tool_calls)
        ],
        "uid": uid,
        "working_directory": working_directory,
    }
    return _sha256_text(_canonical_json_dumps(payload))


def _legacy_operation(tool_name: Any) -> str | None:
    return _TOOL_OPERATIONS.get(tool_name) if isinstance(tool_name, str) else None


def _converted_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    old_name = tool_call.get("name")
    operation = _legacy_operation(old_name)
    if operation is None:
        return tool_call

    tool_call_id = tool_call.get("id")
    arguments = tool_call.get("arguments")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ValueError(f"invalid terminal tool call id: {tool_call_id!r}")
    if not isinstance(arguments, dict):
        raise ValueError(f"invalid arguments for terminal tool call {tool_call_id}")
    if "operation" in arguments and arguments["operation"] != operation:
        raise ValueError(f"conflicting terminal operation for tool call {tool_call_id}")

    converted = dict(tool_call)
    converted["name"] = _TOOL_NAME
    converted_arguments = dict(arguments)
    converted_arguments["operation"] = operation
    converted["arguments"] = converted_arguments
    return converted


def _convert_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return payload, set()

    converted_calls: list[Any] = []
    changed_call_ids: set[str] = set()
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict) or _legacy_operation(tool_call.get("name")) is None:
            converted_calls.append(tool_call)
            continue
        converted = _converted_tool_call(tool_call)
        converted_calls.append(converted)
        changed_call_ids.add(converted["id"])

    converted_payload = dict(payload)
    converted_payload["tool_calls"] = converted_calls
    return converted_payload, changed_call_ids


def _migrate_result_dict(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if _legacy_operation(payload.get("tool_name")) is None:
        return payload, False
    updated_payload = dict(payload)
    updated_payload["tool_name"] = _TOOL_NAME
    return updated_payload, True


def _migrate_result_json_string(value: str) -> tuple[str, bool]:
    try:
        payload = json.loads(value, parse_constant=_reject_nonstandard_json_constant)
    except (TypeError, ValueError):
        return value, False
    if not isinstance(payload, dict):
        return value, False
    migrated_payload, changed = _migrate_result_dict(payload)
    if not changed:
        return value, False
    return _canonical_json_dumps(migrated_payload), True


def _migrate_tool_result_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if payload.get("role") != "tool" or not isinstance(payload.get("content"), str):
        return payload, False
    migrated_content, changed = _migrate_result_json_string(payload["content"])
    if not changed:
        return payload, False
    updated_payload = dict(payload)
    updated_payload["content"] = migrated_content
    return updated_payload, True


def _migrate_serialized_tool_result(content: Any) -> tuple[Any, bool]:
    if not isinstance(content, str):
        return content, False
    try:
        payload = json.loads(content, parse_constant=_reject_nonstandard_json_constant)
    except (TypeError, ValueError):
        return content, False
    if not isinstance(payload, dict):
        return content, False
    migrated_payload, changed = _migrate_tool_result_payload(payload)
    if not changed:
        return content, False
    return _canonical_json_dumps(migrated_payload), True


def _migrate_background_result(result: Any) -> tuple[Any, bool]:
    if not isinstance(result, dict):
        return result, False
    updated_result = dict(result)
    changed = False
    if _legacy_operation(result.get("tool_name")) is not None:
        updated_result["tool_name"] = _TOOL_NAME
        changed = True
    if isinstance(result.get("content"), str):
        migrated_content, content_changed = _migrate_result_json_string(result["content"])
        if content_changed:
            updated_result["content"] = migrated_content
            changed = True
    elif isinstance(result.get("content"), dict):
        migrated_content, content_changed = _migrate_result_dict(result["content"])
        if content_changed:
            updated_result["content"] = migrated_content
            changed = True
    return (updated_result if changed else result), changed


def _migrate_background_tool_result_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if payload.get("type") != "background_tool_result":
        return payload, False
    updated_payload = dict(payload)
    changed = False

    if _legacy_operation(payload.get("tool_name")) is not None:
        updated_payload["tool_name"] = _TOOL_NAME
        changed = True

    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict) and _legacy_operation(tool_call.get("name")) is not None:
        updated_payload["tool_call"] = _converted_tool_call(tool_call)
        changed = True

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        updated_tool_result = dict(tool_result)
        tool_result_changed = False
        result_content = tool_result.get("content")
        if isinstance(result_content, str):
            migrated_content, content_changed = _migrate_result_json_string(result_content)
            if content_changed:
                updated_tool_result["content"] = migrated_content
                tool_result_changed = True
        elif isinstance(result_content, dict):
            migrated_content, content_changed = _migrate_result_dict(result_content)
            if content_changed:
                updated_tool_result["content"] = migrated_content
                tool_result_changed = True
        if tool_result_changed:
            updated_payload["tool_result"] = updated_tool_result
            changed = True

    task = payload.get("task")
    if isinstance(task, dict):
        migrated_task, task_changed = _migrate_background_result(task)
        if task_changed:
            updated_payload["task"] = migrated_task
            changed = True

    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        updated_tasks = list(tasks)
        tasks_changed = False
        for index, item in enumerate(tasks):
            if not isinstance(item, dict):
                continue
            migrated_item, item_changed = _migrate_background_tool_result_payload(item)
            if item_changed:
                updated_tasks[index] = migrated_item
                tasks_changed = True
        if tasks_changed:
            updated_payload["tasks"] = updated_tasks
            changed = True

    return (updated_payload if changed else payload), changed


def _validate_source_tool_calls(source_tool_calls: list[Any], source_message_id: int) -> dict[str, dict[str, Any]]:
    source_by_id: dict[str, dict[str, Any]] = {}
    for index, tool_call in enumerate(source_tool_calls):
        if not isinstance(tool_call, dict):
            raise ValueError(f"audit source message {source_message_id} has invalid tool call at index {index}")
        tool_call_id = tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id or tool_call_id in source_by_id:
            raise ValueError(f"audit source message {source_message_id} has duplicate or invalid tool call id")
        source_by_id[tool_call_id] = tool_call
    return source_by_id


async def _migrate_audit_record(
    session: AsyncSession,
    *,
    source_message_id: int,
    source_tool_calls: Any,
    source_uid: str,
    source_session_id: str,
    record: Any,
    changed_call_ids: set[str],
) -> None:
    record_id = record["id"]
    details = (
        (
            await session.execute(
                select(
                    _AUDIT_TOOL_DETAIL.c.id,
                    _AUDIT_TOOL_DETAIL.c.original_tool_call_id,
                    _AUDIT_TOOL_DETAIL.c.turn_index,
                    _AUDIT_TOOL_DETAIL.c.tool_name,
                    _AUDIT_TOOL_DETAIL.c.arguments_hash,
                )
                .where(_AUDIT_TOOL_DETAIL.c.audit_record_id == record_id)
                .order_by(_AUDIT_TOOL_DETAIL.c.turn_index.asc())
            )
        )
        .mappings()
        .all()
    )
    if not details:
        if isinstance(record["status"], str) and record["status"].lower() in _NON_EXECUTABLE_AUDIT_STATUSES:
            return
        raise ValueError(f"audit record {record_id} has no tool details")
    if not isinstance(source_tool_calls, list):
        raise ValueError(f"audit source message {source_message_id} has no tool calls")
    source_by_id = _validate_source_tool_calls(source_tool_calls, source_message_id)
    if not isinstance(record["uid"], str) or not isinstance(record["session_id"], str) or not isinstance(record["working_directory"], str):
        raise ValueError(f"audit record {record_id} has invalid ownership or working directory")
    if record["uid"] != source_uid or record["session_id"] != source_session_id:
        raise ValueError(f"audit record {record_id} owner does not match source message {source_message_id}")
    if not isinstance(record["round_arguments_hash"], str):
        raise ValueError(f"audit record {record_id} has invalid round hash")
    if any(type(detail["turn_index"]) is not int or detail["turn_index"] < 0 for detail in details):
        raise ValueError(f"audit record {record_id} has invalid turn index")
    if [detail["turn_index"] for detail in details] != list(range(len(details))):
        raise ValueError(f"audit record {record_id} has non-contiguous turn indexes")
    if type(record["tool_count"]) is not int or record["tool_count"] != len(details):
        raise ValueError(f"audit record {record_id} tool count does not match details")

    seen_call_ids: set[str] = set()
    original_tool_calls: list[dict[str, Any]] = []
    for detail in details:
        call_id = detail["original_tool_call_id"]
        if not isinstance(call_id, str) or not call_id or call_id in seen_call_ids:
            raise ValueError(f"audit record {record_id} has duplicate or invalid original tool call id")
        seen_call_ids.add(call_id)
        source_call = source_by_id.get(call_id)
        if source_call is None:
            raise ValueError(f"audit record {record_id} references missing source tool call {call_id}")
        source_name = source_call.get("name")
        source_arguments = source_call.get("arguments")
        if not isinstance(source_name, str) or not source_name or not isinstance(source_arguments, dict):
            raise ValueError(f"audit record {record_id} references invalid source tool call {call_id}")
        if detail["tool_name"] != source_name:
            raise ValueError(f"audit detail {detail['id']} tool name does not match source")
        try:
            expected_arguments_hash = _arguments_hash(source_arguments)
        except ValueError as exc:
            raise ValueError(f"audit record {record_id} has invalid source arguments") from exc
        if not isinstance(detail["arguments_hash"], str) or not hmac.compare_digest(detail["arguments_hash"], expected_arguments_hash):
            raise ValueError(f"audit detail {detail['id']} arguments hash does not match source")
        original_tool_calls.append(source_call)

    try:
        expected_round_hash = _round_hash(
            tool_calls=original_tool_calls,
            uid=record["uid"],
            session_id=record["session_id"],
            working_directory=record["working_directory"],
        )
    except ValueError as exc:
        raise ValueError(f"audit record {record_id} has invalid source round") from exc
    if not hmac.compare_digest(record["round_arguments_hash"], expected_round_hash):
        raise ValueError(f"audit record {record_id} round hash does not match source")

    converted_tool_calls: list[dict[str, Any]] = []
    changed_details: list[tuple[int, str, str]] = []
    for detail, source_call in zip(details, original_tool_calls, strict=True):
        if source_call["id"] in changed_call_ids:
            converted_call = _converted_tool_call(source_call)
            converted_arguments = converted_call["arguments"]
            arguments_json = _canonical_json_dumps(converted_arguments)
            converted_tool_calls.append(converted_call)
            changed_details.append((detail["id"], _sha256_text(arguments_json), arguments_json))
        else:
            converted_tool_calls.append(source_call)
    if not changed_details:
        return

    round_hash = _round_hash(
        tool_calls=converted_tool_calls,
        uid=record["uid"],
        session_id=record["session_id"],
        working_directory=record["working_directory"],
    )
    for detail_id, arguments_hash, arguments_summary in changed_details:
        await session.execute(update(_AUDIT_TOOL_DETAIL).where(_AUDIT_TOOL_DETAIL.c.id == detail_id).values(tool_name=_TOOL_NAME, arguments_hash=arguments_hash, arguments_summary=arguments_summary))
    await session.execute(update(_AUDIT_RECORD).where(_AUDIT_RECORD.c.id == record_id).values(round_arguments_hash=round_hash))


async def _migrate_audit_records(
    session: AsyncSession,
    *,
    source_message_id: int,
    source_payload: dict[str, Any],
    source_uid: str,
    source_session_id: str,
    changed_call_ids: set[str],
) -> None:
    source_tool_calls = source_payload.get("tool_calls")
    records = (
        (
            await session.execute(
                select(
                    _AUDIT_RECORD.c.id,
                    _AUDIT_RECORD.c.status,
                    _AUDIT_RECORD.c.uid,
                    _AUDIT_RECORD.c.session_id,
                    _AUDIT_RECORD.c.working_directory,
                    _AUDIT_RECORD.c.round_arguments_hash,
                    _AUDIT_RECORD.c.tool_count,
                )
                .where(_AUDIT_RECORD.c.source_assistant_message_id == source_message_id)
                .order_by(_AUDIT_RECORD.c.id.asc())
            )
        )
        .mappings()
        .all()
    )
    if not records:
        return
    for record in records:
        await _migrate_audit_record(
            session,
            source_message_id=source_message_id,
            source_tool_calls=source_tool_calls,
            source_uid=source_uid,
            source_session_id=source_session_id,
            record=record,
            changed_call_ids=changed_call_ids,
        )


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    table_names, table_columns = await connection.run_sync(_inspect_schema)
    profile_columns = table_columns.get(_PROFILE_TABLE_NAME)
    if profile_columns is not None:
        missing_profile_columns = {"id", "configs"} - profile_columns
        if missing_profile_columns:
            raise RuntimeError(f"profile table is missing required columns: {sorted(missing_profile_columns)}")
        await _migrate_profiles(session)

    background_task_columns = table_columns.get(_BACKGROUND_TASK_TABLE_NAME)
    if background_task_columns is not None:
        missing_background_task_columns = {"id", "tool_name", "arguments"} - background_task_columns
        if missing_background_task_columns:
            raise RuntimeError(f"background_task table is missing required columns: {sorted(missing_background_task_columns)}")
        await _migrate_background_tasks(session, has_result="result" in background_task_columns)

    work_columns = table_columns.get(_WORK_TABLE_NAME)
    if work_columns is not None:
        missing_work_columns = {"id", "execution_state"} - work_columns
        if missing_work_columns:
            raise RuntimeError(f"session_reply_work_item table is missing required columns: {sorted(missing_work_columns)}")
        await _migrate_work_items(session)

    result_version_columns = table_columns.get(_AUDIT_TOOL_RESULT_VERSION_TABLE_NAME)
    if result_version_columns is not None:
        missing_result_version_columns = {"id", "content"} - result_version_columns
        if missing_result_version_columns:
            raise RuntimeError(f"audit_tool_result_version table is missing required columns: {sorted(missing_result_version_columns)}")
        await _migrate_audit_tool_result_versions(session)

    message_columns = table_columns.get(_MESSAGE_TABLE_NAME)
    if message_columns is None:
        return
    missing_message_columns = {"id", "type", "content"} - message_columns
    if missing_message_columns:
        raise RuntimeError(f"message table is missing required columns: {sorted(missing_message_columns)}")

    audit_record_present = _AUDIT_RECORD_TABLE_NAME in table_names
    audit_detail_present = _AUDIT_TOOL_DETAIL_TABLE_NAME in table_names
    if audit_record_present != audit_detail_present:
        raise RuntimeError("audit_record and audit_tool_detail must exist together")
    audit_enabled = audit_record_present
    if audit_enabled:
        required_record_columns = {
            "id",
            "status",
            "uid",
            "session_id",
            "source_assistant_message_id",
            "working_directory",
            "round_arguments_hash",
            "tool_count",
        }
        required_detail_columns = {
            "id",
            "audit_record_id",
            "original_tool_call_id",
            "turn_index",
            "tool_name",
            "arguments_hash",
            "arguments_summary",
        }
        missing_record_columns = required_record_columns - table_columns[_AUDIT_RECORD_TABLE_NAME]
        missing_detail_columns = required_detail_columns - table_columns[_AUDIT_TOOL_DETAIL_TABLE_NAME]
        if missing_record_columns or missing_detail_columns:
            raise RuntimeError(f"audit schema is missing required columns: record={sorted(missing_record_columns)}, detail={sorted(missing_detail_columns)}")
        if not {"uid", "session_id"}.issubset(message_columns):
            raise RuntimeError("message table is missing uid/session_id required for audit ownership validation")

    message_statement = select(_MESSAGE.c.id, _MESSAGE.c.type, _MESSAGE.c.content)
    if "uid" in message_columns:
        message_statement = message_statement.add_columns(_MESSAGE.c.uid)
    if "session_id" in message_columns:
        message_statement = message_statement.add_columns(_MESSAGE.c.session_id)

    chat_session_columns = table_columns.get(_CHAT_SESSION_TABLE_NAME, set())
    can_bump_context_revision = {"uid", "session_id", "context_content_revision"}.issubset(chat_session_columns)
    last_message_id: int | None = None
    while True:
        statement = message_statement.where(_MESSAGE.c.type.in_(("TOOL_CALL", "tool_call", "TOOL_RESULT", "tool_result", "BACKGROUND_TASK_RESULT", "background_result"))).order_by(_MESSAGE.c.id.asc()).limit(_BATCH_SIZE)
        if last_message_id is not None:
            statement = statement.where(_MESSAGE.c.id > last_message_id)
        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            break
        last_message_id = rows[-1]["id"]
        affected_sessions: set[tuple[str, str]] = set()

        for row in rows:
            content = row["content"]
            source_uid = row["uid"] if "uid" in row else None
            source_session_id = row["session_id"] if "session_id" in row else None
            if row["type"] in ("TOOL_CALL", "tool_call"):
                if not isinstance(content, str):
                    continue
                try:
                    payload = json.loads(content, parse_constant=_reject_nonstandard_json_constant)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("tool_calls"), list):
                    continue
                if not any(isinstance(tool_call, dict) and _legacy_operation(tool_call.get("name")) is not None for tool_call in payload["tool_calls"]):
                    continue
                converted_payload, changed_call_ids = _convert_payload(payload)
                if not changed_call_ids:
                    continue
                if audit_enabled:
                    if not isinstance(source_uid, str) or not isinstance(source_session_id, str):
                        raise ValueError(f"audit source message {row['id']} has no owner identity")
                    await _migrate_audit_records(
                        session,
                        source_message_id=row["id"],
                        source_payload=payload,
                        source_uid=source_uid,
                        source_session_id=source_session_id,
                        changed_call_ids=changed_call_ids,
                    )
                new_content = _canonical_json_dumps(converted_payload)
            elif row["type"] in ("TOOL_RESULT", "tool_result"):
                new_content, changed = _migrate_serialized_tool_result(content)
                if not changed:
                    continue
            else:
                if not isinstance(content, str):
                    continue
                try:
                    payload = json.loads(content, parse_constant=_reject_nonstandard_json_constant)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                migrated_payload, changed = _migrate_background_tool_result_payload(payload)
                if not changed:
                    continue
                new_content = _canonical_json_dumps(migrated_payload)
            values: dict[Any, Any] = {_MESSAGE.c.content: new_content}
            if "content_revision" in message_columns:
                values[_MESSAGE.c.content_revision] = func.coalesce(_MESSAGE.c.content_revision, 0) + 1
            await session.execute(update(_MESSAGE).where(_MESSAGE.c.id == row["id"]).values(values))
            if isinstance(source_uid, str) and isinstance(source_session_id, str):
                affected_sessions.add((source_uid, source_session_id))

        if can_bump_context_revision:
            for uid, session_id in affected_sessions:
                await session.execute(update(_CHAT_SESSION).where(_CHAT_SESSION.c.uid == uid, _CHAT_SESSION.c.session_id == session_id).values(context_content_revision=func.coalesce(_CHAT_SESSION.c.context_content_revision, 0) + 1))
