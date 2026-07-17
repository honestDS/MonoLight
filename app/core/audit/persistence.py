from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.storage import write_audit_json
from app.core.crud.audit import audit_crud
from app.core.paths import AUDIT_DIR
from app.models.audit import AuditFailureType, AuditRecordStatus


async def persist_prepared_audit_round(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    status: AuditRecordStatus,
    context_payload: dict[str, Any],
    tool_details: list[dict[str, Any]],
    intent_summary: str | None = None,
    failure_type: AuditFailureType | None = None,
    error_reason: str | None = None,
    expires_at: datetime | None = None,
    audit_root: str | Path = AUDIT_DIR,
) -> bool:
    try:
        context_file_path = await write_audit_json(
            uid=uid,
            audit_record_id=audit_record_id,
            payload=context_payload,
            audit_root=audit_root,
        )
        completed = await audit_crud.complete_preparation(
            db,
            audit_record_id=audit_record_id,
            status=status,
            tool_details=tool_details,
            context_file_path=str(context_file_path),
            intent_summary=intent_summary,
            failure_type=failure_type,
            error_reason=error_reason,
            expires_at=expires_at,
        )
        if completed:
            return True
        await audit_crud.mark_persistence_failed(
            db,
            audit_record_id=audit_record_id,
            error_reason="审计记录状态在保存过程中发生变化",
        )
        return False
    except Exception as exc:
        await audit_crud.mark_persistence_failed(
            db,
            audit_record_id=audit_record_id,
            error_reason=str(exc),
        )
        return False
