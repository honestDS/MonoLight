import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_AUDIT_EXECUTION_END_STATUS_INVALID,
    ERR_AUDIT_EXECUTION_RESULT_MISMATCH,
    ERR_AUDIT_EXECUTIONS_RUNNING,
    ERR_AUDIT_FAILURE_TYPE_REQUIRED,
    ERR_AUDIT_FAILURE_TYPE_UNEXPECTED,
    ERR_AUDIT_FILE_PATH_NOT_ABSOLUTE,
    ERR_AUDIT_FILE_SNAPSHOT_INVALID,
    ERR_AUDIT_FILE_SNAPSHOTS_INVALID,
    ERR_AUDIT_PENDING_CLOSE_STATUS_INVALID,
    ERR_AUDIT_PENDING_EXPIRY_REQUIRED,
    ERR_AUDIT_PREPARATION_STATUS_INVALID,
    ERR_AUDIT_ROUND_EXECUTION_STATUS_INVALID,
    ERR_AUDIT_ROUND_RESULT_MISMATCH,
    ERR_AUDIT_TOOL_CALL_ID_INVALID,
    ERR_AUDIT_TOOL_DETAIL_COUNT_MISMATCH,
    ERR_AUDIT_TOOL_ORDER_INVALID,
)
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditDecision,
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditFailureType,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
    AuditToolDetail,
)

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
_FILE_SNAPSHOT_DATABASE_FIELDS = {
    "original_path",
    "absolute_path",
    "resolved_path",
    "exists",
    "file_type",
    "size",
    "sha256",
    "truncated",
    "status",
    "error",
}


def _sanitize_file_snapshots(file_snapshots: Any) -> list[dict[str, Any]]:
    if not isinstance(file_snapshots, list):
        raise ValueError(t(ERR_AUDIT_FILE_SNAPSHOTS_INVALID))
    sanitized: list[dict[str, Any]] = []
    for item in file_snapshots:
        if not isinstance(item, dict):
            raise ValueError(t(ERR_AUDIT_FILE_SNAPSHOT_INVALID))
        sanitized.append({key: value for key, value in item.items() if key in _FILE_SNAPSHOT_DATABASE_FIELDS})
    return sanitized


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


class CRUDAudit:
    async def get_record(self, db: AsyncSession, audit_record_id: int) -> AuditRecord | None:
        result = await db.execute(select(AuditRecord).where(AuditRecord.id == audit_record_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def list_tool_details(self, db: AsyncSession, audit_record_id: int) -> list[AuditToolDetail]:
        result = await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index, AuditToolDetail.id))
        return list(result.scalars().all())

    async def list_records_by_status(self, db: AsyncSession, status: AuditRecordStatus) -> list[AuditRecord]:
        result = await db.execute(select(AuditRecord).where(AuditRecord.status == status).order_by(AuditRecord.id).execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def list_expired_pending_confirmations(self, db: AsyncSession) -> list[AuditRecord]:
        result = await db.execute(
            select(AuditRecord)
            .where(
                AuditRecord.status == AuditRecordStatus.PENDING,
                AuditRecord.expires_at.is_not(None),
                AuditRecord.expires_at <= get_local_time(),
            )
            .order_by(AuditRecord.id)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def get_execution_record(self, db: AsyncSession, execution_record_id: int) -> AuditExecutionRecord | None:
        result = await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.id == execution_record_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_execution_binding_for_tool_call(self, db: AsyncSession, *, new_tool_call_id: str) -> tuple[AuditRecord, AuditExecutionRecord] | None:
        result = await db.execute(
            select(AuditRecord, AuditExecutionRecord)
            .join(AuditExecutionRecord, AuditExecutionRecord.audit_record_id == AuditRecord.id)
            .where(
                AuditExecutionRecord.new_tool_call_id == new_tool_call_id,
            )
            .limit(1)
            .execution_options(populate_existing=True)
        )
        binding = result.one_or_none()
        return binding

    async def get_running_execution_binding(self, db: AsyncSession, *, new_tool_call_id: str) -> tuple[AuditRecord, AuditExecutionRecord] | None:
        binding = await self.get_execution_binding_for_tool_call(db, new_tool_call_id=new_tool_call_id)
        if binding is None:
            return None
        record, execution = binding
        if execution.status != AuditExecutionStatus.RUNNING or record.status != AuditRecordStatus.EXECUTING or record.execution_claim_token != execution.claim_token:
            return None
        return binding

    async def create_preparing(
        self,
        db: AsyncSession,
        *,
        uid: str,
        operator_username: str,
        session_id: str,
        source: str,
        language: str,
        source_assistant_message_id: int,
        working_directory: str,
        round_arguments_hash: str,
        tool_count: int,
    ) -> AuditRecord:
        record = AuditRecord(
            uid=uid,
            operator_username=operator_username,
            session_id=session_id,
            source=source,
            language=language,
            source_assistant_message_id=source_assistant_message_id,
            working_directory=str(Path(working_directory).resolve(strict=False)),
            round_arguments_hash=round_arguments_hash,
            tool_count=tool_count,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record

    async def associate_context_path(self, db: AsyncSession, *, audit_record_id: int, context_file_path: str, commit: bool = False) -> bool:
        path = Path(context_file_path)
        if not path.is_absolute():
            raise ValueError(t(ERR_AUDIT_FILE_PATH_NOT_ABSOLUTE))
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.PREPARING,
            )
            .values(context_file_path=str(path.resolve(strict=False)), updated_at=get_local_time())
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def complete_preparation(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        status: AuditRecordStatus,
        tool_details: list[dict[str, Any]],
        context_file_path: str,
        intent_summary: str | None = None,
        failure_type: AuditFailureType | None = None,
        error_reason: str | None = None,
        expires_at: datetime | None = None,
        create_confirmation_claim: bool = True,
    ) -> bool:
        if status not in _PREPARATION_STATUSES:
            raise ValueError(t(ERR_AUDIT_PREPARATION_STATUS_INVALID))
        if status == AuditRecordStatus.AUDIT_FAILED and failure_type is None:
            raise ValueError(t(ERR_AUDIT_FAILURE_TYPE_REQUIRED))
        if status != AuditRecordStatus.AUDIT_FAILED and failure_type is not None:
            raise ValueError(t(ERR_AUDIT_FAILURE_TYPE_UNEXPECTED))
        context_path = Path(context_file_path)
        if not context_path.is_absolute():
            raise ValueError(t(ERR_AUDIT_FILE_PATH_NOT_ABSOLUTE))

        record = await self.get_record(db, audit_record_id)
        if record is None or record.status != AuditRecordStatus.PREPARING:
            return False
        if len(tool_details) != record.tool_count:
            raise ValueError(t(ERR_AUDIT_TOOL_DETAIL_COUNT_MISMATCH))

        seen_call_ids: set[str] = set()
        seen_turn_indexes: set[int] = set()
        detail_models: list[AuditToolDetail] = []
        for detail in tool_details:
            call_id = detail.get("original_tool_call_id")
            turn_index = detail.get("turn_index")
            if not isinstance(call_id, str) or not call_id or call_id in seen_call_ids:
                raise ValueError(t(ERR_AUDIT_TOOL_CALL_ID_INVALID))
            if not isinstance(turn_index, int) or turn_index < 0 or turn_index in seen_turn_indexes:
                raise ValueError(t(ERR_AUDIT_TOOL_ORDER_INVALID))
            seen_call_ids.add(call_id)
            seen_turn_indexes.add(turn_index)
            detail_models.append(
                AuditToolDetail(
                    audit_record_id=audit_record_id,
                    original_tool_call_id=call_id,
                    turn_index=turn_index,
                    tool_name=str(detail["tool_name"]),
                    conclusion=AuditToolConclusion(detail["conclusion"]),
                    score=detail.get("score"),
                    reason=str(detail.get("reason") or ""),
                    arguments_hash=str(detail["arguments_hash"]),
                    arguments_summary=str(detail.get("arguments_summary") or "")[:1000],
                    file_snapshots=_sanitize_file_snapshots(detail.get("file_snapshots") or []),
                )
            )
        if seen_turn_indexes != set(range(record.tool_count)):
            raise ValueError(t(ERR_AUDIT_TOOL_ORDER_INVALID))
        conclusions = {detail.conclusion for detail in detail_models}
        if AuditToolConclusion.BLOCKED in conclusions:
            aggregated_status = AuditRecordStatus.BLOCKED
        elif AuditToolConclusion.AUDIT_FAILED in conclusions:
            aggregated_status = AuditRecordStatus.AUDIT_FAILED
        elif AuditToolConclusion.PENDING in conclusions:
            aggregated_status = AuditRecordStatus.PENDING
        else:
            aggregated_status = AuditRecordStatus.PASSED
        if aggregated_status != status:
            raise ValueError(t(ERR_AUDIT_ROUND_RESULT_MISMATCH))

        now = get_local_time()
        values: dict[str, Any] = {
            "status": status,
            "failure_type": failure_type,
            "error_reason": error_reason,
            "intent_summary": intent_summary,
            "context_file_path": str(context_path.resolve(strict=False)),
            "audited_at": now,
            "updated_at": now,
        }
        if status == AuditRecordStatus.PENDING:
            values.update(pending_at=now, expires_at=expires_at)
        elif status in {AuditRecordStatus.BLOCKED, AuditRecordStatus.AUDIT_FAILED}:
            values["completed_at"] = now

        if status == AuditRecordStatus.PENDING and expires_at is None:
            raise ValueError(t(ERR_AUDIT_PENDING_EXPIRY_REQUIRED))
        await db.execute(build_audit_status_update(audit_record_id, AuditRecordStatus.PREPARING, **values))
        claimed_record_result = await db.execute(select(AuditRecord.status, AuditRecord.context_file_path).where(AuditRecord.id == audit_record_id).execution_options(populate_existing=True))
        claimed_record = claimed_record_result.one_or_none()
        if claimed_record is None or claimed_record.status != status or claimed_record.context_file_path != values["context_file_path"]:
            await db.rollback()
            return False
        db.add_all(detail_models)
        if status == AuditRecordStatus.PENDING and create_confirmation_claim:
            db.add(
                AuditConfirmationClaim(
                    uid=record.uid,
                    session_id=record.session_id,
                    audit_record_id=audit_record_id,
                )
            )
        await db.flush()
        await db.commit()
        return True

    async def activate_confirmation_claim(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        uid: str,
        session_id: str,
        commit: bool = True,
    ) -> bool:
        record_result = await db.execute(
            select(AuditRecord.id).where(
                AuditRecord.id == audit_record_id,
                AuditRecord.uid == uid,
                AuditRecord.session_id == session_id,
                AuditRecord.status == AuditRecordStatus.PENDING,
            )
        )
        if record_result.scalar_one_or_none() is None:
            return False

        claim_result = await db.execute(
            select(AuditConfirmationClaim.id).where(
                AuditConfirmationClaim.audit_record_id == audit_record_id,
                AuditConfirmationClaim.uid == uid,
                AuditConfirmationClaim.session_id == session_id,
            )
        )
        if claim_result.scalar_one_or_none() is not None:
            return True

        db.add(
            AuditConfirmationClaim(
                uid=uid,
                session_id=session_id,
                audit_record_id=audit_record_id,
            )
        )
        await db.flush()
        if commit:
            await db.commit()
        return True

    async def mark_persistence_failed(self, db: AsyncSession, *, audit_record_id: int, error_reason: str) -> bool:
        await db.rollback()
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.PREPARING,
            )
            .values(
                status=AuditRecordStatus.AUDIT_FAILED,
                failure_type=AuditFailureType.AUDIT_PERSISTENCE_FAILED,
                error_reason=error_reason,
                updated_at=now,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def mark_pending_persistence_failed(self, db: AsyncSession, *, audit_record_id: int, error_reason: str) -> bool:
        await db.rollback()
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.PENDING,
            )
            .values(
                status=AuditRecordStatus.AUDIT_FAILED,
                failure_type=AuditFailureType.AUDIT_PERSISTENCE_FAILED,
                error_reason=error_reason,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def get_current_confirmation(self, db: AsyncSession, *, uid: str, session_id: str) -> AuditRecord | None:
        result = await db.execute(
            select(AuditRecord)
            .join(AuditConfirmationClaim, AuditConfirmationClaim.audit_record_id == AuditRecord.id)
            .where(
                AuditConfirmationClaim.uid == uid,
                AuditConfirmationClaim.session_id == session_id,
                AuditRecord.status == AuditRecordStatus.PENDING,
                AuditRecord.expires_at.is_not(None),
                AuditRecord.expires_at > get_local_time(),
            )
        )
        return result.scalars().first()

    async def get_confirmation_claim(self, db: AsyncSession, *, uid: str, session_id: str) -> AuditRecord | None:
        result = await db.execute(
            select(AuditRecord)
            .join(AuditConfirmationClaim, AuditConfirmationClaim.audit_record_id == AuditRecord.id)
            .where(
                AuditConfirmationClaim.uid == uid,
                AuditConfirmationClaim.session_id == session_id,
            )
            .order_by(AuditRecord.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def claim_pending_for_execution(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        uid: str,
        session_id: str,
        decision_message_id: int,
        decision_raw_message: str,
        decided_by: str,
    ) -> tuple[AuditRecord | None, str | None]:
        claim_token = uuid.uuid4().hex
        now = get_local_time()
        await db.execute(
            build_pending_execution_claim_update(
                audit_record_id=audit_record_id,
                uid=uid,
                session_id=session_id,
                now=now,
                claim_token=claim_token,
                decision_message_id=decision_message_id,
                decision_raw_message=decision_raw_message,
                decided_by=decided_by,
            )
        )
        claimed_token_result = await db.execute(
            select(AuditRecord.execution_claim_token)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.EXECUTING,
            )
            .execution_options(populate_existing=True)
        )
        if claimed_token_result.scalar_one_or_none() != claim_token:
            await db.rollback()
            return None, None
        await db.execute(delete(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == audit_record_id))
        await db.commit()
        return await self.get_record(db, audit_record_id), claim_token

    async def claim_passed_for_execution(self, db: AsyncSession, *, audit_record_id: int) -> tuple[AuditRecord | None, str | None]:
        claim_token = uuid.uuid4().hex
        now = get_local_time()
        await db.execute(build_passed_execution_claim_update(audit_record_id=audit_record_id, now=now, claim_token=claim_token))
        claimed_token_result = await db.execute(
            select(AuditRecord.execution_claim_token)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.EXECUTING,
            )
            .execution_options(populate_existing=True)
        )
        if claimed_token_result.scalar_one_or_none() != claim_token:
            await db.rollback()
            return None, None
        await db.commit()
        return await self.get_record(db, audit_record_id), claim_token

    async def close_pending(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        uid: str,
        session_id: str,
        status: AuditRecordStatus,
        decision_message_id: int | None = None,
        decision_raw_message: str | None = None,
        decided_by: str | None = None,
        error_reason: str | None = None,
        commit: bool = True,
    ) -> bool:
        if status not in {AuditRecordStatus.REJECTED, AuditRecordStatus.CANCELLED, AuditRecordStatus.EXPIRED}:
            raise ValueError(t(ERR_AUDIT_PENDING_CLOSE_STATUS_INVALID))
        now = get_local_time()
        values: dict[str, Any] = {
            "status": status,
            "error_reason": error_reason,
            "updated_at": now,
            "completed_at": now,
        }
        if status == AuditRecordStatus.REJECTED:
            values.update(
                decision=AuditDecision.REJECT,
                decision_message_id=decision_message_id,
                decision_raw_message=decision_raw_message,
                decided_by=decided_by,
                decided_at=now,
            )
        conditions = [
            AuditRecord.id == audit_record_id,
            AuditRecord.uid == uid,
            AuditRecord.session_id == session_id,
            AuditRecord.status == AuditRecordStatus.PENDING,
        ]
        if status == AuditRecordStatus.EXPIRED:
            conditions.extend([AuditRecord.expires_at.is_not(None), AuditRecord.expires_at <= now])
        result = await db.execute(update(AuditRecord).where(*conditions).values(**values).execution_options(synchronize_session=False))
        if (result.rowcount or 0) != 1:
            await db.rollback()
            return False
        await db.execute(delete(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == audit_record_id))
        if commit:
            await db.commit()
        else:
            await db.flush()
        return True

    async def cancel_confirmation_by_session(self, db: AsyncSession, *, uid: str, session_id: str, error_reason: str, commit: bool = True) -> int:
        claim_ids = select(AuditConfirmationClaim.audit_record_id).where(
            AuditConfirmationClaim.uid == uid,
            AuditConfirmationClaim.session_id == session_id,
        )
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id.in_(claim_ids),
                AuditRecord.status == AuditRecordStatus.PENDING,
            )
            .values(
                status=AuditRecordStatus.CANCELLED,
                error_reason=error_reason,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            delete(AuditConfirmationClaim).where(
                AuditConfirmationClaim.uid == uid,
                AuditConfirmationClaim.session_id == session_id,
            )
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return result.rowcount or 0

    async def mark_source_message_invalid(self, db: AsyncSession, *, audit_record_id: int, claim_token: str, error_reason: str) -> bool:
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.EXECUTING,
                AuditRecord.execution_claim_token == claim_token,
            )
            .values(
                status=AuditRecordStatus.FAILED,
                failure_type=AuditFailureType.SOURCE_MESSAGE_INVALID,
                error_reason=error_reason,
                execution_claim_token=None,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def cancel_execution_for_file_reaudit(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        claim_token: str,
        error_reason: str,
    ) -> bool:
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.EXECUTING,
                AuditRecord.execution_claim_token == claim_token,
            )
            .values(
                status=AuditRecordStatus.CANCELLED,
                error_reason=error_reason,
                execution_claim_token=None,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def create_execution_attempt(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        audit_tool_detail_id: int,
        claim_token: str,
        execution_node: str,
        new_tool_call_id: str,
    ) -> AuditExecutionRecord | None:
        record_exists = select(AuditRecord.id).where(
            AuditRecord.id == audit_record_id,
            AuditRecord.status == AuditRecordStatus.EXECUTING,
            AuditRecord.execution_claim_token == claim_token,
        )
        detail_exists = select(AuditToolDetail.id).where(
            AuditToolDetail.id == audit_tool_detail_id,
            AuditToolDetail.audit_record_id == audit_record_id,
        )
        valid_result = await db.execute(select(record_exists.exists(), detail_exists.exists()))
        valid_record, valid_detail = valid_result.one()
        if not valid_record or not valid_detail:
            return None
        attempt_result = await db.execute(select(func.count()).select_from(AuditExecutionRecord).where(AuditExecutionRecord.audit_tool_detail_id == audit_tool_detail_id))
        attempt_no = int(attempt_result.scalar_one()) + 1
        execution = AuditExecutionRecord(
            audit_record_id=audit_record_id,
            audit_tool_detail_id=audit_tool_detail_id,
            attempt_no=attempt_no,
            claim_token=claim_token,
            execution_node=execution_node,
            new_tool_call_id=new_tool_call_id,
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)
        return execution

    async def finish_execution_attempt(
        self,
        db: AsyncSession,
        *,
        execution_record_id: int,
        status: AuditExecutionStatus,
        result_summary: str | None = None,
        error: str | None = None,
        commit: bool = True,
    ) -> bool:
        if status == AuditExecutionStatus.RUNNING:
            raise ValueError(t(ERR_AUDIT_EXECUTION_END_STATUS_INVALID))
        record_exists = select(AuditRecord.id).where(
            AuditRecord.id == AuditExecutionRecord.audit_record_id,
            AuditRecord.status == AuditRecordStatus.EXECUTING,
            AuditRecord.execution_claim_token == AuditExecutionRecord.claim_token,
        )
        result = await db.execute(
            update(AuditExecutionRecord)
            .where(
                AuditExecutionRecord.id == execution_record_id,
                AuditExecutionRecord.status == AuditExecutionStatus.RUNNING,
                record_exists.exists(),
            )
            .values(
                status=status,
                result_summary=result_summary,
                error=error,
                finished_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        return (result.rowcount or 0) == 1

    async def cancel_execution_attempt(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        execution_record_id: int,
        claim_token: str,
        error_reason: str,
        commit: bool = True,
    ) -> bool:
        record_exists = select(AuditRecord.id).where(
            AuditRecord.id == audit_record_id,
            AuditRecord.status == AuditRecordStatus.EXECUTING,
            AuditRecord.execution_claim_token == claim_token,
        )
        result = await db.execute(
            update(AuditExecutionRecord)
            .where(
                AuditExecutionRecord.id == execution_record_id,
                AuditExecutionRecord.audit_record_id == audit_record_id,
                AuditExecutionRecord.claim_token == claim_token,
                AuditExecutionRecord.status == AuditExecutionStatus.RUNNING,
                record_exists.exists(),
            )
            .values(
                status=AuditExecutionStatus.CANCELLED,
                error=error_reason,
                finished_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        return (result.rowcount or 0) == 1

    async def mark_execution_started(self, db: AsyncSession, *, execution_record_id: int, claim_token: str) -> bool:
        result = await db.execute(
            update(AuditExecutionRecord)
            .where(
                AuditExecutionRecord.id == execution_record_id,
                AuditExecutionRecord.claim_token == claim_token,
                AuditExecutionRecord.status == AuditExecutionStatus.RUNNING,
            )
            .values(started_at=get_local_time())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def finish_execution_round_if_complete(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        claim_token: str,
        commit: bool = True,
    ) -> AuditRecordStatus | None:
        execution_scope = [
            AuditExecutionRecord.audit_record_id == audit_record_id,
            AuditExecutionRecord.claim_token == claim_token,
        ]
        running_exists = (
            select(AuditExecutionRecord.id)
            .where(
                *execution_scope,
                AuditExecutionRecord.status == AuditExecutionStatus.RUNNING,
            )
            .exists()
        )
        unknown_exists = (
            select(AuditExecutionRecord.id)
            .where(
                *execution_scope,
                AuditExecutionRecord.status == AuditExecutionStatus.EXECUTION_UNKNOWN,
            )
            .exists()
        )
        cancelled_exists = (
            select(AuditExecutionRecord.id)
            .where(
                *execution_scope,
                AuditExecutionRecord.status == AuditExecutionStatus.CANCELLED,
            )
            .exists()
        )
        failed_exists = (
            select(AuditExecutionRecord.id)
            .where(
                *execution_scope,
                AuditExecutionRecord.status == AuditExecutionStatus.FAILED,
            )
            .exists()
        )
        execution_count = select(func.count(AuditExecutionRecord.id)).where(*execution_scope).scalar_subquery()
        unknown_error = select(AuditExecutionRecord.error).where(*execution_scope, AuditExecutionRecord.status == AuditExecutionStatus.EXECUTION_UNKNOWN).order_by(AuditExecutionRecord.id).limit(1).scalar_subquery()
        failed_error = select(AuditExecutionRecord.error).where(*execution_scope, AuditExecutionRecord.status == AuditExecutionStatus.FAILED).order_by(AuditExecutionRecord.id).limit(1).scalar_subquery()
        cancelled_error = select(AuditExecutionRecord.error).where(*execution_scope, AuditExecutionRecord.status == AuditExecutionStatus.CANCELLED).order_by(AuditExecutionRecord.id).limit(1).scalar_subquery()
        status_expression = case(
            (unknown_exists, AuditRecordStatus.EXECUTION_UNKNOWN.name),
            (cancelled_exists, AuditRecordStatus.CANCELLED.name),
            (failed_exists, AuditRecordStatus.FAILED.name),
            else_=AuditRecordStatus.SUCCEEDED.name,
        )
        error_expression = case(
            (unknown_exists, unknown_error),
            (cancelled_exists, cancelled_error),
            (failed_exists, failed_error),
            else_=None,
        )
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.EXECUTING,
                AuditRecord.execution_claim_token == claim_token,
                ~running_exists,
                execution_count == AuditRecord.tool_count,
            )
            .values(
                status=status_expression,
                error_reason=error_expression,
                execution_claim_token=None,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        status_result = await db.execute(select(AuditRecord).where(AuditRecord.id == audit_record_id).execution_options(populate_existing=True))
        updated_record = status_result.scalars().first()
        status = updated_record.status if updated_record is not None else None
        if commit:
            await db.commit()
        return status

    async def finish_execution_round(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        claim_token: str,
        status: AuditRecordStatus,
        error_reason: str | None = None,
    ) -> bool:
        if status not in _FINAL_EXECUTION_STATUSES:
            raise ValueError(t(ERR_AUDIT_ROUND_EXECUTION_STATUS_INVALID))
        execution_result = await db.execute(
            select(AuditExecutionRecord.status).where(
                AuditExecutionRecord.audit_record_id == audit_record_id,
                AuditExecutionRecord.claim_token == claim_token,
            )
        )
        execution_statuses = list(execution_result.scalars().all())
        if AuditExecutionStatus.RUNNING in execution_statuses:
            raise ValueError(t(ERR_AUDIT_EXECUTIONS_RUNNING))
        if status == AuditRecordStatus.SUCCEEDED:
            tool_count_result = await db.execute(select(AuditRecord.tool_count).where(AuditRecord.id == audit_record_id))
            tool_count = tool_count_result.scalar_one_or_none()
            if tool_count is None or len(execution_statuses) != tool_count or any(item != AuditExecutionStatus.SUCCEEDED for item in execution_statuses):
                raise ValueError(t(ERR_AUDIT_EXECUTION_RESULT_MISMATCH))
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.EXECUTING,
                AuditRecord.execution_claim_token == claim_token,
            )
            .values(
                status=status,
                error_reason=error_reason,
                execution_claim_token=None,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def mark_execution_unknown(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        claim_token: str,
        error_reason: str,
        execution_record_id: int | None = None,
        commit: bool = True,
    ) -> bool:
        now = get_local_time()
        attempt_conditions = [
            AuditExecutionRecord.audit_record_id == audit_record_id,
            AuditExecutionRecord.claim_token == claim_token,
            AuditExecutionRecord.status == AuditExecutionStatus.RUNNING,
        ]
        if execution_record_id is not None:
            attempt_conditions.append(AuditExecutionRecord.id == execution_record_id)
        attempt_result = await db.execute(
            update(AuditExecutionRecord)
            .where(*attempt_conditions)
            .values(
                status=AuditExecutionStatus.EXECUTION_UNKNOWN,
                error=error_reason,
                finished_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (attempt_result.rowcount or 0) != 1 and execution_record_id is not None:
            if commit:
                await db.rollback()
            return False
        if execution_record_id is not None:
            await db.execute(
                update(AuditExecutionRecord)
                .where(
                    AuditExecutionRecord.audit_record_id == audit_record_id,
                    AuditExecutionRecord.claim_token == claim_token,
                    AuditExecutionRecord.status == AuditExecutionStatus.RUNNING,
                    AuditExecutionRecord.id != execution_record_id,
                )
                .values(
                    status=AuditExecutionStatus.EXECUTION_UNKNOWN,
                    error=error_reason,
                    finished_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        elif (attempt_result.rowcount or 0) == 0:
            if commit:
                await db.rollback()
            return False
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.status == AuditRecordStatus.EXECUTING,
                AuditRecord.execution_claim_token == claim_token,
            )
            .values(
                status=AuditRecordStatus.EXECUTION_UNKNOWN,
                error_reason=error_reason,
                execution_claim_token=None,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        return (result.rowcount or 0) == 1

    async def mark_running_executions_unknown_except(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        claim_token: str,
        excluded_execution_record_ids: set[int],
        error_reason: str,
    ) -> int:
        conditions = [
            AuditExecutionRecord.audit_record_id == audit_record_id,
            AuditExecutionRecord.claim_token == claim_token,
            AuditExecutionRecord.status == AuditExecutionStatus.RUNNING,
        ]
        if excluded_execution_record_ids:
            conditions.append(AuditExecutionRecord.id.not_in(excluded_execution_record_ids))
        result = await db.execute(
            update(AuditExecutionRecord)
            .where(*conditions)
            .values(
                status=AuditExecutionStatus.EXECUTION_UNKNOWN,
                error=error_reason,
                finished_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount or 0

    async def expire_pending_confirmations(self, db: AsyncSession) -> int:
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.status == AuditRecordStatus.PENDING,
                AuditRecord.expires_at.is_not(None),
                AuditRecord.expires_at <= now,
            )
            .values(
                status=AuditRecordStatus.EXPIRED,
                error_reason="待确认审计已过期",
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        expired_record_ids = select(AuditRecord.id).where(AuditRecord.status == AuditRecordStatus.EXPIRED)
        await db.execute(delete(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id.in_(expired_record_ids)))
        await db.commit()
        return result.rowcount or 0

    async def expire_confirmation_by_session(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
    ) -> int:
        now = get_local_time()
        # MySQL 禁止删除目标表时在子查询读取同表；先物化 ID，让 SQLite、MySQL、PostgreSQL 共用一致逻辑。
        claim_record_ids_result = await db.execute(
            select(AuditConfirmationClaim.audit_record_id).where(
                AuditConfirmationClaim.uid == uid,
                AuditConfirmationClaim.session_id == session_id,
            )
        )
        claim_record_ids = list(claim_record_ids_result.scalars().all())
        if not claim_record_ids:
            return 0
        result = await db.execute(
            update(AuditRecord)
            .where(
                AuditRecord.id.in_(claim_record_ids),
                AuditRecord.status == AuditRecordStatus.PENDING,
                AuditRecord.expires_at.is_not(None),
                AuditRecord.expires_at <= now,
            )
            .values(
                status=AuditRecordStatus.EXPIRED,
                error_reason="待确认审计已过期",
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        expired_record_ids_result = await db.execute(
            select(AuditRecord.id).where(
                AuditRecord.id.in_(claim_record_ids),
                AuditRecord.status == AuditRecordStatus.EXPIRED,
            )
        )
        expired_record_ids = list(expired_record_ids_result.scalars().all())
        if expired_record_ids:
            await db.execute(delete(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id.in_(expired_record_ids)))
        await db.commit()
        return result.rowcount or 0

    async def recover_interrupted(self, db: AsyncSession) -> tuple[int, int]:
        now = get_local_time()
        execution_result = await db.execute(
            update(AuditExecutionRecord)
            .where(AuditExecutionRecord.status == AuditExecutionStatus.RUNNING)
            .values(
                status=AuditExecutionStatus.EXECUTION_UNKNOWN,
                error="服务在工具执行期间中断，结果未知",
                finished_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        record_result = await db.execute(
            update(AuditRecord)
            .where(AuditRecord.status == AuditRecordStatus.EXECUTING)
            .values(
                status=AuditRecordStatus.EXECUTION_UNKNOWN,
                error_reason="服务在工具执行期间中断，禁止自动重试",
                execution_claim_token=None,
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return record_result.rowcount or 0, execution_result.rowcount or 0

    async def recover_preparing(self, db: AsyncSession) -> int:
        now = get_local_time()
        result = await db.execute(
            update(AuditRecord)
            .where(AuditRecord.status == AuditRecordStatus.PREPARING)
            .values(
                status=AuditRecordStatus.AUDIT_FAILED,
                failure_type=AuditFailureType.AUDIT_PERSISTENCE_FAILED,
                error_reason="服务启动时发现未完成的审计保存记录",
                completed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount or 0

    async def list_context_file_paths(self, db: AsyncSession) -> dict[int, str]:
        result = await db.execute(select(AuditRecord.id, AuditRecord.context_file_path).where(AuditRecord.context_file_path.is_not(None)))
        return {record_id: context_path for record_id, context_path in result.all() if context_path}

    async def list_records_without_context_before(self, db: AsyncSession, *, retention_days: int) -> list[int]:
        cutoff = get_local_time() - timedelta(days=retention_days)
        result = await db.execute(
            select(AuditRecord.id).where(
                AuditRecord.context_file_path.is_(None),
                AuditRecord.completed_at.is_not(None),
                AuditRecord.completed_at < cutoff,
            )
        )
        return list(result.scalars().all())

    async def delete_records(self, db: AsyncSession, *, audit_record_ids: set[int]) -> int:
        if not audit_record_ids:
            return 0
        await db.execute(delete(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id.in_(audit_record_ids)))
        await db.execute(delete(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id.in_(audit_record_ids)))
        await db.execute(delete(AuditToolDetail).where(AuditToolDetail.audit_record_id.in_(audit_record_ids)))
        result = await db.execute(delete(AuditRecord).where(AuditRecord.id.in_(audit_record_ids)))
        await db.commit()
        return result.rowcount or 0


audit_crud = CRUDAudit()
