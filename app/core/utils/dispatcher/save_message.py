import json
from datetime import datetime
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.audit.integrity import canonical_json_dumps
from app.core.crud.session.message import (
    message_crud,
)
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim
from app.models.message import (
    InternalMessage,
    MessageContentFormat,
    MessageRole,
    MessageType,
    decode_message_content,
)


def _to_storable_content(content: Any, msg_type: MessageType) -> str:
    if msg_type == MessageType.AUDIT_CONFIRMATION:
        if hasattr(content, "model_dump"):
            content = content.model_dump(mode="json", exclude_none=True)
        return canonical_json_dumps(content)

    if msg_type in {MessageType.TEXT, MessageType.AUDIT_DECISION, MessageType.BACKGROUND_TASK_RESULT, MessageType.OUTBOUND_TEXT_REFINEMENT} and hasattr(content, "content"):
        payload = content.content
        if isinstance(payload, str) or payload is None:
            return payload or ""
        if hasattr(content, "model_dump"):
            payload = content.model_dump(mode="json", include={"content"}).get("content")
        return json.dumps(payload, ensure_ascii=False)

    if msg_type == MessageType.TOOL_CALL and hasattr(content, "model_dump"):
        return canonical_json_dumps(content.model_dump(mode="python", exclude={"reasoning_content", "provider_metadata"}, exclude_none=True))

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
    created_at: datetime | None = None,
    audit_record_id: int | None = None,
    audit_tool_call_id: str | None = None,
    content_revision: int = 0,
    commit: bool = True,
) -> InternalMessage:
    if msg_type == MessageType.AUDIT_CONFIRMATION:
        await ensure_session_reply_work_claim(db)

    # Determine attachments and final content payload
    attachments_to_save = None
    environment_prompt_to_save = None
    reasoning_content_to_save = None
    provider_metadata_to_save = None
    if hasattr(content, "attachments"):
        attachments_to_save = content.attachments
    if hasattr(content, "environment_prompt"):
        environment_prompt_to_save = content.environment_prompt
    if hasattr(content, "reasoning_content"):
        reasoning_content_to_save = content.reasoning_content
    if hasattr(content, "provider_metadata"):
        provider_metadata_to_save = content.provider_metadata

    obj_in_data = {
        "session_id": session_id,
        "uid": uid,
        "role": role,
        "type": msg_type,
        "content": _to_storable_content(content, msg_type),
        "content_format": (
            MessageContentFormat.PARTS
            if msg_type
            in {
                MessageType.TEXT,
                MessageType.AUDIT_DECISION,
                MessageType.BACKGROUND_TASK_RESULT,
                MessageType.OUTBOUND_TEXT_REFINEMENT,
            }
            and isinstance(getattr(content, "content", None), list)
            else MessageContentFormat.TEXT
        ),
        "reasoning_content": reasoning_content_to_save,
        "provider_metadata": provider_metadata_to_save,
        "environment_prompt": environment_prompt_to_save,
        "attachments": attachments_to_save,
        "profile_id": profile_id,
        "is_processed": is_processed,
        "audit_record_id": audit_record_id,
        "audit_tool_call_id": audit_tool_call_id,
        "content_revision": content_revision,
    }
    if created_at is not None:
        obj_in_data["created_at"] = created_at

    if dedupe_key is None:
        db_obj = await message_crud.create(
            db,
            obj_in=obj_in_data,
            commit=commit,
        )
    else:
        db_obj = await message_crud.create_idempotent(
            db,
            obj_in=obj_in_data,
            dedupe_key=dedupe_key,
            commit=commit,
        )
    message_kwargs = {
        "id": db_obj.id,
        "content": decode_message_content(db_obj.content, db_obj.content_format),
        "reasoning_content": db_obj.reasoning_content,
        "provider_metadata": db_obj.provider_metadata,
        "environment_prompt": db_obj.environment_prompt,
        "attachments": db_obj.attachments,
        "created_at": db_obj.created_at.timestamp(),
    }
    if role == MessageRole.USER and msg_type == MessageType.TEXT:
        return InternalMessage.from_user_input(**message_kwargs)
    return InternalMessage(role=role, **message_kwargs)
