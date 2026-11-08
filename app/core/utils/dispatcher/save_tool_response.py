import json

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim
from app.core.crud.session.subagent import subagent_crud
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
    MessageType,
)


def _find_wait_tool_call(messages: list[InternalMessage], tool_call_id: str | None) -> InternalToolCall | None:
    if tool_call_id is None:
        return None
    assistant = None
    for message in reversed(messages):
        if message.role == MessageRole.ASSISTANT:
            assistant = message
            break
    if assistant is None:
        return None
    for tool_call in assistant.tool_calls or []:
        if tool_call.id == tool_call_id and tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments.get("operation") == "wait":
            return tool_call
    return None


async def save_tool_response(
    db: AsyncSession,
    session_id: str,
    uid: str,
    profile_id: int,
    tool_res: InternalMessage,
    messages: list[InternalMessage],
    turn_messages: list[InternalMessage],
    audit_record_id: int | None = None,
    dedupe_key: str | None = None,
) -> InternalMessage:
    stored_tool_res = tool_res.model_copy()

    subagent_call = None
    wait_tool_call = _find_wait_tool_call(messages, tool_res.tool_call_id)
    if wait_tool_call is not None:
        try:
            result = json.loads(tool_res.content or "{}")
        except (TypeError, ValueError):
            result = None

        claim_info = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
        if not (isinstance(result, dict) and result.get("status") == "queued") and claim_info is not None:
            await ensure_session_reply_work_claim(db)
            candidate_call = await subagent_crud.get_call(
                db,
                parent_work_id=claim_info[0],
                tool_call_id=tool_res.tool_call_id,
            )
            if candidate_call is not None and candidate_call.arguments == wait_tool_call.arguments:
                round_row = await subagent_crud.get_round(
                    db,
                    child_work_id=candidate_call.child_work_id,
                    subagent_id=candidate_call.subagent_id,
                )
                if round_row is not None and isinstance(round_row.result, dict) and round_row.result.get("completed") is True:
                    subagent_call = candidate_call

    saved_msg = await save_message(
        db,
        session_id,
        uid,
        MessageRole.TOOL,
        MessageType.TOOL_RESULT,
        stored_tool_res,
        profile_id,
        is_processed=True,
        audit_record_id=audit_record_id,
        audit_tool_call_id=tool_res.tool_call_id if audit_record_id is not None else None,
        dedupe_key=dedupe_key,
        commit=subagent_call is None,
    )
    stored_tool_res.id = saved_msg.id
    stored_tool_res.created_at = saved_msg.created_at
    stored_tool_res.type = saved_msg.type
    if subagent_call is not None:
        subagent_call.tool_result_message_id = saved_msg.id
        subagent_call.delivered = True
        db.add(subagent_call)
        await db.commit()
    messages.append(stored_tool_res)
    turn_messages.append(stored_tool_res)
    return stored_tool_res
