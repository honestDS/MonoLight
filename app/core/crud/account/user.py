from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.user import (
    User,
    UserCreate,
    UserUpdate,
)


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    async def get_by_username(self, db: AsyncSession, username: str) -> User | None:
        result = await db.execute(select(User).where(User.username == username))
        return result.scalars().first()

    async def get_by_uid(
        self,
        db: AsyncSession,
        uid: str,
        *,
        lock_for_reference_write: bool = False,
    ) -> User | None:
        query = select(User).where(User.uid == uid)
        if lock_for_reference_write:
            await db.execute(update(User).where(User.uid == uid).values(uid=User.uid, updated_at=User.updated_at).execution_options(synchronize_session=False))
            query = query.with_for_update().execution_options(populate_existing=True)
        result = await db.execute(query)
        return result.scalars().first()

    async def get_superuser(self, db: AsyncSession) -> User | None:
        result = await db.execute(select(User).where(User.is_superuser.is_(True)).order_by(User.id.asc()))
        return result.scalars().first()

    async def get_multi_by_uids(
        self,
        db: AsyncSession,
        uids: list[str],
        *,
        case_insensitive: bool = False,
        lock_for_update: bool = False,
    ) -> list[User]:
        if not uids:
            return []
        if case_insensitive:
            query = select(User).where(func.lower(User.uid).in_({uid.lower() for uid in uids}))
        else:
            query = select(User).where(User.uid.in_(uids))
        if lock_for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        result = await db.execute(query)
        return list(result.scalars().all())


user_crud = CRUDUser(User)
