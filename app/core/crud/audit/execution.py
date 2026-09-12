from sqlalchemy import case, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_AUDIT_EXECUTION_END_STATUS_INVALID,
    ERR_AUDIT_EXECUTION_RESULT_MISMATCH,
    ERR_AUDIT_EXECUTIONS_RUNNING,
    ERR_AUDIT_ROUND_EXECUTION_STATUS_INVALID,
)
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
)

from .common import (
    _FINAL_EXECUTION_STATUSES,
)

__all__ = [
    "CRUDAuditExecution",
]


class CRUDAuditExecution:
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
