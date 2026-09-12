import uuid
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_AUDIT_PENDING_CLOSE_STATUS_INVALID,
)
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditDecision,
    AuditFailureType,
    AuditRecord,
    AuditRecordStatus,
)

from .common import (
    build_passed_execution_claim_update,
    build_pending_execution_claim_update,
)

__all__ = [
    "CRUDAuditClaims",
]


class CRUDAuditClaims:
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
        commit: bool = True,
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
        if commit:
            await db.commit()
        else:
            await db.flush()
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
            if commit:
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
