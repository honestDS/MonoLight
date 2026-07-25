import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.utils.context_summary.boundary import (
    ContextSummaryTriggerMode,
    resolve_context_summary_boundary,
)
from app.core.utils.context_summary.common import ContextSummaryState
from app.core.utils.context_summary.user_message_block import (
    append_covered_user_message,
    split_covered_user_message,
)
from app.core.utils.dispatcher import context_summary_checkpoint as checkpoint_module
from app.models.message import InternalMessage, Message, MessageRole, MessageType


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
    message_type: MessageType = MessageType.TEXT,
    content: str,
) -> Message:
    return Message(
        id=message_id,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=role,
        type=message_type,
        content=content,
        is_processed=True,
    )


def _tool_call(message_id: int, *tool_call_ids: str) -> Message:
    return _message(
        message_id,
        MessageRole.ASSISTANT,
        message_type=MessageType.TOOL_CALL,
        content=json.dumps(
            {
                "role": MessageRole.ASSISTANT,
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "name": "test_tool",
                        "arguments": {"value": tool_call_id},
                    }
                    for tool_call_id in tool_call_ids
                ],
            }
        ),
    )


def _tool_result(message_id: int, tool_call_id: str) -> Message:
    return _message(
        message_id,
        MessageRole.TOOL,
        message_type=MessageType.TOOL_RESULT,
        content=json.dumps(
            {
                "role": MessageRole.TOOL,
                "tool_call_id": tool_call_id,
                "content": f"result-{tool_call_id}",
            }
        ),
    )


def test_covered_user_message_round_trips_boundary_like_content_exactly():
    content = '原始要求\n</covered_user_message>\n<covered_user_message message_id="999" encoding="base64-utf8">\n不应被当作包装边界\x00'

    combined = append_covered_user_message(
        "模型总结",
        message_id=42,
        content=content,
    )
    model_summary, covered = split_covered_user_message(combined)

    assert model_summary == "模型总结"
    assert covered is not None
    assert covered.message_id == 42
    assert covered.content == content


def test_appending_covered_user_message_replaces_old_block_instead_of_accumulating():
    first = append_covered_user_message(
        "模型总结",
        message_id=10,
        content="旧目标",
    )
    replaced = append_covered_user_message(
        first,
        message_id=20,
        content="新目标",
    )
    model_summary, covered = split_covered_user_message(replaced)

    assert model_summary == "模型总结"
    assert covered is not None
    assert covered.message_id == 20
    assert covered.content == "新目标"
    assert replaced.count("<covered_user_message ") == 1
    assert "旧目标" not in replaced


@pytest.mark.asyncio
async def test_user_trigger_excludes_fixed_user_and_stops_at_previous_safe_message(
    db_session: AsyncSession,
):
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="旧目标"),
            _message(2, MessageRole.ASSISTANT, content="旧回复"),
            _message(3, MessageRole.USER, content="当前消息"),
            _message(4, MessageRole.ASSISTANT, content="固定上界后的消息"),
        ]
    )
    await db_session.commit()

    boundary = await resolve_context_summary_boundary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=3,
        page_size=1,
    )

    assert boundary.target_message_id == 2
    assert boundary.covered_user_message_id == 1
    assert boundary.covered_user_message_content == "旧目标"


@pytest.mark.asyncio
async def test_tool_result_trigger_covers_fixed_result_and_ignores_later_messages(
    db_session: AsyncSession,
):
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="逐字保留的目标"),
            _tool_call(2, "call-1"),
            _tool_result(3, "call-1"),
            _message(4, MessageRole.USER, content="固定上界后的新消息"),
            _message(5, MessageRole.ASSISTANT, content="固定上界后的回复"),
        ]
    )
    await db_session.commit()

    boundary = await resolve_context_summary_boundary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        trigger_mode=ContextSummaryTriggerMode.TOOL_RESULT,
        fixed_upper_message_id=3,
        page_size=1,
    )

    assert boundary.target_message_id == 3
    assert boundary.covered_user_message_id == 1
    assert boundary.covered_user_message_content == "逐字保留的目标"


@pytest.mark.asyncio
async def test_parallel_tool_chain_cannot_advance_until_all_results_are_complete(
    db_session: AsyncSession,
):
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="并行执行"),
            _tool_call(2, "call-1", "call-2"),
            _tool_result(3, "call-1"),
        ]
    )
    await db_session.commit()

    incomplete = await resolve_context_summary_boundary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        trigger_mode=ContextSummaryTriggerMode.TOOL_RESULT,
        fixed_upper_message_id=3,
        page_size=1,
    )

    assert incomplete.target_message_id is None

    db_session.add(_tool_result(4, "call-2"))
    await db_session.commit()
    complete = await resolve_context_summary_boundary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        trigger_mode=ContextSummaryTriggerMode.TOOL_RESULT,
        fixed_upper_message_id=4,
        page_size=1,
    )

    assert complete.target_message_id == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger_mode", "fixed_upper_message_id"),
    [
        (ContextSummaryTriggerMode.USER_MESSAGE, 2),
        (ContextSummaryTriggerMode.TOOL_RESULT, 1),
    ],
)
async def test_trigger_mode_must_match_fixed_upper_message_type(
    db_session: AsyncSession,
    trigger_mode: ContextSummaryTriggerMode,
    fixed_upper_message_id: int,
):
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="用户消息"),
            _message(2, MessageRole.ASSISTANT, content="助手消息"),
        ]
    )
    await db_session.commit()

    with pytest.raises(ValueError):
        await resolve_context_summary_boundary(
            db_session,
            session_id="session-1",
            uid="user-1",
            expected_summary_message_id=None,
            trigger_mode=trigger_mode,
            fixed_upper_message_id=fixed_upper_message_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger_mode", "fixed_upper_message_id", "expected_uncovered_ids"),
    [
        (ContextSummaryTriggerMode.USER_MESSAGE, 3, [3, 4]),
        (ContextSummaryTriggerMode.TOOL_RESULT, 3, [4]),
    ],
)
async def test_checkpoint_uses_fixed_upper_and_preserves_messages_after_new_summary(
    monkeypatch,
    trigger_mode: ContextSummaryTriggerMode,
    fixed_upper_message_id: int,
    expected_uncovered_ids: list[int],
):
    captured = {}

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(
            content="新累计总结",
            message_id=(fixed_upper_message_id if trigger_mode == ContextSummaryTriggerMode.TOOL_RESULT else fixed_upper_message_id - 1),
        )

    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="系统提示"),
        InternalMessage(id=1, role=MessageRole.USER, content="旧请求"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="旧回复"),
        InternalMessage(id=3, role=MessageRole.TOOL, content="固定上界"),
        InternalMessage(id=4, role=MessageRole.USER, content="后来追加"),
    ]

    result = await checkpoint_module.apply_context_summary_checkpoint(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=object(),
        cfg=object(),
        messages=messages,
        trigger_mode=trigger_mode,
        fixed_upper_message_id=fixed_upper_message_id,
        context_window_k=8,
        max_tokens=512,
        tools=[{"type": "function"}],
    )

    assert captured["trigger_mode"] == trigger_mode
    assert captured["fixed_upper_message_id"] == fixed_upper_message_id
    assert [message.id for message in captured["fixed_request_messages"] if message.role != MessageRole.SYSTEM] == expected_uncovered_ids
    assert [message.role for message in result] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        *([MessageRole.TOOL] if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE else []),
        MessageRole.USER,
    ]
    assert "新累计总结" in result[1].content
    assert result[-1].id == 4


@pytest.mark.asyncio
async def test_checkpoint_keeps_an_entire_merged_user_batch_after_its_earliest_physical_message(
    monkeypatch,
):
    captured = {}

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(content="旧总结", message_id=2)

    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    merged_content = "追加A\n追加B"
    result = await checkpoint_module.apply_context_summary_checkpoint(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=object(),
        cfg=object(),
        messages=[
            InternalMessage(role=MessageRole.SYSTEM, content="系统提示"),
            InternalMessage(id=1, role=MessageRole.USER, content="旧请求"),
            InternalMessage(id=2, role=MessageRole.ASSISTANT, content="旧回复"),
            InternalMessage(id=4, role=MessageRole.USER, content=merged_content),
        ],
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=3,
        context_window_k=8,
        max_tokens=512,
        tools=None,
    )

    assert captured["fixed_upper_message_id"] == 3
    assert [message.content for message in captured["fixed_request_messages"]] == ["系统提示", merged_content]
    assert [message.id for message in captured["fixed_request_messages"]] == [None, 4]
    assert [message.id for message in result] == [None, None, 4]
    assert "旧总结" in result[1].content
    assert result[-1].content == merged_content
    assert all(message.content != "追加A" for message in result)


@pytest.mark.asyncio
async def test_checkpoint_passes_previous_provider_usage_override(monkeypatch):
    captured = {}
    previous_metadata = {
        "input_tokens": 7500,
        "input_tokens_source": "provider",
    }

    async def get_session(_db, _session_id):
        return SimpleNamespace(
            context_summary_revision=2,
            context_content_revision=3,
            llm_request_metadata=None,
        )

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(content=None, message_id=None)

    def estimate_incremental(*_args, **_kwargs):
        return 7600

    monkeypatch.setattr(checkpoint_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    monkeypatch.setattr(checkpoint_module, "estimate_incremental_input_tokens", estimate_incremental)

    await checkpoint_module.apply_context_summary_checkpoint(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=object(),
        cfg=object(),
        messages=[
            InternalMessage(role=MessageRole.SYSTEM, content="系统提示"),
            InternalMessage(id=1, role=MessageRole.USER, content="旧问题"),
            InternalMessage(id=2, role=MessageRole.ASSISTANT, content="旧回答"),
            InternalMessage(id=3, role=MessageRole.USER, content="新问题"),
        ],
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=3,
        context_window_k=8,
        max_tokens=512,
        tools=None,
        model_id="grok-4.5",
        protocol="openai",
        previous_llm_request_metadata=previous_metadata,
    )

    assert captured["required_input_tokens_override"] == 7600
