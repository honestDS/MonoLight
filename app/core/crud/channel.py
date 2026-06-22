from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.channel import (
    ChannelCreate,
    ChannelUpdate,
    ModelChannel,
)


class CRUDChannel(CRUDBase[ModelChannel, ChannelCreate, ChannelUpdate]):
    async def get_by_name(self, db: AsyncSession, name: str) -> ModelChannel | None:
        result = await db.execute(select(ModelChannel).where(ModelChannel.name == name))
        return result.scalars().first()

    async def create_with_plain_api_key(self, db: AsyncSession, *, obj_in: ChannelCreate) -> ModelChannel:
        obj_in_data = obj_in.model_dump()
        db_obj = self.model.model_validate(obj_in_data)
        db_obj.set_api_key_plaintext(obj_in.api_key)
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj


channel_crud = CRUDChannel(ModelChannel)
