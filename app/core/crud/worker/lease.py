from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.worker_lease import WorkerLease
from app.providers.database.time import get_database_timestamp


class CRUDWorkerLease(CRUDBase[WorkerLease, WorkerLease, WorkerLease]):
    async def get_by_name(
        self,
        db: AsyncSession,
        worker_name: str,
    ) -> WorkerLease | None:
        result = await db.execute(select(WorkerLease).where(WorkerLease.worker_name == worker_name))
        return result.scalars().first()

    async def acquire(
        self,
        db: AsyncSession,
        *,
        worker_name: str,
        owner_id: str,
        lease_seconds: int,
    ) -> bool:
        now = await get_database_timestamp(db)
        lease_until = now + lease_seconds
        result = await db.execute(
            update(WorkerLease)
            .where(
                WorkerLease.worker_name == worker_name,
                or_(
                    WorkerLease.owner_id == owner_id,
                    WorkerLease.lease_until < now,
                ),
            )
            .values(
                owner_id=owner_id,
                lease_until=lease_until,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await db.commit()
            return True

        db.add(
            WorkerLease(
                worker_name=worker_name,
                owner_id=owner_id,
                lease_until=lease_until,
                updated_at=now,
            )
        )
        try:
            await db.commit()
            return True
        except IntegrityError:
            await db.rollback()

        result = await db.execute(
            update(WorkerLease)
            .where(
                WorkerLease.worker_name == worker_name,
                WorkerLease.lease_until < now,
            )
            .values(
                owner_id=owner_id,
                lease_until=lease_until,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def renew(
        self,
        db: AsyncSession,
        *,
        worker_name: str,
        owner_id: str,
        lease_seconds: int,
    ) -> bool:
        now = await get_database_timestamp(db)
        result = await db.execute(
            update(WorkerLease)
            .where(
                WorkerLease.worker_name == worker_name,
                WorkerLease.owner_id == owner_id,
                WorkerLease.lease_until >= now,
            )
            .values(
                lease_until=now + lease_seconds,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def release(
        self,
        db: AsyncSession,
        *,
        worker_name: str,
        owner_id: str,
    ) -> bool:
        result = await db.execute(
            delete(WorkerLease)
            .where(
                WorkerLease.worker_name == worker_name,
                WorkerLease.owner_id == owner_id,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


worker_lease_crud = CRUDWorkerLease(WorkerLease)
