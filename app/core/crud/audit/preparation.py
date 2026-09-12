from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    AUDIT_HIGH_RISK_SCORE,
    ERR_AUDIT_FAILURE_TYPE_REQUIRED,
    ERR_AUDIT_FAILURE_TYPE_UNEXPECTED,
    ERR_AUDIT_FILE_PATH_NOT_ABSOLUTE,
    ERR_AUDIT_PENDING_EXPIRY_REQUIRED,
    ERR_AUDIT_PREPARATION_STATUS_INVALID,
    ERR_AUDIT_ROUND_RESULT_MISMATCH,
    ERR_AUDIT_TOOL_CALL_ID_INVALID,
    ERR_AUDIT_TOOL_DETAIL_COUNT_MISMATCH,
    ERR_AUDIT_TOOL_ORDER_INVALID,
)
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditFailureType,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
    AuditToolDetail,
)

from .common import (
    _PREPARATION_STATUSES,
    _validate_file_snapshots,
    build_audit_status_update,
)

__all__ = [
    "CRUDAuditPreparation",
]


class CRUDAuditPreparation:
    async def get_record(self, db: AsyncSession, audit_record_id: int) -> AuditRecord | None:
        result = await db.execute(select(AuditRecord).where(AuditRecord.id == audit_record_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def list_tool_details(self, db: AsyncSession, audit_record_id: int) -> list[AuditToolDetail]:
        result = await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index, AuditToolDetail.id))
        return list(result.scalars().all())

    async def requires_high_risk_override(self, db: AsyncSession, audit_record_id: int) -> bool:
        result = await db.execute(
            select(AuditToolDetail.id)
            .where(
                AuditToolDetail.audit_record_id == audit_record_id,
                AuditToolDetail.score >= AUDIT_HIGH_RISK_SCORE,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

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
                    file_snapshots=_validate_file_snapshots(detail.get("file_snapshots") or []),
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
