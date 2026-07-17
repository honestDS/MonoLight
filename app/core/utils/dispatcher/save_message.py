import json
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.audit.integrity import canonical_json_dumps
from app.core.crud.message import (
    message_crud,
)
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)


def _to_storable_content(content: Any, msg_type: MessageType) -> str:
    if msg_type == MessageType.AUDIT_CONFIRMATION:
        if hasattr(content, "model_dump"):
            content = content.model_dump(mode="json", exclude_none=True)
        return canonical_json_dumps(content)

    if msg_type in {MessageType.TEXT, MessageType.BACKGROUND_TASK_RESULT} and hasattr(content, "content"):
        payload = content.content
        if isinstance(payload, str) or payload is None:
            return payload or ""
        if hasattr(content, "model_dump"):
            payload = content.model_dump(mode="json", include={"content"}).get("content")
        return json.dumps(payload, ensure_ascii=False)

    if msg_type == MessageType.TOOL_CALL and hasattr(content, "model_dump"):
        return canonical_json_dumps(content.model_dump(mode="python", exclude_none=True))

    if hasattr(content, "model_dump_json"):
        return content.model_dump_json(exclude_none=True)

    return str(content)


async def save_message(
    db: AsyncSession,
    session_id: str,
    uid: str,
    role: MessageRole,
    msg_type: MessageType,
    content: Any,
    profile_id: int,
    is_processed: bool = True,
    dedupe_key: str | None = None,
) -> InternalMessage:
    # Determine attachments and final content payload
    attachments_to_save = None
    environment_prompt_to_save = None
    if hasattr(content, "attachments"):
        attachments_to_save = content.attachments
    if hasattr(content, "environment_prompt"):
        environment_prompt_to_save = content.environment_prompt

    obj_in_data = {
        "session_id": session_id,
        "uid": uid,
        "role": role,
        "type": msg_type,
        "content": _to_storable_content(content, msg_type),
        "environment_prompt": environment_prompt_to_save,
        "attachments": attachments_to_save,
        "profile_id": profile_id,
        "is_processed": is_processed,
    }

    if dedupe_key is None:
        db_obj = await message_crud.create(
            db,
            obj_in=obj_in_data,
        )
    else:
        db_obj = await message_crud.create_idempotent(
            db,
            obj_in=obj_in_data,
            dedupe_key=dedupe_key,
        )
    return InternalMessage(
        id=db_obj.id,
        role=role,
        content=db_obj.content,
        environment_prompt=db_obj.environment_prompt,
        attachments=db_obj.attachments,
        created_at=db_obj.created_at.timestamp(),
    )
