from sqlalchemy import update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.models.message import (
    Message,
)


async def mark_initial_message_processed(db: AsyncSession, initial_msg_id: int):
    # 核心修复：拿到锁后才标记初始消息已处理，确保若进入队列，消息仍能被活跃调度器捡起
    await db.execute(update(Message).where(Message.id == initial_msg_id).values(is_processed=True))
    await db.commit()
