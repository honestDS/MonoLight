import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.audit.integrity import build_tool_round_integrity_snapshot, canonical_json_dumps, verify_persisted_tool_round
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import SUBAGENT_DELEGATION_SYSTEM_PROMPT
from app.core.utils.dispatcher.checkpoint_message import checkpoint_message_hash
from app.models.audit import AuditRecord, AuditToolDetail, AuditToolResultVersion
from app.models.background_task import BackgroundTask
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import SessionReplyCheckpointMessage, SessionReplyCheckpointMessageReference, SessionReplyWorkItem
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261112_01_rename_subagent_result_query"
_BATCH_SIZE = 200
_GUIDANCE_START = "[Subagent delegation guidance]"
_GUIDANCE_END = "[End subagent delegation guidance]"
_OLD_GUIDANCE = re.compile(r"(?:\bUse\s+`?query`?\s+for\b|`?\bquery`?\s+for\s+targeted\s+status\b)")
_NOTIFICATION_TYPES = frozenset({"subagent_result", "SUBAGENT_RESULT", "subagent_board_update", "subagent_result_ready", "background_result", "BACKGROUND_TASK_RESULT", "background_task_result", "background_tool_result"})
_MESSAGE_KEYS = ("messages", "turn_messages", "history", "request_context", "frozen_context_messages", "external_resume_messages")
_NESTED_LIST_KEYS = ("tasks", "external_resume_context", "deferred_external_results", "external_results")


@dataclass(slots=True)
class _MigrationSources:
    result_ids: set[int]
    board_hints: dict[int, dict[str, Any]]


def _load_json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _with_field(mapping: dict[str, Any], key: str, new_value: Any) -> dict[str, Any]:
    if mapping.get(key) == new_value:
        return mapping
    updated = dict(mapping)
    updated[key] = new_value
    return updated


def _rename_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, dict) or arguments.get("operation") != "query":
        return arguments
    return _with_field(arguments, "operation", "query_result")


def _is_agent_work_call(call: Any) -> bool:
    if not isinstance(call, dict) or call.get("name") != "agent_work":
        return False
    if not isinstance(call.get("id"), str) or not call["id"].strip():
        return False
    arguments = call.get("arguments")
    return isinstance(arguments, dict)


def _rewrite_call(call: Any) -> Any:
    if not isinstance(call, dict) or call.get("name") != "agent_work":
        return call
    return _with_field(call, "arguments", _rename_arguments(call.get("arguments")))


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _enum_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    enum_value = getattr(value, "value", None)
    return enum_value if isinstance(enum_value, str) else None


def _is_message_type(value: Any, message_type: MessageType) -> bool:
    return _enum_text(value) in {message_type.value, message_type.name}


def _board_hint_json(hint: dict[str, Any], operation: str) -> str:
    payload = dict(hint)
    payload["full_result_operation"] = operation
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _rewrite_verified_board_post(post: Any, sources: _MigrationSources | None) -> Any:
    if not isinstance(post, dict) or sources is None or not _positive_int(post.get("id")):
        return post
    hint = sources.board_hints.get(post["id"])
    if hint is None or post.get("child_work_id") != hint.get("work_id") or post.get("stage") != hint.get("status"):
        return post
    if post.get("content") != _board_hint_json(hint, "query"):
        return post
    return _with_field(post, "content", _board_hint_json(hint, "query_result"))


def _rewrite_tool_payload(payload: dict[str, Any], sources: _MigrationSources | None) -> dict[str, Any]:
    if payload.get("operation") == "query":
        payload = _with_field(payload, "operation", "query_result")
    if payload.get("operation") != "board_read" or not isinstance(payload.get("posts"), list):
        return payload
    rewritten_posts: list[Any] = []
    for post in payload["posts"]:
        rewritten_posts.append(_rewrite_verified_board_post(post, sources))
    return _with_field(payload, "posts", rewritten_posts)


def _rewrite_tool_result(value: Any, *, agent_bound: bool = False, sources: _MigrationSources | None = None) -> Any:
    if not agent_bound:
        return value
    if isinstance(value, dict):
        return _rewrite_tool_payload(value, sources)
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    try:
        payload, end = json.JSONDecoder().raw_decode(stripped)
    except (TypeError, ValueError):
        return value
    if not isinstance(payload, dict):
        return value
    rewritten = _rewrite_tool_payload(payload, sources)
    if rewritten is payload:
        return value
    prefix_length = len(value) - len(stripped)
    return value[:prefix_length] + canonical_json_dumps(rewritten) + stripped[end:]


def _rewrite_system_content(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    start = content.find(_GUIDANCE_START)
    if start < 0:
        return content
    end_marker = content.find(_GUIDANCE_END, start + len(_GUIDANCE_START))
    if end_marker < 0:
        return content
    end = end_marker + len(_GUIDANCE_END)
    if _OLD_GUIDANCE.search(content[start:end]) is None:
        return content
    return content[:start] + SUBAGENT_DELEGATION_SYSTEM_PROMPT + content[end:]


def _row_id_bound(value: Any, sources: _MigrationSources | None) -> bool:
    return sources is not None and type(value) is int and value in sources.result_ids


def _rewrite_message(payload: Any, *, agent_bound: bool = False, sources: _MigrationSources | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload
    role = payload.get("role")
    if role == "assistant":
        calls = payload.get("tool_calls")
        if not isinstance(calls, list):
            return payload
        rewritten_calls: list[Any] = []
        for call in calls:
            rewritten_calls.append(_rewrite_call(call))
        return _with_field(payload, "tool_calls", rewritten_calls)
    if role == "tool":
        bound = agent_bound or _row_id_bound(payload.get("id"), sources)
        return _with_field(payload, "content", _rewrite_tool_result(payload.get("content"), agent_bound=bound, sources=sources))
    if role == "system":
        return _with_field(payload, "content", _rewrite_system_content(payload.get("content")))
    if role != "user" or payload.get("type") not in _NOTIFICATION_TYPES:
        return payload
    envelope = _load_json_dict(payload.get("content"))
    if envelope is None:
        return payload
    rewritten_envelope = _rewrite_envelope(envelope, sources)
    if rewritten_envelope is envelope:
        return payload
    return _with_field(payload, "content", canonical_json_dumps(rewritten_envelope))


def _rewrite_message_list(value: Any, sources: _MigrationSources | None = None) -> Any:
    if not isinstance(value, list):
        return value
    pending_call_ids: set[str] = set()
    rewritten_messages: list[Any] = []
    changed = False
    for message in value:
        rewritten_message = message
        if not isinstance(message, dict):
            pending_call_ids.clear()
        elif message.get("role") == "assistant":
            pending_call_ids.clear()
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if _is_agent_work_call(call):
                        pending_call_ids.add(call["id"])
            rewritten_message = _rewrite_message(message, sources=sources)
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            agent_bound = isinstance(call_id, str) and call_id in pending_call_ids
            rewritten_message = _rewrite_message(message, agent_bound=agent_bound, sources=sources)
        else:
            pending_call_ids.clear()
            rewritten_message = _rewrite_message(message, sources=sources)
        if rewritten_message is not message:
            changed = True
        rewritten_messages.append(rewritten_message)
    return rewritten_messages if changed else value


def _rewrite_direct_result(value: Any, sources: _MigrationSources | None = None) -> Any:
    if not isinstance(value, dict):
        return value
    return _rewrite_tool_payload(value, sources)


def _rewrite_envelope(value: Any, sources: _MigrationSources | None = None) -> Any:
    if not isinstance(value, dict):
        return value
    rewritten = value
    envelope_type = value.get("type")
    if envelope_type in {"tool_start", "tool_end"} and value.get("name") == "agent_work":
        rewritten = _with_field(rewritten, "arguments", _rename_arguments(value.get("arguments")))
        if envelope_type == "tool_end":
            rewritten = _with_field(rewritten, "result", _rewrite_tool_result(value.get("result"), agent_bound=True, sources=sources))
    if envelope_type in {"background_tool_result", "background_result"}:
        rewritten = _with_field(rewritten, "tool_call", _rewrite_call(value.get("tool_call")))
    if envelope_type == "external_work_resume" and value.get("source") == "subagent":
        rewritten = _with_field(rewritten, "tool_call", _rewrite_call(value.get("tool_call")))
        results = value.get("results")
        if isinstance(results, list):
            rewritten_results: list[Any] = []
            for result in results:
                rewritten_results.append(_rewrite_direct_result(result, sources))
            rewritten = _with_field(rewritten, "results", rewritten_results)
    for key in _MESSAGE_KEYS:
        messages = value.get(key)
        if isinstance(messages, list):
            rewritten = _with_field(rewritten, key, _rewrite_message_list(messages, sources))
    for key in ("response", "task"):
        nested = value.get(key)
        if isinstance(nested, dict):
            rewritten = _with_field(rewritten, key, _rewrite_envelope(nested, sources))
    for key in _NESTED_LIST_KEYS:
        entries = value.get(key)
        if not isinstance(entries, list):
            continue
        rewritten_entries: list[Any] = []
        for entry in entries:
            if isinstance(entry, dict):
                rewritten_entries.append(_rewrite_envelope(entry, sources))
            else:
                rewritten_entries.append(entry)
        rewritten = _with_field(rewritten, key, rewritten_entries)
    return rewritten


async def _rows(session: AsyncSession, model: Any, key: Any, *, lock: bool = False) -> AsyncIterator[Any]:
    last_id = 0
    while True:
        statement = select(model).where(key > last_id).order_by(key.asc()).limit(_BATCH_SIZE)
        if lock:
            statement = statement.with_for_update()
        result = await session.execute(statement)
        rows = list(result.scalars().all())
        if not rows:
            return
        for row in rows:
            yield row
        current_id = getattr(rows[-1], key.key, None)
        if not _positive_int(current_id):
            raise RuntimeError(f"{MIGRATION_ID}: {model.__name__} contains a row without a positive {key.key}")
        last_id = current_id


def _migration_error(detail: str) -> RuntimeError:
    return RuntimeError(f"{MIGRATION_ID}: {detail}")


async def _validate_checkpoint_hashes(session: AsyncSession) -> None:
    reference_ids_result = await session.execute(select(SessionReplyCheckpointMessageReference.message_id).distinct().order_by(SessionReplyCheckpointMessageReference.message_id))
    reference_ids = list(reference_ids_result.scalars().all())
    for offset in range(0, len(reference_ids), _BATCH_SIZE):
        ids = reference_ids[offset : offset + _BATCH_SIZE]
        result = await session.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id.in_(ids)))
        payloads = {row.id: row for row in result.scalars().all() if row.id is not None}
        for message_id in ids:
            message = payloads.get(message_id)
            if message is None:
                raise _migration_error(f"checkpoint message {message_id} is referenced but missing")
            if checkpoint_message_hash(message.payload) != message.content_hash:
                raise _migration_error(f"checkpoint message {message_id} content hash does not match its payload")


def _scope_for_message(message: Message) -> tuple[Any, ...]:
    return message.uid, message.session_id, message.profile_id, message.work_id


def _tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    calls = payload.get("tool_calls")
    if not isinstance(calls, list):
        return []
    result: list[dict[str, Any]] = []
    for call in calls:
        if isinstance(call, dict):
            result.append(call)
    return result


def _agent_work_call_ids(calls: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for call in calls:
        if _is_agent_work_call(call):
            ids.add(call["id"])
    return ids


def _changed_query_ids(old_payload: dict[str, Any], new_payload: dict[str, Any]) -> set[str]:
    old_calls: dict[str, dict[str, Any]] = {}
    for call in _tool_calls(old_payload):
        if isinstance(call.get("id"), str):
            old_calls[call["id"]] = call
    new_calls: dict[str, dict[str, Any]] = {}
    for call in _tool_calls(new_payload):
        if isinstance(call.get("id"), str):
            new_calls[call["id"]] = call
    changed: set[str] = set()
    for call_id, old_call in old_calls.items():
        new_call = new_calls.get(call_id)
        old_arguments = old_call.get("arguments")
        new_arguments = new_call.get("arguments") if isinstance(new_call, dict) else None
        if isinstance(new_arguments, dict) and isinstance(old_arguments, dict) and old_arguments.get("operation") == "query" and new_arguments.get("operation") == "query_result":
            changed.add(call_id)
    return changed


async def _migrate_audit_record(session: AsyncSession, message: Message, old_payload: dict[str, Any], record: AuditRecord, changed_query_ids: set[str]) -> None:
    if not changed_query_ids:
        return
    details_result = await session.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == record.id).order_by(AuditToolDetail.turn_index.asc(), AuditToolDetail.id.asc()))
    details = list(details_result.scalars().all())
    detail_ids = {detail.original_tool_call_id for detail in details}
    if not changed_query_ids.intersection(detail_ids):
        return
    if record.uid != message.uid or record.session_id != message.session_id or record.source_assistant_message_id != message.id:
        raise _migration_error(f"audit record {record.id} does not match source message {message.id}")
    if type(record.tool_count) is not int or record.tool_count != len(details):
        raise _migration_error(f"audit record {record.id} tool count does not match its details")
    if len(detail_ids) != len(details):
        raise _migration_error(f"audit record {record.id} has duplicate detail call ids")
    turn_indexes = [detail.turn_index for detail in details]
    if any(type(turn_index) is not int or turn_index < 0 for turn_index in turn_indexes) or turn_indexes != list(range(len(details))):
        raise _migration_error(f"audit record {record.id} has non-contiguous detail indexes")
    source_by_id: dict[str, dict[str, Any]] = {}
    for call in _tool_calls(old_payload):
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in source_by_id:
            raise _migration_error(f"audit record {record.id} source tool calls have duplicate or invalid ids")
        source_by_id[call_id] = call
    for detail in details:
        if detail.original_tool_call_id not in source_by_id:
            raise _migration_error(f"audit record {record.id} detail references a missing source call")
    old_subset: list[dict[str, Any]] = []
    expected_details: list[dict[str, Any]] = []
    for detail in details:
        old_subset.append(source_by_id[detail.original_tool_call_id])
        expected_details.append({"original_tool_call_id": detail.original_tool_call_id, "turn_index": detail.turn_index, "tool_name": detail.tool_name, "arguments_hash": detail.arguments_hash})
    old_integrity_calls: list[dict[str, Any]] = []
    for call in old_subset:
        old_integrity_calls.append({"id": call["id"], "name": call.get("name"), "arguments": call.get("arguments")})
    valid = verify_persisted_tool_round(expected_round_sha256=record.round_arguments_hash, expected_tool_calls=expected_details, tool_calls=old_integrity_calls, uid=message.uid, session_id=message.session_id, working_directory=record.working_directory)
    if not valid:
        raise _migration_error(f"audit record {record.id} integrity verification failed")
    converted_subset: list[dict[str, Any]] = []
    for call in old_subset:
        if call.get("id") in changed_query_ids:
            converted_subset.append(_rewrite_call(call))
        else:
            converted_subset.append(call)
    converted_calls: list[dict[str, Any]] = []
    for call in converted_subset:
        converted_calls.append({"id": call["id"], "name": call.get("name"), "arguments": call.get("arguments")})
    converted_snapshot = build_tool_round_integrity_snapshot(tool_calls=converted_calls, uid=message.uid, session_id=message.session_id, working_directory=record.working_directory)
    snapshots_by_id = {snapshot.tool_call_id: snapshot for snapshot in converted_snapshot.tool_calls}
    for detail in details:
        if detail.original_tool_call_id not in changed_query_ids:
            continue
        snapshot = snapshots_by_id[detail.original_tool_call_id]
        detail.arguments_hash = snapshot.arguments_sha256
        detail.arguments_summary = snapshot.arguments_json[:1000]
        session.add(detail)
    record.round_arguments_hash = converted_snapshot.round_sha256
    session.add(record)
    await session.flush()


async def _migrate_audit_for_assistant(session: AsyncSession, message: Message, old_payload: dict[str, Any], changed_query_ids: set[str]) -> None:
    if not changed_query_ids or not _positive_int(message.id):
        return
    records_result = await session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.source_assistant_message_id == message.id,
            AuditRecord.uid == message.uid,
            AuditRecord.session_id == message.session_id,
        )
        .order_by(AuditRecord.id.asc())
    )
    for record in records_result.scalars().all():
        await _migrate_audit_record(session, message, old_payload, record, changed_query_ids)


async def _migrate_messages(session: AsyncSession, sources: _MigrationSources) -> None:
    lookup_by_scope: dict[tuple[Any, ...], set[str]] = {}
    async for message in _rows(session, Message, Message.id):
        scope = _scope_for_message(message)
        role = _enum_text(message.role)
        if role == MessageRole.ASSISTANT.value:
            lookup_by_scope.pop(scope, None)
        stored_payload = _load_json_dict(message.content)
        if stored_payload is None:
            continue
        rewritten_payload = stored_payload
        if role == MessageRole.ASSISTANT.value and not _is_message_type(message.type, MessageType.TOOL_CALL):
            continue
        if role == MessageRole.ASSISTANT.value and _is_message_type(message.type, MessageType.TOOL_CALL):
            calls = _tool_calls(stored_payload)
            lookup_by_scope[scope] = _agent_work_call_ids(calls)
            rewritten_payload = _rewrite_message(stored_payload, sources=sources)
            changed_query_ids = _changed_query_ids(stored_payload, rewritten_payload)
            if not changed_query_ids and rewritten_payload is stored_payload:
                continue
            await _migrate_audit_for_assistant(session, message, stored_payload, changed_query_ids)
        elif role == MessageRole.TOOL.value and _is_message_type(message.type, MessageType.TOOL_RESULT):
            tool_call_id = stored_payload.get("tool_call_id")
            agent_bound = isinstance(tool_call_id, str) and tool_call_id in lookup_by_scope.get(scope, set())
            if agent_bound and _positive_int(message.id):
                sources.result_ids.add(message.id)
            rewritten_payload = _rewrite_message(stored_payload, agent_bound=agent_bound, sources=sources)
        elif role == MessageRole.USER.value and (_is_message_type(message.type, MessageType.SUBAGENT_RESULT) or _is_message_type(message.type, MessageType.BACKGROUND_TASK_RESULT)):
            rewritten_payload = _rewrite_envelope(stored_payload, sources)
        else:
            continue
        if rewritten_payload is stored_payload:
            continue
        message.content = canonical_json_dumps(rewritten_payload)
        if message.audit_record_id is None:
            message.content_revision = int(message.content_revision or 0) + 1
        session.add(message)
        if _positive_int(message.id):
            await session.execute(update(ChatSession).where(ChatSession.uid == message.uid, ChatSession.session_id == message.session_id).values(context_content_revision=ChatSession.context_content_revision + 1))
        await session.flush()


async def _migrate_audit_result_versions(session: AsyncSession, sources: _MigrationSources) -> None:
    async for version in _rows(session, AuditToolResultVersion, AuditToolResultVersion.id):
        payload = _load_json_dict(version.content)
        if payload is None:
            continue
        rewritten = _rewrite_message(payload, agent_bound=version.message_id in sources.result_ids, sources=sources)
        if rewritten is payload:
            continue
        version.content = canonical_json_dumps(rewritten)
        session.add(version)
        await session.flush()


async def _migrate_subagent_tool_calls(session: AsyncSession) -> None:
    async for call in _rows(session, SubagentToolCall, SubagentToolCall.id):
        rewritten = _rename_arguments(call.arguments)
        if rewritten is call.arguments:
            continue
        call.arguments = rewritten
        session.add(call)
        await session.flush()


def _board_hint(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "work_id": snapshot["work_id"],
        "current_work_id": snapshot["current_work_id"],
        "completed": snapshot["completed"],
        "status": snapshot["status"],
        "full_result_operation": "query",
    }


async def _validated_board_hint(session: AsyncSession, post: SubagentBoardPost) -> dict[str, Any] | None:
    if not _positive_int(post.id):
        return None
    content_payload = _load_json_dict(post.content)
    if content_payload is None or set(content_payload) != {"work_id", "current_work_id", "completed", "status", "full_result_operation"}:
        return None
    if content_payload.get("full_result_operation") not in ("query", "query_result"):
        return None
    round_result = await session.execute(
        select(SubagentWork, Subagent)
        .join(Subagent, Subagent.id == SubagentWork.subagent_id)
        .where(
            SubagentWork.child_work_id == post.child_work_id,
            SubagentWork.subagent_id == post.subagent_id,
            Subagent.id == post.subagent_id,
            Subagent.uid == post.uid,
            Subagent.session_id == post.session_id,
            Subagent.profile_id == post.profile_id,
        )
    )
    row = round_result.one_or_none()
    if row is None:
        return None
    round_row, _subagent = row
    snapshot = round_row.result
    if not isinstance(snapshot, dict) or snapshot.get("completed") is not True:
        return None
    if snapshot.get("work_id") != post.child_work_id or snapshot.get("current_work_id") != round_row.current_work_id or snapshot.get("status") != post.stage:
        return None
    current_work = await session.get(SessionReplyWorkItem, round_row.current_work_id)
    if current_work is None or current_work.uid != post.uid or current_work.session_id != post.session_id or current_work.profile_id != post.profile_id or current_work.subagent_id != post.subagent_id:
        return None
    snapshot_text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    if len(snapshot_text.encode("utf-8")) <= 65_535:
        return None
    newer_post = await session.execute(
        select(SubagentBoardPost.id)
        .where(
            SubagentBoardPost.uid == post.uid,
            SubagentBoardPost.session_id == post.session_id,
            SubagentBoardPost.profile_id == post.profile_id,
            SubagentBoardPost.subagent_id == post.subagent_id,
            SubagentBoardPost.child_work_id == post.child_work_id,
            SubagentBoardPost.id > post.id,
        )
        .limit(1)
    )
    if newer_post.scalar_one_or_none() is not None:
        return None
    hint = _board_hint(snapshot)
    old_content = _board_hint_json(hint, "query")
    new_content = _board_hint_json(hint, "query_result")
    if post.content != old_content and post.content != new_content:
        return None
    return hint


async def _migrate_board_posts(session: AsyncSession, sources: _MigrationSources) -> None:
    async for post in _rows(session, SubagentBoardPost, SubagentBoardPost.id):
        hint = await _validated_board_hint(session, post)
        if hint is None:
            continue
        if _positive_int(post.id):
            sources.board_hints[post.id] = hint
        old_content = _board_hint_json(hint, "query")
        if post.content != old_content:
            continue
        post.content = _board_hint_json(hint, "query_result")
        session.add(post)
        await session.flush()


async def _migrate_work_items(session: AsyncSession, sources: _MigrationSources) -> None:
    async for work in _rows(session, SessionReplyWorkItem, SessionReplyWorkItem.id, lock=True):
        original_state = work.execution_state
        rewritten_state = original_state
        if isinstance(original_state, dict):
            rewritten_state = _rewrite_envelope(original_state, sources)
        checkpoint = await session_reply_work_item_crud.get_checkpoint(session, work.id)
        if isinstance(checkpoint, dict):
            rewritten_checkpoint = _rewrite_envelope(checkpoint, sources)
            if rewritten_checkpoint is not checkpoint:
                await session_reply_work_item_crud.save_checkpoint(session, work.id, rewritten_checkpoint)
        frozen_context = await session_reply_work_item_crud.get_frozen_context(session, work)
        if isinstance(frozen_context, list):
            rewritten_frozen = _rewrite_message_list(frozen_context, sources)
            if rewritten_frozen is not frozen_context:
                frozen_ids = await session_reply_work_item_crud.save_frozen_context(session, work.id, rewritten_frozen)
                rewritten_state = _with_field(rewritten_state, "frozen_context_message_ids", frozen_ids)
        if rewritten_state != original_state:
            work.execution_state = rewritten_state
            session.add(work)
        await session.flush()


async def _migrate_background_tasks(session: AsyncSession, sources: _MigrationSources) -> None:
    async for task in _rows(session, BackgroundTask, BackgroundTask.id):
        changed = False
        if isinstance(task.extra, dict):
            rewritten_extra = _rewrite_envelope(task.extra, sources)
            if rewritten_extra is not task.extra:
                task.extra = rewritten_extra
                changed = True
        if isinstance(task.result, dict):
            rewritten_result = _rewrite_envelope(task.result, sources)
            if rewritten_result is not task.result:
                task.result = rewritten_result
                changed = True
        if changed:
            session.add(task)
            await session.flush()


async def _migrate_events(session: AsyncSession, sources: _MigrationSources) -> None:
    async for event in _rows(session, SessionEvent, SessionEvent.id):
        rewritten = _rewrite_envelope(event.event, sources)
        if rewritten is event.event:
            continue
        event.event = rewritten
        session.add(event)
        await session.flush()
    async for event in _rows(session, SessionReplyStreamEvent, SessionReplyStreamEvent.id):
        rewritten = _rewrite_envelope(event.event, sources)
        if rewritten is event.event:
            continue
        event.event = rewritten
        session.add(event)
        await session.flush()


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    sources = _MigrationSources(result_ids=set(), board_hints={})
    await _validate_checkpoint_hashes(session)
    await _migrate_board_posts(session, sources)
    await _migrate_messages(session, sources)
    await _migrate_audit_result_versions(session, sources)
    await _migrate_subagent_tool_calls(session)
    await _migrate_work_items(session, sources)
    await _migrate_background_tasks(session, sources)
    await _migrate_events(session, sources)
    await session.flush()
