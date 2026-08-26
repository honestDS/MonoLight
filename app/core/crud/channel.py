from collections.abc import Iterable

from sqlalchemy import update
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

    async def lock_for_mutation(
        self,
        db: AsyncSession,
        *,
        channel_id: int,
        commit: bool = True,
    ) -> ModelChannel | None:
        # 空更新兼容 SQLite/MySQL 取得写锁，使并发渠道更新在读取最新状态前串行执行。
        await db.execute(update(ModelChannel).where(ModelChannel.id == channel_id).values(id=ModelChannel.id))
        result = await db.execute(select(ModelChannel).where(ModelChannel.id == channel_id).with_for_update().execution_options(populate_existing=True))
        channel = result.scalar_one_or_none()
        if commit:
            await db.commit()
        else:
            await db.flush()
        return channel

    async def lock_many_for_mutation(
        self,
        db: AsyncSession,
        *,
        channel_ids: Iterable[int],
        commit: bool = True,
    ) -> dict[int, ModelChannel]:
        """按渠道 ID 升序获取行锁，避免批量写入产生死锁。"""
        locked_channels: dict[int, ModelChannel] = {}
        for channel_id in sorted(set(channel_ids)):
            channel = await self.lock_for_mutation(
                db,
                channel_id=channel_id,
                commit=False,
            )
            if channel is not None:
                locked_channels[channel_id] = channel

        if commit:
            await db.commit()
        else:
            await db.flush()
        return locked_channels

    async def create_with_plain_api_key(
        self,
        db: AsyncSession,
        *,
        obj_in: ChannelCreate,
        commit: bool = True,
    ) -> ModelChannel:
        obj_in_data = obj_in.model_dump()
        db_obj = self.model.model_validate(obj_in_data)
        db_obj.set_api_key_plaintext(obj_in.api_key)
        db.add(db_obj)
        if commit:
            await db.commit()
        else:
            await db.flush()
        await db.refresh(db_obj)
        return db_obj


channel_crud = CRUDChannel(ModelChannel)
