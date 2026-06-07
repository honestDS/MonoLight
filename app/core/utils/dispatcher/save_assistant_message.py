from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.utils.dispatcher.save_message import save_message
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)


async def save_assistant_message(
    db: AsyncSession,
    session_id: str,
    uid: str,
    profile_id: int,
    ai_msg: InternalMessage,
):
    from app.core.crud.session import session_crud
    from app.core.utils.dispatcher.process_markdown_response import process_markdown_response

    session = await session_crud.get_by_session_id(db, session_id)
    enable_markdown = session.enable_markdown if session else False

    # 清洗 Markdown 标记
    ai_msg = process_markdown_response(ai_msg, enable_markdown)

    saved_msg = await save_message(
        db,
        session_id,
        uid,
        MessageRole.ASSISTANT,
        MessageType.TOOL_CALL if ai_msg.tool_calls else MessageType.TEXT,
        ai_msg,
        profile_id,
        is_processed=True,
    )
    return saved_msg
