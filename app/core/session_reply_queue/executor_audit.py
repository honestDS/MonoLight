import asyncio
from typing import Any

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.audit.integrity import create_file_integrity_snapshot, verify_file_integrity_snapshot
from app.core.constants import ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT, SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.crud.terminal.session import terminal_session_crud
from app.core.i18n import t
from app.models.audit import AuditRecordStatus
from app.models.background_task import BackgroundTaskStatus
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

__all__ = [
    "get_bound_audit_execution",
    "work_has_active_audit_execution",
    "mark_work_audit_execution_unknown",
]


async def _persist_work_audit_execution_binding(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    binding: dict[str, Any] | None,
) -> None:
    """持久化后台回复工作当前的审计执行绑定。"""
    state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    if binding is None:
        state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
    else:
        state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = dict(binding)
    updated = await session_reply_work_item_crud.update_claimed(
        db,
        work_id=work.id,
        worker_id=worker_id,
        values={"execution_state": state},
    )
    if not updated:
        raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
    work.execution_state = state


async def _mark_audit_execution_unknown_reliably(
    db,
    *,
    audit_record_id: int,
    claim_token: str,
    error_reason: str,
) -> bool:
    marked = await audit_crud.mark_execution_unknown(
        db,
        audit_record_id=audit_record_id,
        claim_token=claim_token,
        error_reason=error_reason,
    )
    if not marked:
        marked = await audit_crud.finish_execution_round(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            status=AuditRecordStatus.EXECUTION_UNKNOWN,
            error_reason=error_reason,
        )
    if marked:
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    return marked


async def _mark_new_confirmed_execution_unknown_without_masking(
    db,
    *,
    audit_record_id: int,
    claim_token: str,
) -> None:
    try:
        await _mark_audit_execution_unknown_reliably(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            error_reason=t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT),
        )
    except BaseException:
        pass


async def _persist_confirmed_work_audit_execution_binding(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    audit_record_id: int,
    claim_token: str,
) -> None:
    state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    state["audit_claim_token"] = claim_token
    values = {
        "source_id": str(audit_record_id),
        "execution_state": state,
    }
    if worker_id:
        try:
            updated = await session_reply_work_item_crud.update_claimed(
                db,
                work_id=work.id,
                worker_id=worker_id,
                values=values,
            )
        except asyncio.CancelledError:
            await _mark_new_confirmed_execution_unknown_without_masking(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
            )
            raise
        except Exception:
            await _mark_new_confirmed_execution_unknown_without_masking(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
            )
            raise
        if not updated:
            await _mark_new_confirmed_execution_unknown_without_masking(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
            )
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
    work.source_id = str(audit_record_id)
    work.execution_state = state


def get_bound_audit_execution(work: SessionReplyWorkItem) -> tuple[int, str] | None:
    """读取回复工作中可恢复的审计整轮绑定。"""
    state_value = getattr(work, "execution_state", None)
    state = state_value if isinstance(state_value, dict) else {}
    work_type = getattr(work, "work_type", None)
    binding = state.get(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY)
    if isinstance(binding, dict):
        audit_record_id = binding.get("audit_record_id")
        claim_token = binding.get("claim_token")
    elif work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
        audit_record_id = getattr(work, "source_id", None)
        claim_token = state.get("audit_claim_token")
    elif work_type in {
        SessionReplyWorkType.FOREGROUND_REPLY,
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
    }:
        return None
    else:
        return None

    try:
        audit_record_id = int(audit_record_id)
    except (TypeError, ValueError):
        return None
    if audit_record_id <= 0 or not isinstance(claim_token, str) or not claim_token:
        return None
    return audit_record_id, claim_token


def work_has_active_audit_execution(work: SessionReplyWorkItem) -> bool:
    """判断回复工作是否持有需要禁止自动重试的活动审计绑定。"""
    if getattr(work, "work_type", None) not in {
        SessionReplyWorkType.FOREGROUND_REPLY,
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
    }:
        return False
    state_value = getattr(work, "execution_state", None)
    state = state_value if isinstance(state_value, dict) else {}
    return SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in state


async def mark_work_audit_execution_unknown(work_id: int, worker_id: str, error: str) -> None:
    """将中断回复工作绑定的审计整轮标记为结果未知。"""
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None:
            return
        binding = get_bound_audit_execution(work)
        if binding is None:
            return
        audit_record_id, claim_token = binding
        background_tasks = await background_task_crud.list_by_audit_record(db, audit_record_id)
        active_background_execution_ids = {task.audit_execution_record_id for task in background_tasks if task.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING} and task.audit_execution_record_id is not None}
        active_terminal_execution_ids = await terminal_session_crud.list_active_audit_execution_record_ids(db, audit_record_id)
        excluded_execution_record_ids = active_background_execution_ids | active_terminal_execution_ids
        if excluded_execution_record_ids:
            await audit_crud.mark_running_executions_unknown_except(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
                excluded_execution_record_ids=excluded_execution_record_ids,
                error_reason=error,
            )
            return
        await _mark_audit_execution_unknown_reliably(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            error_reason=error,
        )


def _confirmed_file_snapshots_changed(details: list[Any], *, working_directory: str) -> bool:
    """检查已确认工具引用的文件快照是否发生变化。"""
    try:
        for detail in details:
            for file_snapshot in detail.file_snapshots:
                path = file_snapshot.get("absolute_path")
                if not isinstance(path, str):
                    return True
                current = create_file_integrity_snapshot(path, working_directory=working_directory)
                if not verify_file_integrity_snapshot(file_snapshot, current):
                    return True
    except Exception:
        return True
    return False
