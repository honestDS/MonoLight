import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.message import message_crud
from app.core.memory import chat_history as chat_history_module
from app.models.message import Message, MessageRole, MessageType

_MISSING = object()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[Message.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _message(
    message_id: int,
    *,
    uid: str = "user-1",
    session_id: str = "session-1",
    role: MessageRole = MessageRole.USER,
    message_type: MessageType = MessageType.TEXT,
    content: str | None | object = _MISSING,
) -> Message:
    return Message(
        id=message_id,
        uid=uid,
        session_id=session_id,
        profile_id=1,
        role=role,
        type=message_type,
        content=f"message-{message_id}" if content is _MISSING else content,
        is_processed=True,
    )


async def _commit(db_session: AsyncSession, *messages: Message) -> None:
    db_session.add_all(messages)
    await db_session.commit()


@pytest.mark.asyncio
async def test_chat_history_recall_isolated_by_uid(db_session: AsyncSession):
    await _commit(
        db_session,
        _message(1, uid="user-1", content="shared needle"),
        _message(2, uid="user-2", content="shared needle"),
    )

    result = await chat_history_module.chat_history_recall_service.recall(db_session, "user-1", "needle", top_k=10)

    assert [item.message_id for item in result.items] == [1]


@pytest.mark.asyncio
async def test_chat_history_recall_only_reads_text_user_and_assistant_messages(db_session: AsyncSession):
    await _commit(
        db_session,
        _message(1, role=MessageRole.USER, content="needle user"),
        _message(2, role=MessageRole.ASSISTANT, content="needle assistant"),
        _message(3, role=MessageRole.TOOL, message_type=MessageType.TOOL_RESULT, content="needle tool"),
        _message(4, role=MessageRole.SYSTEM, message_type=MessageType.GUIDANCE, content="needle guidance"),
        _message(5, role=MessageRole.USER, message_type=MessageType.BACKGROUND_TASK_RESULT, content="needle background"),
        _message(6, role=MessageRole.USER, message_type=MessageType.SCHEDULED_TASK_TRIGGER, content="needle scheduled"),
        _message(7, role=MessageRole.SYSTEM, content="needle system"),
        _message(8, role=MessageRole.TOOL, content="needle tool role"),
        _message(9, role=MessageRole.USER, content=""),
        _message(10, role=MessageRole.USER, content="   "),
    )

    result = await chat_history_module.chat_history_recall_service.recall(db_session, "user-1", "needle", top_k=20)

    assert {item.message_id for item in result.items} == {1, 2}


@pytest.mark.asyncio
async def test_chat_history_recall_excludes_current_message_with_before_message_id(db_session: AsyncSession):
    await _commit(
        db_session,
        _message(1, content="old needle"),
        _message(2, content="current needle"),
        _message(3, content="future needle"),
    )

    result = await chat_history_module.chat_history_recall_service.recall(
        db_session,
        "user-1",
        "needle",
        top_k=10,
        before_message_id=2,
    )

    assert [item.message_id for item in result.items] == [1]


@pytest.mark.asyncio
async def test_chat_history_recall_spans_sessions_for_one_uid(db_session: AsyncSession):
    await _commit(
        db_session,
        _message(1, session_id="session-1", content="cross session needle"),
        _message(2, session_id="session-2", role=MessageRole.ASSISTANT, content="cross session needle"),
    )

    result = await chat_history_module.chat_history_recall_service.recall(db_session, "user-1", "needle", top_k=10)

    assert {item.message_id for item in result.items} == {1, 2}


@pytest.mark.asyncio
async def test_chat_history_recall_parses_plain_and_multimodal_text(db_session: AsyncSession):
    multimodal_content = json.dumps(
        [
            {"type": "text", "text": "visual needle"},
            {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
            {"type": "text", "text": "caption"},
        ],
        ensure_ascii=False,
    )
    await _commit(
        db_session,
        _message(1, content="plain needle"),
        _message(2, content=multimodal_content),
    )

    result = await chat_history_module.chat_history_recall_service.recall(db_session, "user-1", "visual caption", top_k=10)

    assert [item.content for item in result.items] == ["visual needle\n[图片]\ncaption"]
    assert result.items[0].role == MessageRole.USER.value


@pytest.mark.asyncio
async def test_chat_history_recall_ranks_relevant_messages_and_prefers_newer_ties(db_session: AsyncSession):
    await _commit(
        db_session,
        _message(1, content="alpha match"),
        _message(2, content="alpha match"),
        _message(3, content="beta unrelated"),
    )

    result = await chat_history_module.chat_history_recall_service.recall(db_session, "user-1", "alpha", top_k=10)

    assert [item.message_id for item in result.items] == [2, 1]


@pytest.mark.asyncio
async def test_chat_history_recall_applies_top_k_and_character_budget(db_session: AsyncSession):
    await _commit(
        db_session,
        _message(1, content="token-aaa"),
        _message(2, content="token-bbb"),
        _message(3, content="token-ccc"),
    )

    top_k_result = await chat_history_module.chat_history_recall_service.recall(db_session, "user-1", "token", top_k=1)
    budget_result = await chat_history_module.chat_history_recall_service.recall(
        db_session,
        "user-1",
        "token",
        top_k=3,
        result_max_chars=13,
    )

    assert len(top_k_result.items) == 1
    assert [item.message_id for item in budget_result.items] == [3, 2]
    assert budget_result.items[0].content == "token-ccc"
    assert budget_result.items[0].truncated is False
    assert budget_result.items[1].content == "toke"
    assert budget_result.items[1].truncated is True
    assert sum(len(item.content) for item in budget_result.items) == 13


@pytest.mark.asyncio
async def test_chat_history_recall_limits_candidates_to_recent_messages(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_history_module, "MEMORY_CHAT_HISTORY_RECALL_CANDIDATE_LIMIT", 2)
    await _commit(
        db_session,
        _message(1, content="old needle"),
        _message(2, content="older needle"),
        _message(3, content="oldest needle"),
        _message(4, content="recent message"),
        _message(5, content="latest message"),
    )

    result = await chat_history_module.chat_history_recall_service.recall(db_session, "user-1", "needle", top_k=10)

    assert result.items == ()


@pytest.mark.asyncio
async def test_list_recallable_chat_page_applies_id_bounds_and_limit(db_session: AsyncSession):
    await _commit(
        db_session,
        *[_message(message_id, content=f"needle-{message_id}") for message_id in range(1, 5)],
    )

    page = await message_crud.list_recallable_chat_page(
        db_session,
        uid="user-1",
        before_message_id=5,
        limit=2,
    )

    assert [message.id for message in page] == [4, 3]
