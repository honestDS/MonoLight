from sqlalchemy import update
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

    async def get_multi_by_uids(self, db: AsyncSession, uids: list[str]) -> list[User]:
        if not uids:
            return []
        result = await db.execute(select(User).where(User.uid.in_(uids)))
        return list(result.scalars().all())


user_crud = CRUDUser(User)
