from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.crud.message import (
    message_crud,
)
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)


async def save_message(
    db: AsyncSession,
    session_id: str,
    uid: str,
    role: MessageRole,
    msg_type: MessageType,
    content: Any,
    profile_id: int,
    is_processed: bool = True,
) -> InternalMessage:
    # Determine attachments and final content payload
    attachments_to_save = None
    if hasattr(content, "attachments"):
        attachments_to_save = content.attachments

    obj_in_data = {
        "session_id": session_id,
        "uid": uid,
        "role": role,
        "type": msg_type,
        "content": (
            content.content
            if (
                msg_type == MessageType.TEXT
                and hasattr(
                    content,
                    "content",
                )
            )
            else (
                content.model_dump_json(exclude_none=True)
                if hasattr(
                    content,
                    "model_dump_json",
                )
                else str(content)
            )
        ),
        "attachments": attachments_to_save,
        "profile_id": profile_id,
        "is_processed": is_processed,
    }

    db_obj = await message_crud.create(
        db,
        obj_in=obj_in_data,
    )
    return InternalMessage(
        id=db_obj.id,
        role=role,
        content=db_obj.content,
        attachments=db_obj.attachments,
        created_at=db_obj.created_at.timestamp(),
    )
