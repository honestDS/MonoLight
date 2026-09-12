import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_TERMINAL_SESSION_DELETED,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.terminal.session import (
    terminal_session_crud,
)
from app.core.i18n import t
from app.core.terminal.recovery import cleanup_terminal_process_identity
from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalSessionStatus,
)
from app.core.utils.background_task_result import serialize_execution_summary
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.terminal_session import TerminalSession

from .manager_common import (
    logger,
)

__all__ = [
    "finalize_terminal_session_audit",
    "cleanup_terminal_sessions_by_chat_session",
]


async def _update_terminal_confirmation_status(db: AsyncSession, *, audit_record_id: int) -> None:
    from app.core.audit.confirmation import update_confirmation_message_status

    await update_confirmation_message_status(db, audit_record_id=audit_record_id)


async def finalize_terminal_session_audit(
    db: AsyncSession,
    terminal_session: TerminalSession,
    *,
    status: TerminalSessionStatus,
    exit_code: int | None,
    failure_reason: str | None,
) -> int | None:
    audit_record_id = terminal_session.audit_record_id
    audit_execution_record_id = terminal_session.audit_execution_record_id
    if (audit_record_id is None) != (audit_execution_record_id is None):
        logger.error(
            "Terminal session has a partial audit binding",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": audit_execution_record_id,
                "terminal_status": status.value,
            },
        )
        return None
    if audit_record_id is None or audit_execution_record_id is None:
        return None

    execution_record_id = audit_execution_record_id
    execution = await audit_crud.get_execution_record(db, execution_record_id)
    if execution is None:
        logger.warning(
            "Terminal session audit execution record not found",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "terminal_status": status.value,
            },
        )
        return None
    if execution.audit_record_id != audit_record_id:
        logger.error(
            "Terminal session audit execution binding mismatch",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "execution_audit_record_id": execution.audit_record_id,
            },
        )
        return None
    if execution.status != AuditExecutionStatus.RUNNING:
        logger.warning(
            "Terminal session audit execution is already finalized",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "execution_status": execution.status.value,
            },
        )
        return None

    audit_record = await audit_crud.get_record(db, audit_record_id)
    if audit_record is None:
        logger.warning(
            "Terminal session audit record not found",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
            },
        )
        return None
    if audit_record.status != AuditRecordStatus.EXECUTING or audit_record.execution_claim_token != execution.claim_token:
        logger.warning(
            "Terminal session audit round is already finalized",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "audit_status": audit_record.status.value,
            },
        )
        return None

    if status is TerminalSessionStatus.EXITED:
        execution_status = AuditExecutionStatus.SUCCEEDED if exit_code == 0 else AuditExecutionStatus.FAILED
    elif status is TerminalSessionStatus.FAILED:
        execution_status = AuditExecutionStatus.FAILED
    else:
        execution_status = AuditExecutionStatus.EXECUTION_UNKNOWN
    result_summary = serialize_execution_summary(
        {
            "terminal_session_id": terminal_session.terminal_session_id,
            "status": status.value,
            "exit_code": exit_code,
            "failure_reason": failure_reason,
        },
        max_chars=1000,
    )
    execution_finished = await audit_crud.finish_execution_attempt(
        db,
        execution_record_id=execution_record_id,
        status=execution_status,
        result_summary=result_summary,
        error=None if execution_status is AuditExecutionStatus.SUCCEEDED else result_summary,
        commit=False,
    )
    if not execution_finished:
        logger.warning(
            "Terminal session audit execution was not finalized",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "terminal_status": status.value,
            },
        )
        return None

    round_status = await audit_crud.finish_execution_round_if_complete(
        db,
        audit_record_id=audit_record_id,
        claim_token=execution.claim_token,
        commit=False,
    )
    return audit_record_id if round_status is not None else None


async def cleanup_terminal_sessions_by_chat_session(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
) -> int:
    terminal_sessions = await terminal_session_crud.list_by_chat_session(
        db,
        session_id=session_id,
        uid=uid,
    )
    failure_reason = t(ERR_TERMINAL_SESSION_DELETED)
    for terminal_session in terminal_sessions:
        try:
            cleanup_result = await cleanup_terminal_process_identity(terminal_session.process_identity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Terminal session process cleanup failed",
                extra={
                    "terminal_session_id": terminal_session.terminal_session_id,
                    "session_id": session_id,
                    "uid": uid,
                    "error": str(exc),
                },
            )
        else:
            if cleanup_result.errors:
                logger.error(
                    "Terminal session process cleanup reported errors",
                    extra={
                        "terminal_session_id": terminal_session.terminal_session_id,
                        "session_id": session_id,
                        "uid": uid,
                        "errors": cleanup_result.errors,
                    },
                )

        if terminal_session.status not in TERMINAL_SESSION_FINAL_STATUSES:
            await finalize_terminal_session_audit(
                db,
                terminal_session,
                status=TerminalSessionStatus.LOST,
                exit_code=None,
                failure_reason=failure_reason,
            )

    return await terminal_session_crud.delete_by_chat_session(
        db,
        session_id=session_id,
        uid=uid,
        commit=False,
    )
