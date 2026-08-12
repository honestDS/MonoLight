from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.base import CRUDBase
from app.models.profile import (
    Profile,
    ProfileConfig,
    ProfileCreate,
    ProfileUpdate,
)

_PROFILE_NON_PERSISTED_FIELDS = {
    "knowledge_base_ids",
    "confirm_memory_embedding_selection",
    "memory_embedding_selection_signature",
    "memory_organization",
}


class CRUDProfile(CRUDBase[Profile, ProfileCreate, ProfileUpdate]):
    async def create(
        self,
        db: AsyncSession,
        *,
        obj_in: ProfileCreate | dict,
        update_dict: dict | None = None,
        commit: bool = True,
    ) -> Profile:
        if isinstance(obj_in, dict):
            data = dict(obj_in)
            data = {key: value for key, value in data.items() if key not in _PROFILE_NON_PERSISTED_FIELDS}
        else:
            data = obj_in.model_dump(exclude=_PROFILE_NON_PERSISTED_FIELDS)
        return await super().create(db, obj_in=data, update_dict=update_dict, commit=commit)

    async def update(
        self,
        db: AsyncSession,
        *,
        db_obj: Profile,
        obj_in: ProfileUpdate | dict,
        commit: bool = True,
    ) -> Profile:
        if isinstance(obj_in, dict):
            data = dict(obj_in)
            data = {key: value for key, value in data.items() if key not in _PROFILE_NON_PERSISTED_FIELDS}
        else:
            data = obj_in.model_dump(exclude=_PROFILE_NON_PERSISTED_FIELDS, exclude_unset=True)
        return await super().update(db, db_obj=db_obj, obj_in=data, commit=commit)

    async def get_by_uid(self, db: AsyncSession, uid: str | None) -> Profile | None:
        stmt = select(Profile).where(Profile.uid == uid)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_with_relations(self, db: AsyncSession, id: int) -> Profile | None:
        stmt = select(Profile).where(Profile.id == id)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_snapshot(self, db: AsyncSession, id: int) -> Profile | None:
        result = await db.execute(select(Profile).where(Profile.id == id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def normalize_memory_selection_by_uid(
        self,
        db: AsyncSession,
        *,
        uid: str,
        embedding_channel_id: int | None,
        embedding_model_id: str | None,
        commit: bool = True,
    ) -> list[Profile]:
        result = await db.execute(select(Profile).where(Profile.uid == uid))
        profiles = list(result.scalars().all())
        for profile in profiles:
            configs = ProfileConfig.model_validate(profile.configs or {}).model_dump()
            configs["memory"]["embedding_channel_id"] = embedding_channel_id
            configs["memory"]["embedding_model_id"] = embedding_model_id
            profile.configs = configs
            db.add(profile)
        if commit:
            await db.commit()
        else:
            await db.flush()
        return profiles

    async def get_by_name(self, db: AsyncSession, name: str, uid: str | None = None) -> Profile | None:
        stmt = select(Profile).where(Profile.name == name).where(Profile.uid == uid)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_multi(self, db: AsyncSession, *, skip: int = 0, limit: int = 100, uid: str | None = None) -> list[Profile]:
        stmt = select(Profile).where(Profile.uid == uid).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_multi_all(self, db: AsyncSession, *, skip: int = 0, limit: int = 100) -> list[Profile]:
        stmt = select(Profile).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_ids(self, db: AsyncSession, profile_ids: set[int]) -> list[Profile]:
        if not profile_ids:
            return []
        result = await db.execute(select(Profile).where(Profile.id.in_(profile_ids)))
        return list(result.scalars().all())

    async def count(self, db: AsyncSession, uid: str | None = None) -> int:
        result = await db.execute(select(func.count()).select_from(Profile).where(Profile.uid == uid))
        return result.scalar()

    async def count_all(self, db: AsyncSession) -> int:
        result = await db.execute(select(func.count()).select_from(Profile))
        return result.scalar()

    async def get_default(self, db: AsyncSession, uid: str | None = None) -> Profile | None:
        stmt = select(Profile).where(Profile.uid == uid).where(Profile.is_default)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def clear_default_by_uid(self, db: AsyncSession, uid: str | None) -> None:
        profiles = await self.get_multi(db, uid=uid)
        for profile in profiles:
            profile.is_default = False
            db.add(profile)

    async def get_multi_by_prompt_id(self, db: AsyncSession, prompt_id: int) -> list[Profile]:
        result = await db.execute(select(Profile).where(Profile.prompt_id == prompt_id))
        return list(result.scalars().all())

    async def reassign_prompt(self, db: AsyncSession, *, source_prompt_id: int, target_prompt_id: int) -> int:
        profiles = await self.get_multi_by_prompt_id(db, source_prompt_id)
        for profile in profiles:
            profile.prompt_id = target_prompt_id
            db.add(profile)
        return len(profiles)


profile_crud = CRUDProfile(Profile)
