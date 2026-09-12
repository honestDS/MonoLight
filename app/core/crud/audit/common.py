from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlmodel import select

from app.core.constants import (
    ERR_AUDIT_FILE_SNAPSHOT_INVALID,
    ERR_AUDIT_FILE_SNAPSHOTS_INVALID,
)
from app.core.i18n import t
from app.models.audit import (
    AuditConfirmationClaim,
    AuditDecision,
    AuditRecord,
    AuditRecordStatus,
)

__all__ = [
    "build_audit_status_update",
    "build_pending_execution_claim_update",
    "build_passed_execution_claim_update",
]

_PREPARATION_STATUSES = {
    AuditRecordStatus.PASSED,
    AuditRecordStatus.BLOCKED,
    AuditRecordStatus.AUDIT_FAILED,
    AuditRecordStatus.PENDING,
}
_FINAL_EXECUTION_STATUSES = {
    AuditRecordStatus.SUCCEEDED,
    AuditRecordStatus.FAILED,
    AuditRecordStatus.EXECUTION_UNKNOWN,
}


def _validate_file_snapshots(file_snapshots: Any) -> list[dict[str, Any]]:
    if not isinstance(file_snapshots, list):
        raise ValueError(t(ERR_AUDIT_FILE_SNAPSHOTS_INVALID))
    validated: list[dict[str, Any]] = []
    for item in file_snapshots:
        if not isinstance(item, dict):
            raise ValueError(t(ERR_AUDIT_FILE_SNAPSHOT_INVALID))
        validated.append(dict(item))
    return validated


def build_audit_status_update(audit_record_id: int, expected_status: AuditRecordStatus, **values: Any):
    return (
        update(AuditRecord)
        .where(
            AuditRecord.id == audit_record_id,
            AuditRecord.status == expected_status,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )


def build_pending_execution_claim_update(
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
    now: datetime,
    claim_token: str,
    decision_message_id: int,
    decision_raw_message: str,
    decided_by: str,
):
    claim_exists = select(AuditConfirmationClaim.id).where(
        AuditConfirmationClaim.audit_record_id == audit_record_id,
        AuditConfirmationClaim.uid == uid,
        AuditConfirmationClaim.session_id == session_id,
    )
    return (
        update(AuditRecord)
        .where(
            AuditRecord.id == audit_record_id,
            AuditRecord.uid == uid,
            AuditRecord.session_id == session_id,
            AuditRecord.status == AuditRecordStatus.PENDING,
            AuditRecord.expires_at.is_not(None),
            AuditRecord.expires_at > now,
            claim_exists.exists(),
        )
        .values(
            status=AuditRecordStatus.EXECUTING,
            decision=AuditDecision.APPROVE,
            decision_message_id=decision_message_id,
            decision_raw_message=decision_raw_message,
            decided_by=decided_by,
            decided_at=now,
            execution_claim_token=claim_token,
            execution_started_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


def build_passed_execution_claim_update(*, audit_record_id: int, now: datetime, claim_token: str):
    return build_audit_status_update(
        audit_record_id,
        AuditRecordStatus.PASSED,
        status=AuditRecordStatus.EXECUTING,
        execution_claim_token=claim_token,
        execution_started_at=now,
        updated_at=now,
    )
