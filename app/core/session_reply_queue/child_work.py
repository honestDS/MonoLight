import hashlib
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.audit.confirmation_events import broadcast_pending_confirmation_cancellation
from app.core.audit.confirmation_results import cancel_persisted_pending_confirmation_bundle
from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    ERR_SUBAGENT_CALL_CONFLICT,
    ERR_SUBAGENT_CANCELLED,
    ERR_SUBAGENT_CONTEXT_INVALID,
    ERR_SUBAGENT_NOT_FOUND,
    ERR_SUBAGENT_WORK_NOT_FOUND,
    GOAL_EXECUTION_PHASE_RUNNING,
    LOG_DISPATCHER_MAIN_AGENT_MESSAGE,
    MSG_SUBAGENT_SUBMITTED,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SHELL_TOOL_NAME,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import (
    ensure_session_reply_work_claim,
    session_reply_work_item_crud,
)
from app.core.crud.session.subagent import subagent_crud
from app.core.crud.task.background import background_task_crud
from app.core.crud.terminal.session import terminal_session_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.log import agent_log_context, get_logger
from app.core.session_reply_queue.child_delivery import deliver_child_work_results
from app.core.session_reply_queue.child_lifecycle import finish_child_work
from app.core.session_reply_queue.child_result import (
    _parse_audit_record_id,
    get_child_snapshot,
    load_child_work,
)
from app.core.session_reply_queue.executor_audit import (
    _mark_audit_execution_unknown_reliably,
    get_bound_audit_execution,
)
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.terminal.audit_lifecycle import cleanup_terminal_session
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.audit import AuditRecordStatus
from app.models.message import Message, MessageContentFormat, MessageRole, MessageType
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall

logger = get_logger(__name__)

__all__ = [
    "build_subagent_queued_result",
    "cancel_subagent",
    "create_subagent",
    "list_subagents",
    "post_subagent_board",
    "query_subagent",
    "read_subagent_board",
    "send_subagent_message",
]


async def _load_claimed_work(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    lock_claim: bool = True,
) -> SessionReplyWorkItem:
    db_info = getattr(db, "info", None)
    claim_info = db_info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY) if isinstance(db_info, dict) else None
    if not isinstance(claim_info, tuple) or len(claim_info) != 2 or not isinstance(claim_info[0], int) or isinstance(claim_info[0], bool) or claim_info[0] <= 0 or not isinstance(claim_info[1], str) or not claim_info[1].strip():
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

    if lock_claim:
        await ensure_session_reply_work_claim(db)
    parent = await session_reply_work_item_crud.get(db, claim_info[0])
    if parent is None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    await db.refresh(parent)

    if parent.uid != uid or parent.session_id != session_id or parent.profile_id != profile_id:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    if lock_claim and (parent.status != SessionReplyWorkStatus.RUNNING or parent.locked_by != claim_info[1]):
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    return parent


async def _load_parent(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
) -> SessionReplyWorkItem:
    parent = await _load_claimed_work(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
    )
    if parent.work_category != SessionReplyWorkCategory.FOREGROUND or getattr(parent, "subagent_id", None) is not None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    return parent


async def _get_scoped_subagent(
    db: AsyncSession,
    *,
    subagent_id: str,
    uid: str,
    session_id: str,
    profile_id: int,
    touch: bool = True,
):
    subagent = await subagent_crud.get_scoped(
        db,
        subagent_id=subagent_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        lock=True,
        touch=touch,
    )
    if subagent is None:
        raise ParameterException(ERR_SUBAGENT_NOT_FOUND)
    return subagent


def _serialize_board_post(post: SubagentBoardPost) -> dict[str, Any]:
    return post.model_dump(
        mode="json",
        include={"id", "subagent_id", "child_work_id", "stage", "content", "created_at"},
    )


async def post_subagent_board(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    stage: str,
    content: str,
) -> dict[str, Any]:
    try:
        work = await _load_claimed_work(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            lock_claim=False,
        )
        subagent_id = getattr(work, "subagent_id", None)
        if work.work_category != SessionReplyWorkCategory.SUBAGENT or not isinstance(subagent_id, str) or not subagent_id.strip():
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

        subagent = await _get_scoped_subagent(
            db,
            subagent_id=subagent_id,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            touch=False,
        )
        work = await _load_claimed_work(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
        )
        if work.work_category != SessionReplyWorkCategory.SUBAGENT or getattr(work, "subagent_id", None) != subagent.id:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        round_row = await subagent_crud.get_round_for_work(db, work=work)
        if round_row is None or subagent.active_child_work_id != round_row.child_work_id or round_row.result is not None or round_row.waiting_confirmation is not False:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

        post = await subagent_crud.create_board_post(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            subagent_id=subagent.id,
            child_work_id=round_row.child_work_id,
            stage=stage,
            content=content,
        )
        subagent_id = subagent.id
        child_work_id = round_row.child_work_id
        result = {
            "status": "success",
            "operation": "board_post",
            "subagent_id": subagent_id,
            "work_id": child_work_id,
            "post": _serialize_board_post(post),
        }
        await db.commit()
        try:
            async with AsyncSession(bind=db.bind, expire_on_commit=False) as delivery_db:
                await deliver_child_work_results(delivery_db, child_work_id=child_work_id)
        except Exception:
            logger.exception(
                "Subagent board post delivery failed",
                extra={"subagent_id": subagent_id, "child_work_id": child_work_id},
            )
        return result
    except BaseException:
        await db.rollback()
        raise


async def read_subagent_board(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    subagent_id: str,
) -> dict[str, Any]:
    try:
        await _load_parent(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
        )
        subagent = await _get_scoped_subagent(
            db,
            subagent_id=subagent_id,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            touch=False,
        )
        posts = await subagent_crud.list_board_posts(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            subagent_id=subagent.id,
        )
        await db.commit()

        serialized_posts = []
        for post in posts:
            serialized_posts.append(_serialize_board_post(post))
        return {
            "status": "success",
            "operation": "board_read",
            "subagent_id": subagent.id,
            "posts": serialized_posts,
        }
    except BaseException:
        await db.rollback()
        raise


def build_subagent_queued_result(call: SubagentToolCall) -> dict[str, Any]:
    result = {
        "status": "queued",
        "operation": call.arguments["operation"],
        "subagent_id": call.subagent_id,
        "work_id": call.child_work_id,
        "child_work_id": call.child_work_id,
    }
    result["message"] = t(MSG_SUBAGENT_SUBMITTED)
    return result


def _log_subagent_message(subagent: Subagent, call: SubagentToolCall, message: str) -> None:
    with agent_log_context(
        subagent_name=subagent.name,
        uid=subagent.uid,
        session_id=subagent.session_id,
        profile_id=subagent.profile_id,
        work_id=call.child_work_id,
    ):
        logger.info(t(LOG_DISPATCHER_MAIN_AGENT_MESSAGE, message=message, attachments=str(None)))


async def _existing_submission(
    db: AsyncSession,
    *,
    call: SubagentToolCall,
    uid: str,
    session_id: str,
    profile_id: int,
) -> dict[str, Any]:
    await _get_scoped_subagent(
        db,
        subagent_id=call.subagent_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        touch=True,
    )
    if call.delivered:
        round_row = await subagent_crud.get_round(
            db,
            child_work_id=call.child_work_id,
            subagent_id=call.subagent_id,
        )
        if round_row is None or not isinstance(round_row.result, dict):
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        from app.core.session_reply_queue.child_result import build_child_call_result

        return build_child_call_result(call, round_row.result)
    return build_subagent_queued_result(call)


async def _load_previous_round_context(
    db: AsyncSession,
    *,
    previous_work: SessionReplyWorkItem,
) -> tuple[dict[str, Any], list[Message]]:
    previous_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, previous_work.id)
    checkpoint = deepcopy(previous_checkpoint) if isinstance(previous_checkpoint, dict) else {}
    checkpoint_messages = checkpoint.get("messages")
    pending_messages = await subagent_crud.list_pending_messages(db, work=previous_work)

    if not isinstance(checkpoint_messages, list):
        history_rows: list[Message] = []
        page_after_id: int | None = None
        while True:
            page = await message_crud.get_history_forward_by_id(
                db,
                session_id=previous_work.session_id,
                uid=previous_work.uid,
                work_id=previous_work.id,
                page_after_id=page_after_id,
                limit=200,
            )
            if not page:
                break
            history_rows.extend(row for row in page if row.profile_id == previous_work.profile_id and row.work_id == previous_work.id)
            last_id = page[-1].id
            if len(page) < 200 or last_id is None:
                break
            page_after_id = last_id
        checkpoint = {}
        checkpoint_messages = [item.model_dump(mode="json", exclude_none=True) for item in parse_db_messages_to_internal(history_rows)]
    else:
        checkpoint_messages = [deepcopy(item) for item in checkpoint_messages if isinstance(item, dict)]

    message_ids = {item.get("id") for item in checkpoint_messages if isinstance(item, dict) and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool)}

    final_message = None
    if previous_work.result_message_id is not None:
        candidate = await db.get(Message, previous_work.result_message_id)
        if candidate is not None and candidate.uid == previous_work.uid and candidate.session_id == previous_work.session_id and candidate.profile_id == previous_work.profile_id and candidate.work_id == previous_work.id and candidate.role == MessageRole.ASSISTANT and candidate.type == MessageType.TEXT:
            final_message = candidate
    if final_message is not None and final_message.id not in message_ids:
        checkpoint_messages.extend(item.model_dump(mode="json", exclude_none=True) for item in parse_db_messages_to_internal([final_message]))
        if final_message.id is not None:
            message_ids.add(final_message.id)

    for item in parse_db_messages_to_internal(pending_messages):
        if item.id not in message_ids:
            checkpoint_messages.append(item.model_dump(mode="json", exclude_none=True))
            if item.id is not None:
                message_ids.add(item.id)

    for key in ("reply_skipped", "deferred_external_results", "pending_resume_message_ids", "external_resume_messages"):
        checkpoint.pop(key, None)
    checkpoint["messages"] = checkpoint_messages
    return checkpoint, pending_messages


async def _start_subagent_round(
    db: AsyncSession,
    *,
    parent: SessionReplyWorkItem,
    subagent: Subagent | None,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_call_id: str,
    message: str,
    arguments: dict[str, Any],
    previous_work: SessionReplyWorkItem | None = None,
) -> SubagentToolCall:
    locked_profile = await profile_crud.lock_for_runtime_use(
        db,
        profile_id=profile_id,
        uid=uid,
    )
    if locked_profile is None or locked_profile.id != profile_id or locked_profile.uid != uid:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)

    previous_checkpoint: dict[str, Any] | None = None
    pending_messages: list[Message] = []
    if previous_work is not None:
        previous_checkpoint, pending_messages = await _load_previous_round_context(
            db,
            previous_work=previous_work,
        )

    if subagent is None:
        subagent = await subagent_crud.create(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            name=arguments["name"],
        )

    spec = AgentWorkSpec(
        instruction=message,
        execution_scope=END_SESSION_SCOPE_TASK,
        report_target=AgentWorkReportTarget.PARENT_WORK,
        parent_work_id=parent.id,
        subagent_id=subagent.id,
        parent_tool_call_id=tool_call_id,
    )
    child_work, _created = await session_reply_queue_manager.enqueue_agent_work(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        source_type=SessionReplySourceType.PARENT_WORK,
        source_id=parent.id,
        dedupe_key=hashlib.sha256(f"{parent.id}:{tool_call_id}".encode()).hexdigest(),
        spec=spec,
        commit=False,
    )

    instruction_message = Message(
        session_id=session_id,
        uid=uid,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content_format=MessageContentFormat.TEXT,
        content=message,
        profile_id=profile_id,
        work_id=child_work.id,
        is_processed=False,
    )
    instruction_message = await message_crud.persist(db, message=instruction_message, commit=False)
    spec.instruction_message_id = instruction_message.id

    child_state = dict(child_work.execution_state) if isinstance(child_work.execution_state, dict) else {}
    child_state["agent_work"] = spec.model_dump(mode="json")
    child_state["child_work_id"] = child_work.id
    child_work.execution_state = child_state
    db.add(child_work)

    await subagent_crud.create_round(
        db,
        subagent_id=subagent.id,
        child_work_id=child_work.id,
    )
    call = await subagent_crud.create_call(
        db,
        subagent_id=subagent.id,
        child_work_id=child_work.id,
        parent_work_id=parent.id,
        tool_call_id=tool_call_id,
        arguments=arguments,
    )
    subagent.active_child_work_id = child_work.id
    subagent.latest_child_work_id = child_work.id
    db.add(subagent)

    parent_state = dict(parent.execution_state) if isinstance(parent.execution_state, dict) else {}
    parent_state["has_child_work"] = True
    parent.execution_state = parent_state
    db.add(parent)

    if previous_work is not None and previous_checkpoint is not None:
        instruction_items = parse_db_messages_to_internal([instruction_message])
        if not instruction_items:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        previous_checkpoint["messages"].extend(item.model_dump(mode="json", exclude_none=True) for item in instruction_items)
        previous_checkpoint.update(
            {
                "turn_messages": [],
                "files_to_user": [],
                "current_turn": 0,
                "execution_phase": GOAL_EXECUTION_PHASE_RUNNING,
                "reload_history": False,
            }
        )

        migrated_message_ids = [item.id for item in pending_messages if item.id is not None]
        for item in pending_messages:
            item.work_id = child_work.id
            db.add(item)
        if instruction_message.id is not None:
            migrated_message_ids.append(instruction_message.id)
        if migrated_message_ids:
            await db.flush()
            await subagent_crud.acknowledge_messages(
                db,
                work=child_work,
                message_ids=migrated_message_ids,
            )
        await session_reply_work_item_crud.save_checkpoint(
            db,
            child_work.id,
            previous_checkpoint,
        )

    return call


async def create_subagent(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_call_id: str,
    name: str,
    message: str,
) -> dict[str, Any]:
    parent = await _load_parent(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
    )
    arguments = {"operation": "create", "name": name, "message": message}
    existing_call = await subagent_crud.get_call(
        db,
        parent_work_id=parent.id,
        tool_call_id=tool_call_id,
    )
    if existing_call is not None:
        if existing_call.arguments != arguments:
            raise ParameterException(ERR_SUBAGENT_CALL_CONFLICT)
        result = await _existing_submission(
            db,
            call=existing_call,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
        )
        await db.commit()
        return result

    call = await _start_subagent_round(
        db,
        parent=parent,
        subagent=None,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        tool_call_id=tool_call_id,
        message=message,
        arguments=arguments,
    )

    await db.commit()
    return build_subagent_queued_result(call)


async def send_subagent_message(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_call_id: str,
    subagent_id: str,
    message: str,
) -> dict[str, Any]:
    try:
        parent = await _load_parent(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
        )
        arguments = {
            "operation": "send_message",
            "subagent_id": subagent_id,
            "message": message,
        }
        existing_call = await subagent_crud.get_call(
            db,
            parent_work_id=parent.id,
            tool_call_id=tool_call_id,
        )
        if existing_call is not None:
            if existing_call.arguments != arguments:
                raise ParameterException(ERR_SUBAGENT_CALL_CONFLICT)
            result = await _existing_submission(
                db,
                call=existing_call,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
            )
            await db.commit()
            return result

        subagent = await _get_scoped_subagent(
            db,
            subagent_id=subagent_id,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            touch=True,
        )
        child_work_id = subagent.active_child_work_id or subagent.latest_child_work_id
        if child_work_id is None:
            raise ParameterException(ERR_SUBAGENT_WORK_NOT_FOUND)
        round_row = await subagent_crud.get_round(
            db,
            child_work_id=child_work_id,
            subagent_id=subagent.id,
        )
        if round_row is None:
            raise ParameterException(ERR_SUBAGENT_WORK_NOT_FOUND)

        work = await load_child_work(db, subagent=subagent, round_row=round_row)
        snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
        round_completed = round_row.result is not None or snapshot.get("completed") is True
        if round_row.result is None and snapshot.get("completed") is True:
            await finish_child_work(db, work=work)

        if not round_completed:
            pending_message = Message(
                session_id=session_id,
                uid=uid,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content_format=MessageContentFormat.TEXT,
                content=message,
                profile_id=profile_id,
                work_id=round_row.current_work_id,
                is_processed=False,
            )
            await message_crud.persist(db, message=pending_message, commit=False)
            call = await subagent_crud.create_call(
                db,
                subagent_id=subagent.id,
                child_work_id=round_row.child_work_id,
                parent_work_id=parent.id,
                tool_call_id=tool_call_id,
                arguments=arguments,
            )
            parent_state = dict(parent.execution_state) if isinstance(parent.execution_state, dict) else {}
            parent_state["has_child_work"] = True
            parent.execution_state = parent_state
            db.add(parent)
            await db.commit()
            _log_subagent_message(subagent, call, message)
            return build_subagent_queued_result(call)

        call = await _start_subagent_round(
            db,
            parent=parent,
            subagent=subagent,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            tool_call_id=tool_call_id,
            message=message,
            arguments=arguments,
            previous_work=work,
        )
        await db.commit()
        _log_subagent_message(subagent, call, message)
        return build_subagent_queued_result(call)
    except BaseException:
        await db.rollback()
        raise


async def _list_interactive_tool_call_ids(
    db: AsyncSession,
    *,
    works: list[SessionReplyWorkItem],
) -> list[str]:
    tool_call_ids: list[str] = []
    seen_tool_call_ids: set[str] = set()
    for work in works:
        page_after_id: int | None = None
        while True:
            page = await message_crud.get_history_forward_by_id(
                db,
                session_id=work.session_id,
                uid=work.uid,
                work_id=work.id,
                page_after_id=page_after_id,
                limit=200,
            )
            if not page:
                break

            scoped_messages = []
            for message in page:
                if message.uid != work.uid or message.session_id != work.session_id or message.profile_id != work.profile_id or message.work_id != work.id:
                    continue
                scoped_messages.append(message)

            for message in parse_db_messages_to_internal(scoped_messages):
                for tool_call in message.tool_calls or []:
                    if tool_call.name != SHELL_TOOL_NAME or tool_call.arguments.get("execution_mode") != "interactive":
                        continue
                    if tool_call.id in seen_tool_call_ids:
                        continue
                    seen_tool_call_ids.add(tool_call.id)
                    tool_call_ids.append(tool_call.id)

            last_id = page[-1].id
            if len(page) < 200 or last_id is None:
                break
            page_after_id = last_id

    return tool_call_ids


async def cancel_subagent(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    subagent_id: str,
    work_id: int | None = None,
) -> dict[str, Any]:
    try:
        await _load_parent(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
        )
        subagent = await _get_scoped_subagent(
            db,
            subagent_id=subagent_id,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            touch=True,
        )
        child_work_id = work_id if work_id is not None else subagent.active_child_work_id or subagent.latest_child_work_id
        if child_work_id is None:
            raise ParameterException(ERR_SUBAGENT_WORK_NOT_FOUND)
        round_row = await subagent_crud.get_round(
            db,
            child_work_id=child_work_id,
            subagent_id=subagent.id,
        )
        if round_row is None:
            raise ParameterException(ERR_SUBAGENT_WORK_NOT_FOUND)

        work = await load_child_work(db, subagent=subagent, round_row=round_row)
        snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
        if round_row.result is not None or snapshot.get("completed") is True:
            if round_row.result is None:
                await finish_child_work(db, work=work)
                snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
            await db.commit()
            return {
                "status": "success",
                "operation": "cancel",
                "subagent_id": subagent.id,
                "work": snapshot,
            }

        error = t(ERR_SUBAGENT_CANCELLED)
        confirmation_cancellation = None
        if snapshot.get("status") == "waiting_confirmation":
            confirmation_message = await db.get(Message, work.result_message_id) if work.result_message_id is not None else await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
            if confirmation_message is None or confirmation_message.uid != work.uid or confirmation_message.session_id != work.session_id or confirmation_message.profile_id != work.profile_id or confirmation_message.type != MessageType.AUDIT_CONFIRMATION:
                raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
            audit_record_id = _parse_audit_record_id(confirmation_message)
            if audit_record_id is None:
                raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
            record = await audit_crud.get_record(db, audit_record_id)
            if record is None or record.source_work_id != work.id or record.uid != work.uid or record.session_id != work.session_id:
                raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
            if record.status == AuditRecordStatus.PENDING:
                confirmation_cancellation = await cancel_persisted_pending_confirmation_bundle(
                    db,
                    audit_record_id=audit_record_id,
                    uid=work.uid,
                    session_id=work.session_id,
                    feedback=error,
                    confirmation_status=AuditRecordStatus.CANCELLED.value,
                    commit=False,
                )

        await subagent_crud.cancel_work(db, work=work, error=error)
        await db.refresh(work)

        round_works = await subagent_crud.list_round_work_items(
            db,
            subagent_id=subagent.id,
            child_work_id=round_row.child_work_id,
            uid=work.uid,
            session_id=work.session_id,
            profile_id=work.profile_id,
        )
        work_ids = [round_work.id for round_work in round_works if round_work.id is not None]
        interactive_tool_call_ids = await _list_interactive_tool_call_ids(db, works=round_works)
        terminal_sessions = await terminal_session_crud.list_by_tool_calls(
            db,
            uid=work.uid,
            session_id=work.session_id,
            profile_id=work.profile_id,
            tool_call_ids=interactive_tool_call_ids,
        )

        terminal_audit_ids: list[int] = []
        for terminal_session in terminal_sessions:
            audit_record_id = await cleanup_terminal_session(
                db,
                terminal_session=terminal_session,
                failure_reason=error,
            )
            if audit_record_id is not None:
                terminal_audit_ids.append(audit_record_id)

        background_audit_ids = await background_task_crud.cancel_by_origin_work(
            db,
            uid=work.uid,
            session_id=work.session_id,
            profile_id=work.profile_id,
            work_ids=work_ids,
            commit=False,
        )

        binding = get_bound_audit_execution(work)
        if binding is not None:
            audit_record_id, claim_token = binding
            await _mark_audit_execution_unknown_reliably(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
                error_reason=error,
                commit=False,
            )

        await db.refresh(work)
        await finish_child_work(db, work=work)
        snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
        result = {
            "status": "success",
            "operation": "cancel",
            "subagent_id": subagent.id,
            "work": snapshot,
        }
        delivery_child_work_id = round_row.child_work_id
        await db.commit()

        for audit_record_id in dict.fromkeys([*background_audit_ids, *terminal_audit_ids]):
            await update_confirmation_message_status(db, audit_record_id=audit_record_id)

        if confirmation_cancellation is not None:
            await broadcast_pending_confirmation_cancellation(db, cancellation=confirmation_cancellation)
        await deliver_child_work_results(db, child_work_id=delivery_child_work_id)
        return result
    except BaseException:
        await db.rollback()
        raise


async def list_subagents(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
) -> dict[str, Any]:
    await _load_parent(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
    )
    active_subagents = await subagent_crud.list_active_scoped(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
    )

    entries = []
    for subagent, round_row in active_subagents:
        snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
        if snapshot.get("completed") is True:
            continue
        entries.append(
            {
                "subagent_id": subagent.id,
                "name": subagent.name,
                "work": snapshot,
            }
        )

    await db.commit()
    return {
        "status": "success",
        "operation": "list",
        "subagents": entries,
    }


async def query_subagent(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    subagent_id: str,
    work_id: int | None = None,
) -> dict[str, Any]:
    await _load_parent(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
    )
    subagent = await _get_scoped_subagent(
        db,
        subagent_id=subagent_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        touch=True,
    )
    child_work_id = work_id or subagent.latest_child_work_id
    round_row = await subagent_crud.get_round(
        db,
        child_work_id=child_work_id,
        subagent_id=subagent_id,
    )
    if round_row is None:
        raise ParameterException(ERR_SUBAGENT_WORK_NOT_FOUND)

    from app.core.session_reply_queue.child_result import get_child_snapshot

    snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
    await db.commit()
    return {
        "status": "success",
        "operation": "query",
        "subagent_id": subagent.id,
        "work": snapshot,
    }
