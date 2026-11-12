import json
from copy import deepcopy
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.utils.dispatcher.checkpoint_message import checkpoint_message_hash
from app.models.audit import AuditRecord, AuditToolResultVersion
from app.models.background_task import BackgroundTask
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import SessionReplyCheckpointMessage, SessionReplyCheckpointMessageReference, SessionReplyWorkItem
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261112_02_flatten_subagent_tool_results"
_BATCH_SIZE = 200
_OPS = {"create", "send_message", "query", "query_result", "wait", "cancel", "list", "board_post", "board_read"}
_ROUND_OPS = {"create", "send_message", "query", "query_result", "wait", "cancel"}
_MESSAGE_KEYS = ("history", "messages", "turn_messages", "request_context", "frozen_context_messages", "external_resume_messages")
_LIST_KEYS = ("tasks", "external_resume_context", "external_results", "deferred_external_results")
_MARKERS = {MessageType.SUBAGENT_RESULT.value, MessageType.SUBAGENT_RESULT.name, MessageType.BACKGROUND_TASK_RESULT.value, MessageType.BACKGROUND_TASK_RESULT.name}
_META = (
    "audit_record_id",
    "audit_execution_record_id",
    "audit_tool_detail_id",
    "audit_tool_call_id",
    "audit_status",
    "audit_execution_status",
    "audit_claim_token",
    "confirmation_decision",
    "confirmation_status",
    "rejection_source",
    "full_result_operation",
    "full_result_path",
    "full_result_saved",
    "original_chars",
    "original_tokens",
    "final_tokens",
    "removed_chars",
    "truncated",
    "bytes_read",
    "size",
    "sha256",
    "error_code",
    "execution_status",
)


def _id(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _op(value: Any) -> str | None:
    return value if isinstance(value, str) and value in _OPS else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _calls(value: Any) -> list[dict[str, Any]]:
    return [item for item in value.get("tool_calls", []) if isinstance(item, dict)] if isinstance(value, dict) and isinstance(value.get("tool_calls"), list) else []


def _agent_call(value: Any) -> tuple[str, dict[str, Any], str] | None:
    if not isinstance(value, dict):
        return None
    args = value.get("arguments")
    call_id = value.get("id")
    operation = _op(args.get("operation")) if isinstance(args, dict) else None
    if value.get("name") != "agent_work" or not isinstance(call_id, str) or not call_id.strip() or not isinstance(args, dict) or operation is None:
        return None
    return call_id, args, operation


def _same_op(left: Any, right: str) -> bool:
    return isinstance(left, str) and (left == right or {left, right} == {"query", "query_result"})


def _metadata(target: dict[str, Any], *sources: Any) -> None:
    for source in sources:
        if isinstance(source, dict):
            for key in _META:
                if key in source and key not in target:
                    target[key] = deepcopy(source[key])


def _rewrite_raw(value: Any, convert) -> Any:
    if isinstance(value, dict):
        result = convert(value)
        return value if result is None or result == value else result
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    try:
        payload, end = json.JSONDecoder().raw_decode(stripped)
    except (TypeError, ValueError):
        return value
    result = convert(payload)
    if result is None or result == payload:
        return value
    return value[: len(value) - len(stripped)] + _json(result) + stripped[end:]


def _rewrite_json(value: Any, operation: str | None, board_edits: dict[int, tuple[str, str, int, str, str]] | None = None) -> Any:
    def convert(payload: Any) -> Any:
        if not isinstance(payload, dict) or not isinstance(payload.get("operation"), str):
            return None
        current_operation = _op(payload["operation"])
        if current_operation is None or (operation is not None and not _same_op(current_operation, operation)):
            return None
        rewritten = _flatten(payload, current_operation, board_edits or {})
        if rewritten is not None:
            _metadata(rewritten, payload)
        return rewritten

    return _rewrite_raw(value, convert)


def _flatten_work(snapshot: Any, subagent_id: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict) or _id(snapshot.get("work_id")) is None or not isinstance(snapshot.get("status"), str) or not isinstance(subagent_id, str) or not subagent_id.strip():
        return None
    result_payload = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
    result: dict[str, Any] = {"subagent_id": subagent_id, "work_id": snapshot["work_id"], "status": snapshot["status"]}
    if result_payload.get("content"):
        result["content"] = deepcopy(result_payload["content"])
    if result_payload.get("files"):
        result["files"] = deepcopy(result_payload["files"])
    if result["status"] == "succeeded" and result_payload.get("skip_reply") is True:
        result["status"] = "ended_without_reply"
    if snapshot.get("error"):
        result["reason"] = deepcopy(snapshot["error"])
    _metadata(result, snapshot, result_payload)
    return result


def _flatten(payload: dict[str, Any], operation: str, board_edits: dict[int, tuple[str, str, int, str, str]]) -> dict[str, Any] | None:
    if operation in {"create", "send_message", "wait"} and payload.get("status") == "queued":
        has_work_id = "work_id" in payload
        has_child_work_id = "child_work_id" in payload
        work_id = _id(payload.get("work_id")) if has_work_id else None
        child_work_id = _id(payload.get("child_work_id")) if has_child_work_id else None
        if operation == "wait" and not has_work_id:
            work_id = child_work_id
        subagent_id = payload.get("subagent_id")
        if work_id is None or (has_child_work_id and (child_work_id is None or child_work_id != work_id)) or not isinstance(subagent_id, str) or not subagent_id.strip():
            return None
        result = {"subagent_id": subagent_id, "work_id": work_id, "status": "queued"}
        return result
    if operation in _ROUND_OPS and isinstance(payload.get("work"), dict):
        work = payload["work"]
        work_id = _id(work.get("work_id", work.get("child_work_id")))
        old_ids = [value for value in (payload.get("work_id"), payload.get("child_work_id")) if value is not None]
        if work_id is None or any(_id(value) != work_id for value in old_ids):
            return None
        if "work_id" not in work:
            work = {**work, "work_id": work_id}
        return _flatten_work(work, payload.get("subagent_id") or work.get("subagent_id"))
    if operation == "list" and payload.get("status") == "success" and isinstance(payload.get("subagents"), list):
        entries = []
        for item in payload["subagents"]:
            if not isinstance(item, dict):
                return None
            work = item.get("work") if isinstance(item.get("work"), dict) else item
            work_id = _id(work.get("work_id", work.get("child_work_id")))
            status = work.get("status")
            if work_id is None or not isinstance(status, str) or not isinstance(item.get("subagent_id"), str) or not isinstance(item.get("name"), str):
                return None
            entries.append({"subagent_id": item["subagent_id"], "name": item["name"], "work_id": work_id, "status": status})
        return {"subagents": entries}
    if operation == "board_post" and payload.get("status") == "success":
        return {"status": "saved"}
    if operation == "board_read" and payload.get("status") == "success" and isinstance(payload.get("posts"), list):
        posts = []
        for item in payload["posts"]:
            if not isinstance(item, dict) or _id(item.get("child_work_id", item.get("work_id"))) is None or not isinstance(item.get("stage"), str) or not isinstance(item.get("content"), str):
                return None
            work_id, stage, content = item.get("child_work_id", item.get("work_id")), item["stage"], item["content"]
            edit = board_edits.get(_id(item.get("id")))
            if edit is not None and (stage, content, work_id) == edit[:3]:
                stage, content = edit[3], edit[4]
            posts.append({"work_id": work_id, "stage": stage, "content": content})
        return {"posts": posts}
    if payload.get("status") == "failed" and "work" not in payload:
        result = {}
        if payload.get("error"):
            result["error"] = deepcopy(payload["error"])
        if payload.get("details"):
            result["details"] = deepcopy(payload["details"])
        return result or None
    return None


def _call_op(calls: dict[tuple[str, str, int, int | None, str], str | None], scope: tuple[str, str, int, int | None], call_id: Any) -> str | None:
    if not isinstance(call_id, str):
        return None
    operation = calls.get((*scope, call_id))
    return operation if operation is not None else None


def _result_op(result_ids: dict[int, tuple[str, str, int, str, str, int | None]], message: dict[str, Any], scope: tuple[str, str, int, int | None], calls: dict[tuple[str, str, int, int | None, str], str | None]) -> str | None:
    call_id = message.get("tool_call_id")
    result_id = _id(message.get("id"))
    saved = result_ids.get(result_id) if result_id is not None else None
    if saved is not None and saved[:3] == scope[:3] and saved[3] == call_id:
        return saved[4]
    return _call_op(calls, scope, call_id)


def _rewrite_messages(values: Any, scope: tuple[str, str, int, int | None], calls: dict[tuple[str, str, int, int | None, str], str | None], result_ids: dict[int, tuple[str, str, int, str, str, int | None]], boards: dict[int, tuple[str, str, int, str, str]]) -> Any:
    if not isinstance(values, list):
        return values
    pending: dict[str, str] = {}
    output, changed = [], False
    for item in values:
        rewritten = item
        if isinstance(item, dict) and item.get("role") == MessageRole.ASSISTANT.value:
            pending = {}
            for call in _calls(item):
                parsed = _agent_call(call)
                if parsed is not None:
                    pending[parsed[0]] = parsed[2]
        elif isinstance(item, dict) and item.get("role") == MessageRole.TOOL.value:
            call_id = item.get("tool_call_id")
            binding = pending.get(call_id) if isinstance(call_id, str) else None
            if binding is None:
                resolved = _result_op(result_ids, item, scope, calls)
                binding = resolved
            if binding is not None:
                content = _rewrite_json(item.get("content"), binding, boards)
                if content != item.get("content"):
                    rewritten = dict(item)
                    rewritten["content"] = content
        else:
            pending = {}
            if isinstance(item, dict) and item.get("role") == MessageRole.USER.value and item.get("type") in _MARKERS:
                content = _rewrite_raw(item.get("content"), lambda value: _rewrite_envelope(value, scope, calls, result_ids, boards))
                if content != item.get("content"):
                    rewritten = dict(item)
                    rewritten["content"] = content
        changed = changed or rewritten != item
        output.append(rewritten)
    return output if changed else values


def _rewrite_envelope(value: Any, scope: tuple[str, str, int, int | None], calls: dict[tuple[str, str, int, int | None, str], str | None], result_ids: dict[int, tuple[str, str, int, str, str, int | None]], boards: dict[int, tuple[str, str, int, str, str]]) -> Any:
    if not isinstance(value, dict):
        return value
    result = value

    def put(key: str, replacement: Any) -> None:
        nonlocal result
        if replacement == value.get(key):
            return
        if result is value:
            result = dict(value)
        result[key] = replacement

    if value.get("type") == "tool_end" and value.get("name") == "agent_work":
        put("result", _rewrite_json(value.get("result"), None, boards))
    elif value.get("type") == "external_work_resume" and value.get("source") == "subagent":
        call = value.get("tool_call")
        parsed = _agent_call(call)
        if parsed is not None and isinstance(value.get("results"), list):
            put("results", [_rewrite_json(item, parsed[2], boards) if isinstance(item, (dict, str)) else item for item in value["results"]])
    elif value.get("type") == "background_tool_result":
        parsed = _agent_call(value.get("tool_call"))
        if parsed is not None:
            if "tool_result" in value:
                tool_result = value["tool_result"]
                if isinstance(tool_result, dict) and tool_result.get("role") == MessageRole.TOOL.value:
                    tool_result = dict(tool_result)
                    tool_result["content"] = _rewrite_json(tool_result.get("content"), parsed[2], boards)
                else:
                    tool_result = _rewrite_json(tool_result, parsed[2], boards)
                put("tool_result", tool_result)
            if "task" in value:
                put("task", _rewrite_json(value["task"], parsed[2], boards))
    for key in _MESSAGE_KEYS:
        if isinstance(value.get(key), list):
            put(key, _rewrite_messages(value[key], scope, calls, result_ids, boards))
    for key in ("response", "task", "tool_result"):
        if value.get("type") == "background_tool_result" and key in {"task", "tool_result"}:
            continue
        if isinstance(value.get(key), dict):
            put(key, _rewrite_envelope(value[key], scope, calls, result_ids, boards))
    for key in _LIST_KEYS:
        if isinstance(value.get(key), list):
            put(key, [_rewrite_envelope(item, scope, calls, result_ids, boards) if isinstance(item, dict) else item for item in value[key]])
    return result


async def _rows(session: AsyncSession, model: Any, key: Any, *, lock: bool = False):
    last = 0
    while True:
        statement = select(model).where(key > last).order_by(key.asc()).limit(_BATCH_SIZE)
        if lock:
            statement = statement.with_for_update()
        rows = list((await session.execute(statement)).scalars().all())
        if not rows:
            return
        for row in rows:
            yield row
        last = _id(getattr(rows[-1], key.key, None))
        if last is None:
            raise RuntimeError(f"{MIGRATION_ID}: invalid keyset in {model.__name__}")


async def _result_bindings(session: AsyncSession) -> dict[int, tuple[str, str, int, str, str, int | None]]:
    result: dict[int, tuple[str, str, int, str, str, int | None]] = {}
    async for call in _rows(session, SubagentToolCall, SubagentToolCall.id):
        message_id = _id(call.tool_result_message_id)
        operation = _op(call.arguments.get("operation")) if isinstance(call.arguments, dict) else None
        if message_id is None or operation is None:
            continue
        parent = await session.get(SessionReplyWorkItem, call.parent_work_id)
        subagent = await session.get(Subagent, call.subagent_id) if parent is not None else None
        if parent is not None and subagent is not None and subagent.uid == parent.uid and subagent.session_id == parent.session_id and subagent.profile_id == parent.profile_id:
            message = await session.get(Message, message_id)
            if message is not None and message.uid == parent.uid and message.session_id == parent.session_id and message.profile_id == parent.profile_id and message.role == MessageRole.TOOL and message.type == MessageType.TOOL_RESULT:
                result[message_id] = (parent.uid, parent.session_id, parent.profile_id, call.tool_call_id, operation, message.audit_record_id)
    return result


async def _collect_calls(session: AsyncSession) -> dict[tuple[str, str, int, int | None, str], str | None]:
    calls: dict[tuple[str, str, int, int | None, str], str | None] = {}
    async for message in _rows(session, Message, Message.id):
        if message.role != MessageRole.ASSISTANT or message.type != MessageType.TOOL_CALL:
            continue
        payload = _dict(message.content)
        scope = (message.uid, message.session_id, message.profile_id, _id(message.work_id))
        for call in _calls(payload):
            parsed = _agent_call(call)
            if parsed is None:
                continue
            key = (*scope, parsed[0])
            if key not in calls:
                calls[key] = parsed[2]
            elif calls[key] is not None and calls[key] != parsed[2]:
                calls[key] = None
    return calls


async def _validate_hashes(session: AsyncSession) -> None:
    ids = list((await session.execute(select(SessionReplyCheckpointMessageReference.message_id).distinct().order_by(SessionReplyCheckpointMessageReference.message_id))).scalars().all())
    for start in range(0, len(ids), _BATCH_SIZE):
        batch = ids[start : start + _BATCH_SIZE]
        rows = {row.id: row for row in (await session.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id.in_(batch)))).scalars().all()}
        for message_id in batch:
            row = rows.get(message_id)
            if row is None or checkpoint_message_hash(row.payload) != row.content_hash:
                raise RuntimeError(f"{MIGRATION_ID}: invalid checkpoint message hash {message_id}")


async def _plan_boards(session: AsyncSession) -> dict[int, tuple[str, str, int, str, str]]:
    edits: dict[int, tuple[str, str, int, str, str]] = {}
    async for post in _rows(session, SubagentBoardPost, SubagentBoardPost.id):
        round_row = await session.get(SubagentWork, post.child_work_id)
        snapshot = round_row.result if round_row is not None and isinstance(round_row.result, dict) else None
        if snapshot is None or snapshot.get("work_id") != post.child_work_id or snapshot.get("current_work_id") != round_row.current_work_id or snapshot.get("completed") is not True or post.stage != snapshot.get("status"):
            continue
        subagent = await session.get(Subagent, post.subagent_id)
        current = await session.get(SessionReplyWorkItem, round_row.current_work_id)
        if (
            subagent is None
            or current is None
            or subagent.uid != post.uid
            or subagent.session_id != post.session_id
            or subagent.profile_id != post.profile_id
            or round_row.subagent_id != subagent.id
            or current.uid != post.uid
            or current.session_id != post.session_id
            or current.profile_id != post.profile_id
            or current.subagent_id != subagent.id
        ):
            continue
        snapshot_text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        inner = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        hint_base = {
            "work_id": snapshot.get("work_id"),
            "current_work_id": snapshot.get("current_work_id"),
            "completed": snapshot.get("completed"),
            "status": snapshot.get("status"),
        }
        old_hints = set()
        if len(snapshot_text.encode("utf-8")) > 65_535:
            for operation in ("query", "query_result"):
                hint = {**hint_base, "full_result_operation": operation}
                old_hints.update({_json(hint), json.dumps(hint, ensure_ascii=False, sort_keys=True)})
        if post.content != snapshot_text and post.content not in old_hints:
            continue
        newer = await session.execute(
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
        if newer.scalar_one_or_none() is not None:
            continue
        content = inner.get("content") or snapshot.get("error") or ""
        if not isinstance(content, str) or len(content.encode("utf-8")) > 65_535 or (not content and inner.get("files")):
            content = json.dumps({"full_result_operation": "query_result"}, ensure_ascii=False)
        stage = "ended_without_reply" if snapshot.get("status") == "succeeded" and inner.get("skip_reply") is True else snapshot.get("status")
        edits[post.id] = (post.stage, post.content, post.child_work_id, stage, content)
    return edits


async def _migrate_messages(session: AsyncSession, calls: dict[tuple[str, str, int, int | None, str], str | None], result_ids: dict[int, tuple[str, str, int, str, str, int | None]], boards: dict[int, tuple[str, str, int, str, str]]) -> None:
    async for message in _rows(session, Message, Message.id):
        is_tool_result = message.role == MessageRole.TOOL and message.type == MessageType.TOOL_RESULT
        is_marker = message.role == MessageRole.USER and message.type in _MARKERS
        if not is_tool_result and not is_marker:
            continue
        outer = _dict(message.content)
        if outer is None:
            continue
        scope = (message.uid, message.session_id, message.profile_id, _id(message.work_id))
        if is_tool_result:
            outer_tool_call_id = outer.get("tool_call_id")
            if not isinstance(outer_tool_call_id, str):
                continue
            info = result_ids.get(message.id)
            verified = info is not None and info[:3] == scope[:3] and (info[3] == outer_tool_call_id or info[3] == message.audit_tool_call_id)
            operation = info[4] if verified else _call_op(calls, scope, outer_tool_call_id)
            if message.audit_record_id is not None or message.audit_tool_call_id is not None:
                continue
            if operation is None:
                continue
            result_ids[message.id] = (
                message.uid,
                message.session_id,
                message.profile_id,
                outer_tool_call_id,
                operation,
                message.audit_record_id,
            )
            rewritten = dict(outer)
            rewritten["content"] = _rewrite_json(outer.get("content"), operation, boards)
        else:
            rewritten = _rewrite_raw(outer, lambda value: _rewrite_envelope(value, scope, calls, result_ids, boards))
        if rewritten == outer:
            continue
        message.content = _json(rewritten)
        message.content_revision = int(message.content_revision or 0) + 1
        session.add(message)
        await session.execute(update(ChatSession).where(ChatSession.uid == message.uid, ChatSession.session_id == message.session_id).values(context_content_revision=ChatSession.context_content_revision + 1))


async def _migrate_audit(session: AsyncSession, calls: dict[tuple[str, str, int, int | None, str], str | None], boards: dict[int, tuple[str, str, int, str, str]]) -> None:
    async for version in _rows(session, AuditToolResultVersion, AuditToolResultVersion.id):
        result_message = await session.get(Message, version.message_id)
        source = await session.get(Message, version.source_assistant_message_id)
        record = await session.get(AuditRecord, version.audit_record_id)
        if (
            result_message is None
            or source is None
            or record is None
            or result_message.uid != version.uid
            or result_message.session_id != version.session_id
            or result_message.profile_id != source.profile_id
            or result_message.audit_record_id != version.audit_record_id
            or result_message.audit_tool_call_id != version.original_tool_call_id
            or result_message.role != MessageRole.TOOL
            or result_message.type != MessageType.TOOL_RESULT
            or source.uid != version.uid
            or source.session_id != version.session_id
            or source.role != MessageRole.ASSISTANT
            or source.type != MessageType.TOOL_CALL
            or record.uid != version.uid
            or record.session_id != version.session_id
            or record.source_assistant_message_id != source.id
        ):
            continue
        old_version_content = version.content
        outer = _dict(old_version_content)
        if outer is None or outer.get("role") != MessageRole.TOOL.value or outer.get("tool_call_id") != version.original_tool_call_id:
            continue
        scope = (source.uid, source.session_id, source.profile_id, _id(source.work_id))
        operation = calls.get((*scope, version.original_tool_call_id))
        if operation is None:
            continue
        inner = _rewrite_json(outer.get("content"), operation, boards)
        if inner == outer.get("content"):
            continue
        outer["content"] = inner
        rewritten = _json(outer)
        version.content = rewritten
        session.add(version)
        latest = await session.execute(
            select(AuditToolResultVersion.version_no)
            .where(
                AuditToolResultVersion.audit_record_id == version.audit_record_id,
                AuditToolResultVersion.original_tool_call_id == version.original_tool_call_id,
                AuditToolResultVersion.uid == version.uid,
                AuditToolResultVersion.session_id == version.session_id,
                AuditToolResultVersion.source_assistant_message_id == version.source_assistant_message_id,
            )
            .order_by(AuditToolResultVersion.version_no.desc())
            .limit(1)
        )
        if latest.scalar_one_or_none() != version.version_no or result_message.content != old_version_content or result_message.content_revision != version.version_no:
            continue
        result_message.content = rewritten
        result_message.content_revision = version.version_no
        session.add(result_message)
        await session.execute(update(ChatSession).where(ChatSession.uid == result_message.uid, ChatSession.session_id == result_message.session_id).values(context_content_revision=ChatSession.context_content_revision + 1))


async def _migrate_work(session: AsyncSession, calls: dict, result_ids: dict, boards: dict) -> None:
    async for work in _rows(session, SessionReplyWorkItem, SessionReplyWorkItem.id, lock=True):
        scope = (work.uid, work.session_id, work.profile_id, work.id)
        if isinstance(work.execution_state, dict):
            state = _rewrite_envelope(work.execution_state, scope, calls, result_ids, boards)
            if state != work.execution_state:
                work.execution_state = state
        checkpoint = await session_reply_work_item_crud.get_checkpoint(session, work.id)
        if isinstance(checkpoint, dict):
            rewritten = _rewrite_envelope(checkpoint, scope, calls, result_ids, boards)
            if rewritten != checkpoint:
                await session_reply_work_item_crud.save_checkpoint(session, work.id, rewritten)
        frozen = await session_reply_work_item_crud.get_frozen_context(session, work)
        if isinstance(frozen, list):
            rewritten = _rewrite_messages(frozen, scope, calls, result_ids, boards)
            if rewritten != frozen:
                ids = await session_reply_work_item_crud.save_frozen_context(session, work.id, rewritten)
                state = dict(work.execution_state or {})
                state["frozen_context_message_ids"] = ids
                work.execution_state = state
        session.add(work)


async def _migrate_other(session: AsyncSession, calls: dict, result_ids: dict, boards: dict[int, tuple[str, str, int, str, str]]) -> None:
    async for task in _rows(session, BackgroundTask, BackgroundTask.id):
        extra = task.extra if isinstance(task.extra, dict) else None
        scope = (task.uid, task.session_id, task.profile_id, _id(extra.get("origin_work_id")) if extra else None)
        changed = False
        if extra is not None:
            rewritten = _rewrite_envelope(extra, scope, calls, result_ids, boards)
            if rewritten != extra:
                task.extra = rewritten
                changed = True
        if isinstance(task.result, dict):
            rewritten = _rewrite_envelope(task.result, scope, calls, result_ids, boards)
            if rewritten != task.result:
                task.result = rewritten
                changed = True
        if changed:
            session.add(task)
    async for event in _rows(session, SessionEvent, SessionEvent.id):
        work_id = _id(event.event.get("work_id")) if isinstance(event.event, dict) else None
        work = await session.get(SessionReplyWorkItem, work_id) if work_id is not None else None
        scope = (event.uid, event.session_id, work.profile_id if work is not None else 0, work_id)
        rewritten = _rewrite_envelope(event.event, scope, calls, result_ids, boards)
        if rewritten != event.event:
            event.event = rewritten
            session.add(event)
    async for event in _rows(session, SessionReplyStreamEvent, SessionReplyStreamEvent.id):
        work = await session.get(SessionReplyWorkItem, event.work_id)
        if work is not None:
            rewritten = _rewrite_envelope(event.event, (work.uid, work.session_id, work.profile_id, work.id), calls, result_ids, boards)
            if rewritten != event.event:
                event.event = rewritten
                session.add(event)


async def _migrate_boards(session: AsyncSession, boards: dict[int, tuple[str, str, int, str, str]]) -> None:
    async for post in _rows(session, SubagentBoardPost, SubagentBoardPost.id):
        edit = boards.get(post.id)
        if edit is not None and (post.stage, post.content, post.child_work_id) == edit[:3]:
            post.stage, post.content = edit[3], edit[4]
            session.add(post)


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    await _validate_hashes(session)
    result_ids = await _result_bindings(session)
    calls = await _collect_calls(session)
    boards = await _plan_boards(session)
    await _migrate_messages(session, calls, result_ids, boards)
    await _migrate_audit(session, calls, boards)
    await _migrate_work(session, calls, result_ids, boards)
    await _migrate_other(session, calls, result_ids, boards)
    await _migrate_boards(session, boards)
    await session.flush()
