from typing import Any

from pydantic import ValidationError

from app.core.constants import ERR_LLM_UNEXPECTED_ERROR, ERR_SUBAGENT_CONTEXT_INVALID
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.session_reply_queue.executor_common import _response_content
from app.core.session_reply_queue.executor_interactive import _dispatch_interactive_work
from app.core.session_reply_queue.executor_resume import restore_interactive_tool_replies
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.dispatcher.helpers import dump_background_proactive_history
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.message import InternalMessage, MessageRole
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem

__all__ = ["execute_agent_work", "load_agent_work_spec"]


def load_agent_work_spec(work: SessionReplyWorkItem) -> AgentWorkSpec:
    state = work.execution_state if isinstance(work.execution_state, dict) else {}
    try:
        return AgentWorkSpec.model_validate(state.get("agent_work"))
    except ValidationError as exc:
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR)) from exc


async def _load_instruction_message(db, work: SessionReplyWorkItem, spec: AgentWorkSpec) -> InternalMessage:
    message_id = spec.instruction_message_id
    if message_id is None:
        return InternalMessage(role=MessageRole.USER, content=spec.instruction)

    message = await message_crud.get(db, message_id)
    if message is None or message.uid != work.uid or message.session_id != work.session_id or message.profile_id != work.profile_id or message.role != MessageRole.USER or (spec.report_target == AgentWorkReportTarget.PARENT_WORK and message.work_id != work.id):
        if spec.report_target == AgentWorkReportTarget.PARENT_WORK:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))

    parsed = parse_db_messages_to_internal([message])
    initial_message = next((item for item in parsed if item.role == MessageRole.USER), None)
    if initial_message is None:
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    initial_message.content = spec.instruction
    return initial_message


async def execute_agent_work(db, work: SessionReplyWorkItem, worker_id: str = "") -> dict[str, Any]:
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))

    spec = load_agent_work_spec(work)
    if spec.report_target == AgentWorkReportTarget.PARENT_WORK:
        if work.source_type != SessionReplySourceType.PARENT_WORK or work.source_id != str(spec.parent_work_id) or work.subagent_id != spec.subagent_id:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

        subagent = await subagent_crud.get_scoped(
            db,
            subagent_id=spec.subagent_id,
            uid=work.uid,
            session_id=work.session_id,
            profile_id=work.profile_id,
        )
        round_row = await subagent_crud.get_round_for_work(db, work)
        call = await subagent_crud.get_call(
            db,
            parent_work_id=spec.parent_work_id,
            tool_call_id=spec.parent_tool_call_id,
        )
        if subagent is None or round_row is None or call is None or round_row.result is not None or round_row.waiting_confirmation is not False or subagent.active_child_work_id != round_row.child_work_id or call.child_work_id != round_row.child_work_id or call.subagent_id != subagent.id:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
    if execution_state.get("frozen_context_message_ids") != []:
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))

    checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work.id)
    resume_state = None
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("messages"), list):
        if checkpoint.get("reload_history") is True:
            resume_state = checkpoint
        else:
            resume_state = await restore_interactive_tool_replies(
                db,
                work=work,
                worker_id=worker_id,
                checkpoint=checkpoint,
            )

    initial_message = await _load_instruction_message(db, work, spec)
    instruction_message_ids = [spec.instruction_message_id] if spec.instruction_message_id is not None else []
    interactive_response = await _dispatch_interactive_work(
        db,
        work=work,
        worker_id=worker_id,
        message=spec.instruction,
        initial_message=initial_message,
        history_before_id=spec.instruction_message_id,
        frozen_user_message_ids=instruction_message_ids,
        attachments=initial_message.attachments,
        allow_additional_user_messages=False,
        execution_resume_state=resume_state,
        execution_scope=spec.execution_scope,
        frozen_context_messages=[],
    )

    if interactive_response.get("status") == "waiting_external_work" or interactive_response.get("skip_reply") is True:
        return interactive_response

    history = interactive_response.get("history")
    history_messages = [InternalMessage.model_validate(message) for message in history] if isinstance(history, list) else []
    response = {
        "content": parse_assistant_files_content(_response_content(interactive_response)),
        "history": dump_background_proactive_history(history_messages),
        "files": interactive_response.get("files") or [],
    }
    if interactive_response.get("llm_request_metadata") is not None:
        response["llm_request_metadata"] = interactive_response["llm_request_metadata"]
    response_id = interactive_response.get("response_id")
    if isinstance(response_id, str) and response_id:
        response["response_id"] = response_id
    return response
