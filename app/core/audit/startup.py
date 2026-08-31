import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import sync_expired_confirmation_messages, update_confirmation_message_status
from app.core.audit.storage import AuditCleanupResult, cleanup_audit_storage
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN
from app.core.crud.audit.audit import audit_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.paths import AUDIT_DIR
from app.models.audit import AuditRecordStatus


@dataclass(frozen=True, slots=True)
class AuditStartupRecoveryResult:
    expired_pending_records: int
    recovered_preparing_records: int
    unknown_execution_records: int
    unknown_execution_attempts: int
    deleted_database_records: int
    file_cleanup: AuditCleanupResult


async def recover_and_cleanup_audit_data(
    db: AsyncSession,
    *,
    retention_days: int,
    audit_root: str | Path = AUDIT_DIR,
) -> AuditStartupRecoveryResult:
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
    context_paths = await audit_crud.list_context_file_paths(db)
    file_cleanup = await asyncio.to_thread(
        cleanup_audit_storage,
        retention_days=retention_days,
        audit_root=audit_root,
        referenced_paths=set(context_paths.values()),
    )

    path_to_record_id = {str(Path(path).resolve(strict=False)): record_id for record_id, path in context_paths.items()}
    deleted_or_missing_paths = set(file_cleanup.deleted_files) | set(file_cleanup.missing_referenced_files)
    record_ids_to_delete = {path_to_record_id[path] for path in deleted_or_missing_paths if path in path_to_record_id}
    record_ids_to_delete.update(await audit_crud.list_records_without_context_before(db, retention_days=retention_days))
    deleted_database_records = await audit_crud.delete_records(db, audit_record_ids=record_ids_to_delete)

    return AuditStartupRecoveryResult(
        expired_pending_records=expired_pending_records,
        recovered_preparing_records=recovered_preparing_records,
        unknown_execution_records=unknown_execution_records,
        unknown_execution_attempts=unknown_execution_attempts,
        deleted_database_records=deleted_database_records,
        file_cleanup=file_cleanup,
    )
