import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.context_messages import message_token_text
from app.core.utils.context_summary.history import (
    iter_persistent_summary_source_units,
    measure_complete_replacement_input,
    measure_snapshot_history,
)
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot, iter_persistent_summary_rounds
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.transformers.openai import OpenAIChatCompletionsTransformer


def _message(message_id: int, role: MessageRole, *, content: str | None = None) -> Message:
    return Message(
        id=message_id,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=role,
        type=MessageType.TEXT,
        content=content or f"message-{message_id}",
        is_processed=True,
    )


@pytest.mark.asyncio
async def test_snapshot_finds_recent_two_rounds_across_backward_pages(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.USER),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.TOOL),
            _message(8, MessageRole.USER),
            _message(9, MessageRole.ASSISTANT),
            _message(10, MessageRole.USER, content="current input"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=10,
        frozen_user_message_ids=[10],
        page_size=2,
    )

    assert snapshot.snapshot_before_id == 10
    assert snapshot.snapshot_max_message_id == 9
    assert snapshot.persistent_summary_target_id == 4
    assert snapshot.recent_round_start_ids == (5, 8)
    assert snapshot.frozen_user_message_ids == (10,)
    assert [message.id for message in snapshot.recent_messages] == [5, 6, 7, 8, 9]


@pytest.mark.asyncio
async def test_forward_round_scan_keeps_cross_page_rounds_complete_and_ordered(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.TOOL),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.USER),
            _message(8, MessageRole.ASSISTANT),
            _message(9, MessageRole.USER),
            _message(10, MessageRole.ASSISTANT),
            _message(11, MessageRole.USER, content="current input"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=11,
        page_size=2,
    )
    rounds = [
        [message.id for message in round_messages]
        async for round_messages in iter_persistent_summary_rounds(
            db_session,
            session_id="session-1",
            uid="user-1",
            snapshot=snapshot,
            page_size=2,
        )
    ]

    assert snapshot.persistent_summary_target_id == 6
    assert rounds == [[1, 2, 3, 4], [5, 6]]
    assert [message_id for round_ids in rounds for message_id in round_ids] == list(range(1, 7))


@pytest.mark.asyncio
async def test_snapshot_boundaries_ignore_new_messages_arriving_after_snapshot(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.USER),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.USER),
            _message(8, MessageRole.ASSISTANT),
            _message(9, MessageRole.USER, content="current input"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=9,
        frozen_user_message_ids=[9],
        page_size=3,
    )
    db_session.add_all(
        [
            _message(10, MessageRole.ASSISTANT, content="late response"),
            _message(11, MessageRole.USER, content="late input"),
        ]
    )
    await db_session.commit()

    scanned_ids = [
        message.id
        async for round_messages in iter_persistent_summary_rounds(
            db_session,
            session_id="session-1",
            uid="user-1",
            snapshot=snapshot,
            page_size=2,
        )
        for message in round_messages
    ]

    assert snapshot.snapshot_max_message_id == 8
    assert snapshot.persistent_summary_target_id == 4
    assert [message.id for message in snapshot.recent_messages] == [5, 6, 7, 8]
    assert scanned_ids == [1, 2, 3, 4]
    assert 9 not in scanned_ids
    assert 10 not in scanned_ids
    assert 11 not in scanned_ids


@pytest.mark.asyncio
async def test_frozen_user_ids_restore_snapshot_boundary_when_before_id_is_absent(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.USER),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.USER, content="frozen input part one"),
            _message(8, MessageRole.USER, content="frozen input part two"),
            _message(9, MessageRole.ASSISTANT, content="current work response"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=None,
        frozen_user_message_ids=[7, 8],
        page_size=2,
    )

    assert snapshot.snapshot_before_id == 7
    assert snapshot.snapshot_max_message_id == 6
    assert snapshot.frozen_user_message_ids == (7, 8)
    assert all(message.id < 7 for message in snapshot.recent_messages)
    assert snapshot.persistent_summary_target_id == 2


@pytest.mark.asyncio
async def test_model_excluded_user_message_extends_source_coverage_without_exposing_content(db_session: AsyncSession):
    covered_user_content = "必须逐字保留且不得交给总结模型的用户原文"
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content=covered_user_content),
            _message(2, MessageRole.ASSISTANT, content="并行工具调用"),
            _message(3, MessageRole.TOOL, content="工具结果一"),
            _message(4, MessageRole.TOOL, content="工具结果二"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=None,
        target_message_id=4,
        model_excluded_message_ids=[1],
    )
    units = [
        unit
        async for unit in iter_persistent_summary_source_units(
            db_session,
            session_id="session-1",
            uid="user-1",
            snapshot=snapshot,
            max_unit_tokens=10_000,
        )
    ]

    assert len(units) == 1
    assert units[0].message_start_id == 1
    assert units[0].message_end_id == 4
    assert covered_user_content not in units[0].content
    assert "并行工具调用" in units[0].content
    assert "工具结果一" in units[0].content
    assert "工具结果二" in units[0].content


@pytest.mark.parametrize(
    ("tool_content", "expected_tool_result"),
    [
        pytest.param(
            'first line\n"quoted value"\nlast line',
            'first line\n"quoted value"\nlast line',
            id="plain-multiline-tool-text",
        ),
        pytest.param(
            '{"status":"ok","items":[{"id":1,"name":"alpha"},{"id":2,"name":"beta"}]}',
            {"status": "ok", "items": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]},
            id="compact-json-tool-result",
        ),
    ],
)
@pytest.mark.asyncio
async def test_context_summary_budget_counts_outbound_tool_result_wrapper(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tool_content: str,
    expected_tool_result: object,
):
    monkeypatch.setattr("app.models.message.time", SimpleNamespace(time=lambda: 1.0))
    tool_call = InternalToolCall(
        id="snapshot-budget-call",
        name="inspect_context",
        arguments={"query": "budget"},
    )
    tool_call_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        tool_calls=[tool_call],
    )
    tool_result_message = InternalMessage(
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        tool_call_id=tool_call.id,
        content=tool_content,
    )
    user_message = _message(1, MessageRole.USER, content="inspect the result")
    tool_call_row = Message(
        id=2,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=tool_call_message.model_dump_json(exclude_none=True),
        is_processed=True,
    )
    tool_result_row = Message(
        id=3,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=tool_result_message.model_dump_json(exclude_none=True),
        is_processed=True,
    )
    db_session.add_all([user_message, tool_call_row, tool_result_row])
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=None,
        target_message_id=tool_result_row.id,
    )
    assert snapshot.persistent_summary_target_id == tool_result_row.id
    assert snapshot.has_persistent_history

    parsed_history = parse_db_messages_to_internal([user_message, tool_call_row, tool_result_row])
    parsed_tool_result = next(message for message in parsed_history if message.role == MessageRole.TOOL)
    materialized_history = materialize_user_environment_prompts(parsed_history)
    provider_history = OpenAIChatCompletionsTransformer.to_provider(materialized_history)
    provider_tool_result = next(message for message in provider_history if message.get("role") == MessageRole.TOOL.value and message.get("tool_call_id") == parsed_tool_result.tool_call_id)
    provider_tool_content = provider_tool_result["content"]
    assert isinstance(provider_tool_content, str)
    assert json.loads(provider_tool_content) == {"tool_result": expected_tool_result}

    baseline_tokens = sum(estimate_tokens(message_token_text(message)) for message in parsed_history if message.role != MessageRole.TOOL)
    raw_tool_tokens = estimate_tokens(tool_content)
    wrapped_tool_tokens = estimate_tokens(provider_tool_content)
    assert wrapped_tool_tokens > raw_tool_tokens
    expected_tokens = baseline_tokens + wrapped_tool_tokens

    snapshot_history_tokens, history_message_count = await measure_snapshot_history(
        db_session,
        session_id="session-1",
        uid="user-1",
        snapshot=snapshot,
        use_request_token_text=True,
    )
    replacement_input_tokens = await measure_complete_replacement_input(
        db_session,
        session_id="session-1",
        uid="user-1",
        snapshot=snapshot,
        existing_summary=None,
    )

    assert history_message_count == len(parsed_history)
    assert snapshot_history_tokens == expected_tokens
    assert replacement_input_tokens == expected_tokens


@pytest.mark.asyncio
async def test_snapshot_scan_processes_more_than_five_thousand_messages(db_session: AsyncSession):
    history_message_count = 5006
    db_session.add_all(
        [
            _message(
                message_id,
                MessageRole.USER if message_id % 2 == 1 else MessageRole.ASSISTANT,
            )
            for message_id in range(1, history_message_count + 1)
        ]
    )
    db_session.add(_message(history_message_count + 1, MessageRole.USER, content="current input"))
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=history_message_count + 1,
        page_size=200,
    )
    scanned_count = 0
    first_id = None
    last_id = None
    async for round_messages in iter_persistent_summary_rounds(
        db_session,
        session_id="session-1",
        uid="user-1",
        snapshot=snapshot,
        page_size=200,
    ):
        for message in round_messages:
            scanned_count += 1
            first_id = message.id if first_id is None else first_id
            last_id = message.id

    assert snapshot.snapshot_max_message_id == history_message_count
    assert snapshot.persistent_summary_target_id == history_message_count - 4
    assert [message.id for message in snapshot.recent_messages] == list(range(history_message_count - 3, history_message_count + 1))
    assert first_id == 1
    assert last_id == history_message_count - 4
    assert scanned_count == history_message_count - 4
