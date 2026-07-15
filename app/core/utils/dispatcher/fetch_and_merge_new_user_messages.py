from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.crud.message import (
    message_crud,
)
from app.core.utils.dispatcher.markdown_instruction import append_user_runtime_instructions
from app.models.message import (
    InternalMessage,
    MessageRole,
)


async def fetch_and_merge_new_user_messages(
    db: AsyncSession,
    session_id: str,
    uid: str,
    max_tokens: int = 0,
) -> list[InternalMessage]:
    """
    检索并合并未处理的新产生用户消息
    """
    raw_messages = await message_crud.get_unprocessed_messages(db, session_id=session_id, uid=uid)
    # 仅处理未标记的用户消息
    user_messages = []
    for message in raw_messages:
        if message.role == MessageRole.USER:
            user_messages.append(message)
    if not user_messages:
        return []

    # 获取数据库记录的ID集合以便更新
    message_ids = []
    for message in user_messages:
        if message.id is not None:
            message_ids.append(message.id)

    # 合并内容与附件
    merged_content = []
    merged_attachments = []
    for message in user_messages:
        if message.content:
            merged_content.append(str(message.content).strip())
        if message.attachments:
            merged_attachments.extend(message.attachments)

    # 标记为已处理 (通过 ORM 对象属性更新方式)
    for message in user_messages:
        message.is_processed = True
        db.add(message)
    await db.commit()

    # 返回合并后的单条 InternalMessage
    combined_message = InternalMessage(
        id=message_ids[-1] if message_ids else None,  # 使用最后一条的 ID
        role=MessageRole.USER,
        content="\n".join(merged_content) if merged_content else None,
        attachments=list(dict.fromkeys(merged_attachments)) if merged_attachments else None,
    )
    await append_user_runtime_instructions(db, session_id, combined_message, max_tokens)
    return [combined_message]
