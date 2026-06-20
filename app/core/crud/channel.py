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


channel_crud = CRUDChannel(ModelChannel)