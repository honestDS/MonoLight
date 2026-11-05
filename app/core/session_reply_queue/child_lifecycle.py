from copy import deepcopy

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_SUBAGENT_CONTEXT_INVALID, GOAL_EXECUTION_PHASE_RUNNING
from app.core.crud.session.reply_work_item import session_reply_work_item_crud as reply_work_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.exceptions import ParameterException
from app.core.session_reply_queue.child_result import get_child_snapshot
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus

__all__ = ["finish_child_work", "prepare_child_completion"]


async def prepare_child_completion(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    result_message_id: int | None,
    skip_reply: bool,
) -> bool:
    if work.subagent_id is None:
        return False

    subagent = await subagent_crud.get_scoped(
        db,
        subagent_id=work.subagent_id,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        lock=True,
        touch=False,
    )
    if subagent is None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

    actual_work = await reply_work_crud.get(db, work.id)
    if actual_work is None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    await db.refresh(actual_work)
    if actual_work.uid != work.uid or actual_work.session_id != work.session_id or actual_work.profile_id != work.profile_id or actual_work.subagent_id != subagent.id:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    if actual_work.status != SessionReplyWorkStatus.RUNNING or actual_work.locked_by != worker_id:
        return False

    execution_state = actual_work.execution_state if isinstance(actual_work.execution_state, dict) else {}
    round_row = await subagent_crud.get_round_for_work(db, actual_work)
    if round_row is None or round_row.subagent_id != subagent.id or round_row.current_work_id != actual_work.id or round_row.child_work_id != execution_state.get("child_work_id", actual_work.id) or subagent.active_child_work_id != round_row.child_work_id or round_row.result is not None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

    result_message: Message | None = None
    if result_message_id is not None:
        result_message = await db.get(Message, result_message_id)
        if result_message is None:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        if result_message.type == MessageType.AUDIT_CONFIRMATION:
            return False
        if result_message.uid != actual_work.uid or result_message.session_id != actual_work.session_id or result_message.profile_id != actual_work.profile_id or result_message.work_id != actual_work.id:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

    pending_messages = await subagent_crud.list_pending_messages(db, work=actual_work)
    if not pending_messages:
        return False

    checkpoint = await reply_work_crud.get_checkpoint(db, actual_work.id)
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("messages"), list):
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    checkpoint = deepcopy(checkpoint)

    checkpoint_messages = checkpoint["messages"]
    if result_message is not None and result_message.role == MessageRole.ASSISTANT and result_message.type == MessageType.TEXT and not any(isinstance(message, dict) and message.get("id") == result_message.id for message in checkpoint_messages):
        parsed_messages = parse_db_messages_to_internal([result_message])
        if not parsed_messages:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        checkpoint_messages.extend(message.model_dump(mode="json", exclude_none=True) for message in parsed_messages)

    checkpoint["current_turn"] = 0
    checkpoint["execution_phase"] = GOAL_EXECUTION_PHASE_RUNNING
    checkpoint["turn_messages"] = []
    checkpoint.pop("reply_skipped", None)
    checkpoint.pop("deferred_external_results", None)

    state = deepcopy(execution_state)
    state.pop("response", None)
    return await subagent_crud.requeue_for_messages(
        db,
        work=actual_work,
        worker_id=worker_id,
        execution_state=state,
        checkpoint=checkpoint,
        message_id=result_message_id,
    )


async def finish_child_work(db: AsyncSession, *, work: SessionReplyWorkItem) -> bool:
    if work.subagent_id is None:
        return False

    subagent = await subagent_crud.get_scoped(
        db,
        subagent_id=work.subagent_id,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        lock=True,
        touch=False,
    )
    if subagent is None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

    actual_work = await reply_work_crud.get(db, work.id)
    if actual_work is None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    await db.refresh(actual_work)
    if actual_work.uid != work.uid or actual_work.session_id != work.session_id or actual_work.profile_id != work.profile_id or actual_work.subagent_id != subagent.id:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

    round_row = await subagent_crud.get_round_for_work(db, actual_work)
    execution_state = actual_work.execution_state if isinstance(actual_work.execution_state, dict) else {}
    if round_row is None or round_row.subagent_id != subagent.id or round_row.current_work_id != actual_work.id or round_row.child_work_id != execution_state.get("child_work_id", actual_work.id) or round_row.result is not None:
        return False

    snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
    if snapshot.get("status") == "waiting_confirmation":
        round_row.waiting_confirmation = True
        db.add(round_row)
        await db.flush()
        return False
    if snapshot.get("completed") is not True:
        return False

    round_row.result = deepcopy(snapshot)
    round_row.waiting_confirmation = False
    if subagent.active_child_work_id == round_row.child_work_id:
        subagent.active_child_work_id = None
    db.add(round_row)
    db.add(subagent)
    await db.flush()
    return True
