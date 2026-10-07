import asyncio
import json
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import (
    InternalMessage,
    Message,
    MessageContentFormat,
    MessageResponse,
    MessageRole,
    MessageType,
    TextPart,
    VoiceTextPart,
)
from app.models.session import ChatSession
from app.providers.database import AsyncSessionLocal
from app.providers.database.client import CancellationSafeAsyncSession

TEST_SESSION_ID = "message-dedupe-session"
TEST_UID = "message-dedupe-user"


@pytest.fixture(autouse=True)
async def clean_message_table(database_factory, monkeypatch):
    async with database_factory(
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
    ) as factory:
        monkeypatch.setattr(sys.modules[__name__], "AsyncSessionLocal", factory)
        async with factory() as db:
            db.add(ChatSession(session_id=TEST_SESSION_ID, uid=TEST_UID))
            await db.commit()

        yield


@pytest.mark.asyncio
async def test_save_message_is_idempotent_by_dedupe_key():
    message = InternalMessage(role=MessageRole.ERR, content="reply failed", environment_prompt="internal notice")
    dedupe_key = "background-task:1:reply-error"

    async with AsyncSessionLocal() as db:
        first = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.ERR,
            MessageType.TEXT,
            message,
            1,
            dedupe_key=dedupe_key,
        )
        repeated = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.ERR,
            MessageType.TEXT,
            message,
            1,
            dedupe_key=dedupe_key,
        )
        count = await db.scalar(select(func.count()).select_from(Message).where(Message.dedupe_key == dedupe_key))
        persisted = await db.scalar(select(Message).where(Message.dedupe_key == dedupe_key))

    assert first.id == repeated.id
    assert first.environment_prompt == "internal notice"
    assert persisted.environment_prompt == "internal notice"
    assert count == 1


@pytest.mark.asyncio
async def test_save_message_persists_outbound_text_refinement_as_plain_text():
    async with AsyncSessionLocal() as db:
        saved = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.USER,
            MessageType.OUTBOUND_TEXT_REFINEMENT,
            InternalMessage(role=MessageRole.USER, content="refinement prompt"),
            1,
        )
        persisted = await db.get(Message, saved.id)

    assert persisted is not None
    assert persisted.type == MessageType.OUTBOUND_TEXT_REFINEMENT
    assert persisted.content == "refinement prompt"


@pytest.mark.asyncio
async def test_save_message_round_trips_text_content_formats():
    parts = [VoiceTextPart(text="转写", input_source="voice"), TextPart(text="补充")]
    serialized_parts = json.dumps([part.model_dump(mode="json") for part in parts], ensure_ascii=False)

    async with AsyncSessionLocal() as db:
        saved_parts = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.USER,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.USER, content=parts),
            1,
        )
        saved_text = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.USER,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.USER, content=serialized_parts),
            1,
        )
        persisted_parts = await db.get(Message, saved_parts.id)
        persisted_text = await db.get(Message, saved_text.id)
        assert persisted_parts is not None
        assert persisted_text is not None
        await db.refresh(persisted_parts)
        await db.refresh(persisted_text)

    assert persisted_parts.content == serialized_parts
    assert persisted_text.content == serialized_parts
    assert persisted_parts.content.encode() == persisted_text.content.encode()
    assert persisted_parts.content_format == MessageContentFormat.PARTS
    assert persisted_text.content_format == MessageContentFormat.TEXT

    assert isinstance(saved_parts.content, list)
    assert saved_parts.content == parts
    assert saved_parts.assembled_attachment_part_count == 0
    assert isinstance(saved_parts.content[0], VoiceTextPart)
    assert isinstance(saved_parts.content[1], TextPart)
    assert saved_text.content == serialized_parts
    assert isinstance(saved_text.content, str)

    response_parts = MessageResponse.model_validate(persisted_parts)
    response_text = MessageResponse.model_validate(persisted_text)
    assert response_parts.content == [part.model_dump(mode="json") for part in parts]
    assert isinstance(response_parts.content, list)
    assert response_text.content == serialized_parts
    assert isinstance(response_text.content, str)

    parsed_parts, parsed_text = parse_db_messages_to_internal([persisted_parts, persisted_text])
    assert parsed_parts.content == parts
    assert parsed_parts.assembled_attachment_part_count == 0
    assert isinstance(parsed_parts.content, list)
    assert isinstance(parsed_parts.content[0], VoiceTextPart)
    assert isinstance(parsed_parts.content[1], TextPart)
    assert parsed_text.content == serialized_parts
    assert isinstance(parsed_text.content, str)
    assert persisted_parts.content == serialized_parts
    assert persisted_text.content == serialized_parts


@pytest.mark.asyncio
async def test_save_message_persists_reasoning_content():
    async with AsyncSessionLocal() as db:
        saved = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="answer", reasoning_content="thinking"),
            1,
        )
        persisted = await db.get(Message, saved.id)

    assert saved.reasoning_content == "thinking"
    assert persisted is not None
    assert persisted.reasoning_content == "thinking"
    assert MessageResponse.model_validate(persisted).reasoning_content == "thinking"


@pytest.mark.asyncio
async def test_save_message_persists_explicit_created_at():
    created_at = datetime(2026, 7, 21, 6, 0, tzinfo=UTC)

    async with AsyncSessionLocal() as db:
        saved = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="reply"),
            1,
            created_at=created_at,
        )
        persisted = await db.get(Message, saved.id)

    assert persisted is not None
    assert persisted.created_at.replace(tzinfo=created_at.tzinfo) == created_at


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["commit", "rollback", "close"])
async def test_session_finishes_database_cleanup_before_propagating_cancellation(monkeypatch, method_name):
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def delayed_operation(_session):
        started.set()
        await release.wait()
        completed.set()

    monkeypatch.setattr(AsyncSession, method_name, delayed_operation)
    session = CancellationSafeAsyncSession()
    operation = asyncio.create_task(getattr(session, method_name)())
    await started.wait()

    operation.cancel()
    await asyncio.sleep(0)

    assert not operation.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert completed.is_set()
