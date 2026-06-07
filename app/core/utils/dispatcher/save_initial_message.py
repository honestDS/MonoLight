from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.utils.dispatcher.save_message import save_message
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)
from app.models.profile import (
    Profile,
)


async def save_initial_message(
    db: AsyncSession,
    session_id: str,
    uid: str,
    profile: Profile,
    message: Any,
    attachments: list[str] | None,
) -> InternalMessage:
    # 初始保存消息 (设置 is_processed=False，锁获取后才标记 True)
    initial_msg_obj = InternalMessage(
        role=MessageRole.USER,
        content=message,
        attachments=attachments,
    )
    return await save_message(
        db,
        session_id,
        uid,
        MessageRole.USER,
        MessageType.TEXT,
        initial_msg_obj,
        profile.id if profile and profile.id else -1,
        is_processed=False,
    )
