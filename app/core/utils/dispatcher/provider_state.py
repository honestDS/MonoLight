from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim
from app.models.message import (
    InternalMessage,
    MessageRole,
    build_provider_source,
    provider_source_matches,
)


async def discard_mismatched_provider_state(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    messages: list[InternalMessage],
    channel_id: int | None,
    model_id: str,
    protocol: str,
) -> None:
    source = build_provider_source(
        channel_id=channel_id,
        model_id=model_id,
        protocol=protocol,
    )
    context_message_ids = sorted({message.id for message in messages if message.role == MessageRole.ASSISTANT and message.id is not None})
    invalid_ids = set()
    discard_ids = set()
    for start in range(0, len(context_message_ids), 500):
        provider_states = await message_crud.get_provider_states(
            db,
            session_id=session_id,
            uid=uid,
            message_ids=context_message_ids[start : start + 500],
        )
        for message_id, provider_metadata, has_reasoning_content in provider_states:
            if provider_source_matches(provider_metadata, source):
                continue
            invalid_ids.add(message_id)
            if has_reasoning_content or bool(provider_metadata):
                discard_ids.add(message_id)

    if discard_ids:
        await ensure_session_reply_work_claim(db)
        message_ids = sorted(discard_ids)
        for start in range(0, len(message_ids), 500):
            await message_crud.discard_provider_states(
                db,
                session_id=session_id,
                uid=uid,
                message_ids=message_ids[start : start + 500],
            )

    for message in messages:
        if message.role != MessageRole.ASSISTANT:
            continue
        has_provider_state = message.reasoning_content is not None or bool(message.provider_metadata) or any(tool_call.provider_metadata for tool_call in message.tool_calls or [])
        if has_provider_state and (message.id in invalid_ids or not provider_source_matches(message.provider_metadata, source)):
            message.discard_provider_state()
