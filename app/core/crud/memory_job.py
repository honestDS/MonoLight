from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_MEMORY_ACTIVE_MUTATION_KEY_CLEAR_STATUS_INVALID
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryMutationStatus


def _input_data(obj_in: Any) -> dict[str, Any]:
    if obj_in is None:
        return {}
    if isinstance(obj_in, dict):
        return dict(obj_in)
    return obj_in.model_dump(exclude_unset=True)


class CRUDLongTermMemoryMutationJob:
    async def get_by_id(self, db: AsyncSession, *, uid: str, job_id: int) -> LongTermMemoryMutationJob | None:
        result = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id))
        return result.scalars().first()

    async def get_by_dedupe_key(self, db: AsyncSession, *, uid: str, dedupe_key: str) -> LongTermMemoryMutationJob | None:
        result = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.dedupe_key == dedupe_key))
        return result.scalars().first()

    async def get_by_active_mutation_key(self, db: AsyncSession, *, uid: str, active_mutation_key: str) -> LongTermMemoryMutationJob | None:
        result = await db.execute(
            select(LongTermMemoryMutationJob).where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.active_mutation_key == active_mutation_key,
            )
        )
        return result.scalars().first()

    async def list_by_uid(self, db: AsyncSession, *, uid: str, skip: int = 0, limit: int = 100) -> list[LongTermMemoryMutationJob]:
        result = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid).order_by(LongTermMemoryMutationJob.created_at.desc(), LongTermMemoryMutationJob.id.desc()).offset(skip).limit(limit))
        return list(result.scalars().all())

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> tuple[LongTermMemoryMutationJob, bool]:
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        job = LongTermMemoryMutationJob.model_validate({"uid": uid, **data})

        try:
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError:
            existing = await self.get_by_dedupe_key(db, uid=uid, dedupe_key=job.dedupe_key)
            if existing is not None:
                return existing, False
            raise

        if commit:
            await db.commit()
        await db.refresh(job)
        return job, True

    async def create_job(self, db: AsyncSession, **kwargs: Any) -> tuple[LongTermMemoryMutationJob, bool]:
        return await self.create(db, **kwargs)

    async def update_status(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        status: LongTermMemoryMutationStatus,
        commit: bool = True,
        clear_active_mutation_key: bool = False,
        **values: Any,
    ) -> LongTermMemoryMutationJob | None:
        status = LongTermMemoryMutationStatus(status)
        if clear_active_mutation_key and status not in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }:
            raise ValueError(t(ERR_MEMORY_ACTIVE_MUTATION_KEY_CLEAR_STATUS_INVALID, field="status"))
        allowed = {
            "result",
            "error",
            "available_at",
            "attempt_count",
            "locked_by",
            "lock_until",
            "cancel_requested_at",
            "started_at",
            "finished_at",
        }
        update_values = {key: value for key, value in values.items() if key in allowed}
        update_values["status"] = status
        update_values["updated_at"] = get_local_time()
        if clear_active_mutation_key:
            update_values["active_mutation_key"] = None
        result = await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(**update_values).execution_options(synchronize_session=False))
        if (result.rowcount or 0) != 1:
            return None
        if commit:
            await db.commit()
        else:
            await db.flush()
        refreshed = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).execution_options(populate_existing=True))
        return refreshed.scalars().first()


memory_job_crud = CRUDLongTermMemoryMutationJob()
long_term_memory_mutation_job_crud = memory_job_crud
