import json
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core import context as context_module
from app.core.context import ContextManager
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
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
    role: MessageRole,
    *,
    content: str | None = None,
    message_type: MessageType = MessageType.TEXT,
) -> Message:
    return Message(
        id=message_id,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=role,
        type=message_type,
        content=content if content is not None else f"message-{message_id}",
        is_processed=True,
    )


def _profile() -> Profile:
    return Profile(id=1, uid="user-1", name="test-profile", configs={})


class CapturingLogger:
    def __init__(self):
        self.warning_messages: list[str] = []

    def bind(self, **_kwargs):
        return self

    def warning(self, message):
        self.warning_messages.append(str(message))

    def info(self, _message):
        return None


@pytest.mark.asyncio
async def test_context_history_reads_more_than_five_thousand_messages(db_session: AsyncSession):
    history_message_count = 5006
    db_session.add_all(
        [
            _message(
                message_id,
                MessageRole.USER if message_id % 2 == 1 else MessageRole.ASSISTANT,
                content="",
            )
            for message_id in range(1, history_message_count + 1)
        ]
    )
    db_session.add(
        _message(
            history_message_count + 1,
            MessageRole.USER,
            content="current input",
        )
    )
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="current input",
        before_id=history_message_count + 1,
        context_window_k=8,
    )

    assert len(messages) == history_message_count
    assert [message.id for message in messages[:3]] == [1, 2, 3]
    assert [message.id for message in messages[-3:]] == [5004, 5005, 5006]


@pytest.mark.asyncio
async def test_context_history_respects_fixed_id_bounds(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(
                message_id,
                MessageRole.USER if message_id % 2 == 1 else MessageRole.ASSISTANT,
            )
            for message_id in range(1, 11)
        ]
    )
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="current input",
        before_id=9,
        after_id=3,
        context_window_k=4,
    )

    assert [message.id for message in messages] == [4, 5, 6, 7, 8]


@pytest.mark.asyncio
async def test_context_history_keeps_tool_chain_complete_across_backward_pages(
    db_session: AsyncSession,
):
    tool_call_content = json.dumps(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "query_knowledge_base",
                    "arguments": {"query": "pagination"},
                }
            ],
        },
        ensure_ascii=False,
    )
    tool_result_content = json.dumps(
        {
            "tool_call_id": "call-1",
            "content": json.dumps({"result": "matched"}, ensure_ascii=False),
        },
        ensure_ascii=False,
    )
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="find context"),
            _message(
                2,
                MessageRole.ASSISTANT,
                content=tool_call_content,
                message_type=MessageType.TOOL_CALL,
            ),
            _message(
                3,
                MessageRole.TOOL,
                content=tool_result_content,
                message_type=MessageType.TOOL_RESULT,
            ),
            _message(4, MessageRole.ASSISTANT, content="final answer"),
        ]
    )
    await db_session.commit()

    raw_history = await ContextManager._load_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        before_id=None,
        after_id=None,
        limit_tokens=4096,
        current_msg_tokens=0,
        context_window_k=4,
        page_size=2,
    )
    parsed_history = parse_db_messages_to_internal(raw_history)
    messages, log_data = ContextManager._strategy_atomic_truncate(
        uid="user-1",
        session_id="session-1",
        parsed_history=parsed_history,
        limit_tokens=4096,
        current_msg_tokens=0,
        context_window_k=4,
    )

    assert [message.id for message in messages] == [1, 2, 3, 4]
    assert messages[1].tool_calls is not None
    assert messages[1].tool_calls[0].id == "call-1"
    assert messages[2].tool_call_id == "call-1"
    assert log_data["is_hard_truncated"] is False


def test_atomic_truncate_does_not_report_tool_result_orphaned_only_by_budget(
    monkeypatch,
):
    log = CapturingLogger()
    monkeypatch.setattr(context_module, "logger", log)
    messages = parse_db_messages_to_internal(
        [
            _message(
                2,
                MessageRole.ASSISTANT,
                content=json.dumps(
                    {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "name": "firecrawl_scrape",
                                "arguments": {},
                            }
                        ],
                    }
                ),
                message_type=MessageType.TOOL_CALL,
            ),
            _message(
                3,
                MessageRole.TOOL,
                content=json.dumps(
                    {
                        "tool_call_id": "call-1",
                        "content": "small result",
                    }
                ),
                message_type=MessageType.TOOL_RESULT,
            ),
        ]
    )

    retained, log_data = ContextManager._strategy_atomic_truncate(
        uid="user-1",
        session_id="session-1",
        parsed_history=list(reversed(messages)),
        limit_tokens=10,
        current_msg_tokens=0,
        context_window_k=1,
    )

    assert retained == []
    assert log_data["is_hard_truncated"] is True
    assert log.warning_messages == []


def test_atomic_truncate_still_reports_genuine_orphan_tool_result(
    monkeypatch,
):
    log = CapturingLogger()
    monkeypatch.setattr(context_module, "logger", log)
    orphan_result = parse_db_messages_to_internal(
        [
            _message(
                3,
                MessageRole.TOOL,
                content=json.dumps(
                    {
                        "tool_call_id": "missing-call",
                        "content": "orphan result",
                    }
                ),
                message_type=MessageType.TOOL_RESULT,
            )
        ]
    )

    retained, _log_data = ContextManager._strategy_atomic_truncate(
        uid="user-1",
        session_id="session-1",
        parsed_history=orphan_result,
        limit_tokens=1024,
        current_msg_tokens=0,
        context_window_k=1,
    )

    assert retained == []
    assert len(log.warning_messages) == 1
    assert "missing-call" in log.warning_messages[0]


def test_tool_audit_does_not_report_duplicate_known_results_as_orphaned(monkeypatch):
    log = CapturingLogger()
    monkeypatch.setattr(context_module, "logger", log)
    user_message = InternalMessage(role=MessageRole.USER, content="run tool")
    tool_call_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={})],
    )
    tool_call_message.tool_calls.append(InternalToolCall(id="call-1", name="execute_shell", arguments={}))
    first_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content="first")
    repeated_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content="repeated")

    retained = ContextManager.audit_tool_chain(
        [user_message, tool_call_message, first_result, repeated_result],
        uid="user-1",
        session_id="session-1",
    )

    assert retained == [user_message, tool_call_message, first_result]
    assert log.warning_messages == []


@pytest.mark.asyncio
async def test_context_history_stops_at_user_boundary_after_budget_is_reached(
    db_session: AsyncSession,
):
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="old-user " * 300),
            _message(2, MessageRole.ASSISTANT, content="old-answer " * 300),
            _message(3, MessageRole.USER, content="recent-user " * 300),
            _message(4, MessageRole.ASSISTANT, content="recent-answer " * 300),
            _message(5, MessageRole.USER, content="latest-user " * 300),
            _message(6, MessageRole.ASSISTANT, content="latest-answer " * 300),
        ]
    )
    await db_session.commit()

    raw_history = await ContextManager._load_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        before_id=None,
        after_id=None,
        limit_tokens=1024,
        current_msg_tokens=0,
        context_window_k=1,
        page_size=2,
    )

    loaded_ids = [message.id for message in raw_history]
    assert loaded_ids == sorted(loaded_ids, reverse=True)
    assert loaded_ids[-1] in {1, 3, 5}
    assert 1 not in loaded_ids
