from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import sync_expired_confirmation_messages, update_confirmation_message_status
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN
from app.core.crud.audit.audit import audit_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.models.audit import AuditRecordStatus


@dataclass(frozen=True, slots=True)
class AuditStartupRecoveryResult:
    expired_pending_records: int
    recovered_preparing_records: int
    unknown_execution_records: int
    unknown_execution_attempts: int


async def recover_audit_state(db: AsyncSession) -> AuditStartupRecoveryResult:
    expired_records = [(record.id, record.language) for record in await audit_crud.list_expired_pending_confirmations(db) if record.id is not None]
    preparing_records = [record.id for record in await audit_crud.list_records_by_status(db, AuditRecordStatus.PREPARING) if record.id is not None]
    interrupted_records = [(record.id, record.uid, record.session_id, record.language) for record in await audit_crud.list_records_by_status(db, AuditRecordStatus.EXECUTING) if record.id is not None]
    expired_pending_records = await audit_crud.expire_pending_confirmations(db)
    recovered_preparing_records = await audit_crud.recover_preparing(db)
    unknown_execution_records, unknown_execution_attempts = await audit_crud.recover_interrupted(db)
    await background_task_crud.fail_tasks_for_terminal_audits(db, error=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
    for audit_record_id, language in expired_records:
        await sync_expired_confirmation_messages(db, audit_record_id=audit_record_id, locale=language)
    for audit_record_id in preparing_records:
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    for audit_record_id, _uid, _session_id, _language in interrupted_records:
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)

    return AuditStartupRecoveryResult(
        expired_pending_records=expired_pending_records,
        recovered_preparing_records=recovered_preparing_records,
        unknown_execution_records=unknown_execution_records,
        unknown_execution_attempts=unknown_execution_attempts,
    )
