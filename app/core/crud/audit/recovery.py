from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditFailureType,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
)

__all__ = [
    "CRUDAuditRecovery",
]


class CRUDAuditRecovery:
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
        # 先物化 ID，以规避 MySQL 同表删除限制并保持 SQLite、MySQL 一致逻辑。
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
