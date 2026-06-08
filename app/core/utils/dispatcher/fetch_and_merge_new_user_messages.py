from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.crud.message import (
    message_crud,
)
from app.core.utils.dispatcher.markdown_instruction import append_session_markdown_instruction
from app.models.message import (
    InternalMessage,
    MessageRole,
)


async def fetch_and_merge_new_user_messages(
    db: AsyncSession,
    session_id: str,
    uid: str,
) -> list[InternalMessage]:
    """
    检索并合并未处理的新产生用户消息
    """
    raw_msgs = await message_crud.get_unprocessed_messages(db, session_id=session_id, uid=uid)
    # 仅处理未标记的用户消息
    user_msgs = [m for m in raw_msgs if m.role == MessageRole.USER]
    if not user_msgs:
        return []

    # 获取数据库记录的ID集合以便更新
    msg_ids = [m.id for m in user_msgs if m.id is not None]

    # 合并内容与附件
    merged_content = []
    merged_attachments = []
    for m in user_msgs:
        if m.content:
            merged_content.append(str(m.content).strip())
        if m.attachments:
            merged_attachments.extend(m.attachments)

    # 标记为已处理 (通过 ORM 对象属性更新方式)
    if user_msgs:
        for m in user_msgs:
            m.is_processed = True
            db.add(m)
        await db.commit()

    # 返回合并后的单条 InternalMessage
    combined_msg = InternalMessage(
        id=msg_ids[-1] if msg_ids else None,  # 使用最后一条的 ID
        role=MessageRole.USER,
        content="\n".join(merged_content) if merged_content else None,
        attachments=list(dict.fromkeys(merged_attachments)) if merged_attachments else None,
    )
    await append_session_markdown_instruction(db, session_id, combined_msg)
    return [combined_msg]
