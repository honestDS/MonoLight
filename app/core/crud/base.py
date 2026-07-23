from typing import (
    Any,
    TypeVar,
)

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import (
    SQLModel,
    select,
)

ModelType = TypeVar("ModelType", bound=SQLModel)
CreateSchemaType = TypeVar("CreateSchemaType", bound=SQLModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=SQLModel)


class CRUDBase[ModelType: SQLModel, CreateSchemaType: SQLModel, UpdateSchemaType: SQLModel]:
    def __init__(self, model: type[ModelType]):
        self.model = model

    async def get(self, db: AsyncSession, id: Any) -> ModelType | None:
        result = await db.execute(select(self.model).where(self.model.id == id))
        return result.scalars().first()

    async def get_multi(self, db: AsyncSession, *, skip: int = 0, limit: int = 100) -> list[ModelType]:
        result = await db.execute(select(self.model).order_by(self.model.id.desc()).offset(skip).limit(limit))
        return result.scalars().all()

    async def count(self, db: AsyncSession) -> int:
        result = await db.execute(select(func.count()).select_from(self.model))
        return result.scalar()

    async def create(
        self,
        db: AsyncSession,
        *,
        obj_in: CreateSchemaType | dict[str, Any],
        update_dict: dict[str, Any] = None,
        commit: bool = True,
    ) -> ModelType:
        if isinstance(obj_in, dict):
            obj_in_data = obj_in
        else:
            obj_in_data = obj_in.model_dump()

        if update_dict:
            obj_in_data.update(update_dict)
        db_obj = self.model.model_validate(obj_in_data)
        db.add(db_obj)
        if commit:
            await db.commit()
        else:
            await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db: AsyncSession,
        *,
        db_obj: ModelType,
        obj_in: UpdateSchemaType | dict[str, Any],
    ) -> ModelType:
        obj_data = db_obj.model_dump()
        if isinstance(obj_in, dict):
            update_data = obj_in
        else:
            update_data = obj_in.model_dump(exclude_unset=True)
        for field in obj_data:
            if field in update_data:
                setattr(db_obj, field, update_data[field])
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def remove(self, db: AsyncSession, *, id: Any) -> ModelType | None:
        obj = await self.get(db, id)
        if obj:
            await db.delete(obj)
            await db.commit()
        return obj
