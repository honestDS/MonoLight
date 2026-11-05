import json
from copy import deepcopy

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation_results import replace_pending_tool_result
from app.core.constants import AGENT_WORK_TOOL_NAME, ERR_SUBAGENT_CONTEXT_INVALID
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.exceptions import ParameterException
from app.core.session_reply_queue.child_work import build_subagent_queued_result
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.models.subagent import SubagentToolCall

__all__ = ["recover_child_work_wait"]


def _bad() -> None:
    raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)


def _valid_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _messages(value: object) -> list[InternalMessage]:
    if not isinstance(value, list):
        _bad()
    try:
        return [InternalMessage.model_validate(item) for item in value]
    except (TypeError, ValueError):
        _bad()


def _bounds(messages: list[InternalMessage], assistant_id: int, call_id: str) -> tuple[int, int] | None:
    found = [i for i, item in enumerate(messages) if item.role == MessageRole.ASSISTANT and item.id == assistant_id and any(call.id == call_id for call in item.tool_calls or [])]
    if len(found) > 1:
        _bad()
    if not found:
        return None
    end = found[0] + 1
    while end < len(messages) and messages[end].role == MessageRole.TOOL:
        end += 1
    return found[0], end


def _group_result(messages: list[InternalMessage], assistant_id: int, call_id: str, result_id: int | None) -> InternalMessage | None:
    bounds = _bounds(messages, assistant_id, call_id)
    if bounds is None:
        return None
    start, end = bounds
    found = [item for item in messages[start + 1 : end] if item.role == MessageRole.TOOL and (item.tool_call_id == call_id or item.id == result_id)]
    if len(found) > 1:
        _bad()
    return found[0] if found else None


def _put_group_result(messages: list[InternalMessage], assistant_id: int, call_id: str, result: InternalMessage) -> None:
    bounds = _bounds(messages, assistant_id, call_id)
    if bounds is None:
        _bad()
    start, end = bounds
    found = [i for i in range(start + 1, end) if messages[i].role == MessageRole.TOOL and (messages[i].id == result.id or messages[i].tool_call_id == call_id)]
    if len(found) > 1:
        _bad()
    if any(i < start or i >= end for i, item in enumerate(messages) if item.id == result.id or (item.role == MessageRole.TOOL and item.tool_call_id == call_id)):
        _bad()
    if found:
        messages[found[0]] = result.model_copy(deep=True)
    else:
        messages.insert(end, result.model_copy(deep=True))


def _row_result(row: Message) -> InternalMessage:
    try:
        result = InternalMessage.model_validate_json(row.content or "{}")
    except (TypeError, ValueError):
        _bad()
    result.id = row.id
    return result


def _check_row(row: Message, work: SessionReplyWorkItem, scope_work_id: int | None) -> None:
    if not _valid_id(row.id) or row.uid != work.uid or row.session_id != work.session_id or row.profile_id != work.profile_id or row.role != MessageRole.TOOL or row.type != MessageType.TOOL_RESULT or row.work_id != scope_work_id or row.audit_record_id is not None:
        _bad()


async def _history_result(db: AsyncSession, work: SessionReplyWorkItem, assistant: Message, call_id: str) -> Message | None:
    cursor: int | None = None
    while True:
        page = await message_crud.get_history_forward_by_id(db, session_id=work.session_id, uid=work.uid, work_id=assistant.work_id, after_id=assistant.id, page_after_id=cursor, limit=200)
        found: Message | None = None
        for row in page:
            if not _valid_id(row.id) or row.uid != work.uid or row.session_id != work.session_id or row.profile_id != work.profile_id or row.work_id != assistant.work_id:
                _bad()
            if row.role == MessageRole.ASSISTANT:
                break
            if row.role != MessageRole.TOOL or row.type != MessageType.TOOL_RESULT:
                continue
            if _row_result(row).tool_call_id == call_id:
                if found is not None:
                    _bad()
                found = row
        if found is not None:
            return found
        if not page or len(page) < 200 or any(row.role == MessageRole.ASSISTANT for row in page):
            return None
        cursor = page[-1].id


async def _recover_result(db: AsyncSession, work: SessionReplyWorkItem, call: SubagentToolCall, assistant: Message, checkpoint_result: InternalMessage | None, message_work_id: int | None) -> InternalMessage:
    row = await message_crud.get(db, checkpoint_result.id) if checkpoint_result and _valid_id(checkpoint_result.id) else None
    if row is None and _valid_id(call.tool_result_message_id):
        row = await message_crud.get(db, call.tool_result_message_id)
    if row is None:
        row = await _history_result(db, work, assistant, call.tool_call_id)

    queued = build_subagent_queued_result(call)
    queued_content = json.dumps(queued, ensure_ascii=False)
    if row is None:
        result = InternalMessage(role=MessageRole.TOOL, tool_call_id=call.tool_call_id, content=queued_content)
        saved = await save_message(db, work.session_id, work.uid, MessageRole.TOOL, MessageType.TOOL_RESULT, result, work.profile_id, is_processed=True, dedupe_key=f"subagent-queued:{call.id}", commit=False, work_id=message_work_id)
        if not _valid_id(saved.id):
            _bad()
        result.id = saved.id
        return result

    _check_row(row, work, assistant.work_id)
    result = _row_result(row)
    if result.tool_call_id != call.tool_call_id:
        _bad()
    try:
        payload = json.loads(result.content or "{}")
    except (TypeError, ValueError):
        _bad()
    if payload.get("status") == "queued":
        if payload.get("child_work_id") != call.child_work_id or payload.get("subagent_id") != call.subagent_id:
            _bad()
        return result
    if result.tool_execution_status != "execution_unknown" and payload.get("status") != "execution_unknown":
        _bad()
    await replace_pending_tool_result(db, pending_message=row, original_tool_call_id=call.tool_call_id, content=queued_content, tool_execution_status=None, audit_record_id=None)
    return InternalMessage(role=MessageRole.TOOL, tool_call_id=call.tool_call_id, content=queued_content, id=row.id)


async def recover_child_work_wait(db: AsyncSession, *, work: SessionReplyWorkItem, worker_id: str) -> dict | None:
    if work.subagent_id is not None or not _valid_id(work.id):
        return None
    calls = await subagent_crud.list_calls(db, parent_work_id=work.id, undelivered_only=True)
    if not calls:
        return None

    await ensure_session_reply_work_claim(db)
    checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work.id)
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("messages"), list):
        _bad()
    messages = _messages(checkpoint["messages"])
    turn_messages = _messages(checkpoint["turn_messages"]) if "turn_messages" in checkpoint else None
    message_work_id = await session_reply_work_item_crud.get_message_work_id(db, work)
    pending_tools: list[dict] = []
    assistant_ids: set[int] = set()
    turn_changed = False

    for call in calls:
        if call.parent_work_id != work.id or call.delivered is not False or not isinstance(call.tool_call_id, str) or not isinstance(call.arguments, dict) or not _valid_id(call.id):
            _bad()
        matches = [(assistant, tool_call) for assistant in messages if assistant.role == MessageRole.ASSISTANT for tool_call in assistant.tool_calls or [] if tool_call.id == call.tool_call_id and tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments == call.arguments]
        if len(matches) != 1:
            _bad()
        assistant_internal, _ = matches[0]
        if not _valid_id(assistant_internal.id):
            _bad()
        assistant_ids.add(assistant_internal.id)
        assistant = await message_crud.get(db, assistant_internal.id)
        if assistant is None or assistant.uid != work.uid or assistant.session_id != work.session_id or assistant.profile_id != work.profile_id or assistant.role != MessageRole.ASSISTANT or assistant.type != MessageType.TOOL_CALL:
            _bad()
        if _bounds(messages, assistant_internal.id, call.tool_call_id) is None:
            _bad()
        existing = _group_result(messages, assistant_internal.id, call.tool_call_id, call.tool_result_message_id)
        if existing is None and turn_messages is not None:
            existing = _group_result(turn_messages, assistant_internal.id, call.tool_call_id, call.tool_result_message_id)
        result = await _recover_result(db, work, call, assistant, existing, message_work_id)
        if not _valid_id(result.id):
            _bad()
        _put_group_result(messages, assistant_internal.id, call.tool_call_id, result)
        if turn_messages is not None and _bounds(turn_messages, assistant_internal.id, call.tool_call_id) is not None:
            _put_group_result(turn_messages, assistant_internal.id, call.tool_call_id, result)
            turn_changed = True
        call.tool_result_message_id = result.id
        call.response_id = call.response_id or None
        db.add(call)
        pending_tools.append({"child_work_id": call.child_work_id, "subagent_id": call.subagent_id, "tool_call_id": call.tool_call_id, "tool_name": AGENT_WORK_TOOL_NAME, "tool_result_message_id": result.id, "response_id": call.response_id})

    new_checkpoint = deepcopy(checkpoint)
    new_checkpoint["messages"] = [item.model_dump(mode="json", exclude_none=True) for item in messages]
    if turn_messages is not None and turn_changed:
        new_checkpoint["turn_messages"] = [item.model_dump(mode="json", exclude_none=True) for item in turn_messages]
    claimed = await session_reply_work_item_crud.update_claimed(db, work_id=work.id, worker_id=worker_id, values={}, checkpoint=new_checkpoint, commit=False)
    if not claimed:
        await db.rollback()
        return None
    await db.commit()
    from app.core.session_reply_queue.executor_resume import restore_interactive_tool_replies
    from app.core.utils.dispatcher.external_work import build_terminal_wait_response

    restored_checkpoint = await restore_interactive_tool_replies(db, work=work, worker_id=worker_id, checkpoint=new_checkpoint)
    restored_messages = _messages(restored_checkpoint["messages"])
    tool_calls = []
    tool_results = []
    collecting = False
    for message in restored_messages:
        if message.role == MessageRole.ASSISTANT:
            collecting = message.id in assistant_ids
            if collecting:
                tool_calls.extend(message.tool_calls or [])
        elif collecting:
            if message.role == MessageRole.TOOL:
                tool_results.append(message)
            else:
                collecting = False
    external_wait = await build_terminal_wait_response(
        db,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        tool_calls=tool_calls,
        tool_results=tool_results,
    )
    if external_wait is not None:
        for pending_tool in external_wait["pending_tools"]:
            if "child_work_id" not in pending_tool:
                pending_tools.append(pending_tool)
    return {"status": "waiting_external_work", "pending_tools": pending_tools}
