from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.user import UserFileCleanup


class CRUDUserFileCleanup:
    async def get_by_uid(self, db: AsyncSession, *, uid: str, lock_for_update: bool = False) -> UserFileCleanup | None:
        if lock_for_update:
            await db.execute(update(UserFileCleanup).where(UserFileCleanup.uid == uid).values(legacy_session_ids=UserFileCleanup.legacy_session_ids).execution_options(synchronize_session=False))
        query = select(UserFileCleanup).where(UserFileCleanup.uid == uid).execution_options(populate_existing=True)
        if lock_for_update:
            query = query.with_for_update()
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def list_pending(self, db: AsyncSession, *, limit: int = 100) -> list[UserFileCleanup]:
        result = await db.execute(select(UserFileCleanup).order_by(UserFileCleanup.uid).limit(limit))
        return list(result.scalars().all())

    async def enqueue(
        self,
        db: AsyncSession,
        *,
        uid: str,
        legacy_session_ids: list[str],
    ) -> None:
        cleanup = await self.get_by_uid(db, uid=uid)
        if cleanup is None:
            cleanup = UserFileCleanup(uid=uid, legacy_session_ids=sorted(set(legacy_session_ids)))
            db.add(cleanup)
        else:
            cleanup.legacy_session_ids = sorted(set(cleanup.legacy_session_ids).union(legacy_session_ids))
        await db.flush()

    async def remove(self, db: AsyncSession, *, uid: str) -> None:
        await db.execute(delete(UserFileCleanup).where(UserFileCleanup.uid == uid))
        await db.flush()


user_file_cleanup_crud = CRUDUserFileCleanup()
