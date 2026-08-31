from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_CONTEXT_SUMMARY_MESSAGE_ID_REQUIRED
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.utils.dispatcher.markdown_instruction import append_user_runtime_instructions
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.message import InternalMessage, MessageRole


async def fetch_and_merge_new_user_messages(
    db: AsyncSession,
    session_id: str,
    uid: str,
    max_tokens: int = 0,
) -> UserInputBatch | None:
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
        return None

    source_message_ids: list[int] = []
    seen_message_ids: set[int] = set()
    for message in user_messages:
        if message.id is None:
            raise ValueError(t(ERR_CONTEXT_SUMMARY_MESSAGE_ID_REQUIRED))
        if message.id not in seen_message_ids:
            seen_message_ids.add(message.id)
            source_message_ids.append(message.id)

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
        id=source_message_ids[-1],
        role=MessageRole.USER,
        content="\n".join(merged_content) if merged_content else None,
        attachments=list(dict.fromkeys(merged_attachments)) if merged_attachments else None,
    )
    await append_user_runtime_instructions(db, session_id, combined_message, max_tokens)
    return UserInputBatch(
        messages=(combined_message,),
        source_message_ids=tuple(source_message_ids),
    )
