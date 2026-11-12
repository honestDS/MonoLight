import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.core.utils.context_summary import cleanup as cleanup_module
from app.core.utils.context_summary import reduction as reduction_module
from app.core.utils.context_summary import service as service_module
from app.core.utils.context_summary.boundary import (
    ContextSummaryTriggerMode,
    resolve_context_summary_boundary,
)
from app.core.utils.context_summary.common import (
    ContextSummaryState,
    ContextSummaryWorkInvalidError,
)
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot
from app.core.utils.context_summary.stage import GeneratedSummaryResult
from app.core.utils.context_summary.user_message_block import (
    append_covered_user_message,
    split_covered_user_message,
)
from app.core.utils.dispatcher import context_summary_checkpoint as checkpoint_module
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage
from app.models.message import InternalMessage, Message, MessageContentFormat, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan
from app.providers.llm.client import estimate_request_context_tokens


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


def _tool_result(message_id: int | None, tool_call_id: str) -> Message:
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


def _checkpoint_user_message(message_id: int, content: str) -> Message:
    message = _message(message_id, MessageRole.USER, content=content)
    message.environment_prompt = json.dumps(
        {
            "environment": {"runtime_context": "runtime context"},
            "platform_constraints": "platform constraint",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    message.guidance_prompt = "guidance prompt"
    return message


def _build_checkpoint_case(case_name: str, multimodal_kind: str | None = None) -> dict:
    def case(
        rows: list[Message],
        *,
        expected_summary_message_id: int,
        expected_retained_ids: list[int | None],
        expected_retained_call_ids: list[str],
        expected_retained_result_ids: list[str],
        protected_tool_call_ids: set[str] | None = None,
        extra_messages: list[InternalMessage] | None = None,
        expected_covered_user_message_id: int | None = None,
        expected_covered_user_message_content: str | None = None,
    ) -> dict:
        return {
            "rows": rows,
            "fixed_upper_message_id": 3,
            "protected_tool_call_ids": protected_tool_call_ids,
            "extra_messages": extra_messages or [],
            "expected_summary_message_id": expected_summary_message_id,
            "expected_retained_ids": expected_retained_ids,
            "expected_retained_call_ids": expected_retained_call_ids,
            "expected_retained_result_ids": expected_retained_result_ids,
            "expected_covered_user_message_id": expected_covered_user_message_id,
            "expected_covered_user_message_content": expected_covered_user_message_content,
        }

    history = [
        _message(1, MessageRole.USER, content="historical request " * 40),
        _message(2, MessageRole.ASSISTANT, content="historical answer " * 40),
    ]
    request = _checkpoint_user_message(3, "current request")
    if case_name == "complete_reverse":
        return case(
            [
                *history,
                request,
                _tool_call(4, "call-a", "call-b"),
                _tool_result(5, "call-b"),
                _tool_result(6, "call-a"),
            ],
            expected_summary_message_id=6,
            expected_retained_ids=[],
            expected_retained_call_ids=[],
            expected_retained_result_ids=[],
            expected_covered_user_message_id=3,
            expected_covered_user_message_content="current request",
        )
    if case_name == "complete_after_background_result":
        background_result = _message(
            4,
            MessageRole.USER,
            message_type=MessageType.BACKGROUND_TASK_RESULT,
            content=json.dumps(
                {
                    "type": "background_tool_result",
                    "task_id": 44,
                    "task": {"result": "background task result"},
                },
                ensure_ascii=False,
            ),
        )
        background_result.environment_prompt = json.dumps(
            {
                "environment": {"runtime_context": "background runtime context"},
                "platform_constraints": "background platform constraint",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        background_result.guidance_prompt = "background guidance prompt"
        return case(
            [
                *history,
                request,
                background_result,
                _tool_call(5, "call-a", "call-b"),
                _tool_result(6, "call-b"),
                _tool_result(7, "call-a"),
            ],
            expected_summary_message_id=7,
            expected_retained_ids=[],
            expected_retained_call_ids=[],
            expected_retained_result_ids=[],
            expected_covered_user_message_id=3,
            expected_covered_user_message_content="current request",
        )
    if case_name == "incomplete":
        return case(
            [*history, request, _tool_call(4, "call-a", "call-b"), _tool_result(5, "call-a")],
            expected_summary_message_id=2,
            expected_retained_ids=[3, 4, 5],
            expected_retained_call_ids=["call-a", "call-b"],
            expected_retained_result_ids=["call-a"],
        )
    if case_name == "protected":
        return case(
            [
                *history,
                request,
                _tool_call(4, "call-a", "call-b"),
                _tool_result(5, "call-b"),
                _tool_result(6, "call-a"),
            ],
            expected_summary_message_id=2,
            expected_retained_ids=[3, 4, 5, 6],
            expected_retained_call_ids=["call-a", "call-b"],
            expected_retained_result_ids=["call-b", "call-a"],
            protected_tool_call_ids={"call-a"},
        )
    if case_name == "missing_result_id":
        return case(
            [*history, request, _tool_call(4, "call-a", "call-b"), _tool_result(5, "call-a")],
            expected_summary_message_id=2,
            expected_retained_ids=[3, 4, 5, None],
            expected_retained_call_ids=["call-a", "call-b"],
            expected_retained_result_ids=["call-a", "call-b"],
            extra_messages=[
                InternalMessage(
                    role=MessageRole.TOOL,
                    type=MessageType.TOOL_RESULT,
                    tool_call_id="call-b",
                    content="result-call-b",
                )
            ],
        )
    if case_name == "incomplete_then_later":
        return case(
            [
                *history,
                request,
                _tool_call(4, "call-a", "call-b"),
                _tool_result(5, "call-a"),
                _tool_call(6, "call-c"),
                _tool_result(7, "call-c"),
            ],
            expected_summary_message_id=2,
            expected_retained_ids=[3, 4, 5, 6, 7],
            expected_retained_call_ids=["call-a", "call-b", "call-c"],
            expected_retained_result_ids=["call-a", "call-c"],
        )
    if case_name == "complete_then_incomplete":
        return case(
            [
                *history,
                request,
                _tool_call(4, "call-a", "call-b"),
                _tool_result(5, "call-b"),
                _tool_result(6, "call-a"),
                _tool_call(7, "call-c", "call-d"),
                _tool_result(8, "call-c"),
            ],
            expected_summary_message_id=6,
            expected_retained_ids=[7, 8],
            expected_retained_call_ids=["call-c", "call-d"],
            expected_retained_result_ids=["call-c"],
            expected_covered_user_message_id=3,
            expected_covered_user_message_content="current request",
        )
    if case_name == "multimodal":
        if multimodal_kind == "attachments":
            current = _checkpoint_user_message(7, "multimodal request")
            current.attachments = ["uploaded-file.bin"]
        elif multimodal_kind == "parts":
            current = _checkpoint_user_message(7, "multimodal request")
            current.content = json.dumps(
                [
                    {"type": "text", "text": "multimodal request"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}},
                ],
                ensure_ascii=False,
            )
            current.content_format = MessageContentFormat.PARTS
        else:
            raise AssertionError(f"unexpected multimodal kind: {multimodal_kind}")
        return case(
            [
                *history,
                _checkpoint_user_message(3, "previous request"),
                _tool_call(4, "call-a", "call-b"),
                _tool_result(5, "call-b"),
                _tool_result(6, "call-a"),
                current,
                _tool_call(8, "call-c"),
                _tool_result(9, "call-c"),
            ],
            expected_summary_message_id=6,
            expected_retained_ids=[7, 8, 9],
            expected_retained_call_ids=["call-c"],
            expected_retained_result_ids=["call-c"],
            expected_covered_user_message_id=3,
            expected_covered_user_message_content="previous request",
        )
    raise AssertionError(f"unexpected checkpoint case: {case_name}")


async def _load_checkpoint_rows(db_session: AsyncSession) -> list[Message]:
    result = await db_session.execute(select(Message).where(Message.session_id == "session-1", Message.uid == "user-1").order_by(Message.id.asc()))
    return list(result.scalars().all())


def _message_snapshot(rows: list[Message]) -> list[tuple]:
    return [
        (
            row.id,
            row.role,
            row.type,
            row.content,
            row.content_format,
            row.attachments,
            row.environment_prompt,
            row.guidance_prompt,
            row.model_context_suffix,
            row.is_processed,
        )
        for row in rows
    ]


async def _seed_checkpoint_case(db_session: AsyncSession, rows: list[Message], monkeypatch) -> None:
    connection = await db_session.connection()
    await connection.run_sync(SessionTodoPlan.__table__.create, checkfirst=True)
    await connection.run_sync(ContextSummaryStage.__table__.create, checkfirst=True)
    await connection.run_sync(ContextSummaryFragment.__table__.create, checkfirst=True)
    db_session.add(
        ChatSession(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
        )
    )
    db_session.add(
        SessionTodoPlan(
            session_id="session-1",
            uid="user-1",
            revision=7,
            todos=[{"content": "latest todo", "status": "in_progress"}],
        )
    )
    db_session.add_all(rows)
    await db_session.commit()
    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(service_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(cleanup_module, "AsyncSessionLocal", session_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name",
    [
        "complete_reverse",
        "complete_after_background_result",
        "incomplete",
        "protected",
        "missing_result_id",
        "incomplete_then_later",
        "complete_then_incomplete",
    ],
)
async def test_user_checkpoint_uses_completed_tool_batch_boundary_and_preserves_chains(
    db_session: AsyncSession,
    monkeypatch,
    case_name: str,
):
    test_case = _build_checkpoint_case(case_name)
    await _seed_checkpoint_case(db_session, test_case["rows"], monkeypatch)
    raw_rows = await _load_checkpoint_rows(db_session)
    raw_snapshot = _message_snapshot(raw_rows)
    parsed_messages = parse_db_messages_to_internal(raw_rows)

    async def generate_summary(_db, **_kwargs):
        return GeneratedSummaryResult(
            content="short model summary",
            message_count=3,
            completed_stage=SimpleNamespace(stage_key="boundary-checkpoint-test"),
        )

    monkeypatch.setattr(service_module, "generate_snapshot_summary_result", generate_summary)

    result = await checkpoint_module.apply_context_summary_checkpoint(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=1),
        cfg=SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=90)),
        messages=[InternalMessage(role=MessageRole.SYSTEM, content="system prompt"), *parsed_messages, *test_case["extra_messages"]],
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=test_case["fixed_upper_message_id"],
        context_window_k=8,
        max_tokens=128,
        tools=None,
        force=True,
        protected_tool_call_ids=test_case["protected_tool_call_ids"],
    )

    summary_message = next(message for message in result if message.conversation_summary is not None)
    retained_messages = [message for message in result if message.role != MessageRole.SYSTEM and message is not summary_message]
    assert [message.id for message in retained_messages] == test_case["expected_retained_ids"]
    assert [tool_call.id for message in retained_messages for tool_call in message.tool_calls or []] == test_case["expected_retained_call_ids"]
    assert [message.tool_call_id for message in retained_messages if message.role == MessageRole.TOOL] == test_case["expected_retained_result_ids"]

    summary_index = result.index(summary_message)
    materialized_result = materialize_user_environment_prompts([message.model_copy(deep=True) for message in result])
    materialized_summary = materialized_result[summary_index]
    assert isinstance(materialized_summary.content, str)
    materialized_payload = json.loads(materialized_summary.content)
    assert "short model summary" in materialized_payload["conversation_summary"]
    assert '<current_session_todo_snapshot>{"revision":7,"todos":[{"content":"latest todo","status":"in_progress"}]}</current_session_todo_snapshot>' in materialized_payload["conversation_summary"]
    assert "through_message_id" not in materialized_payload
    assert "covered_user_message_id" not in materialized_payload
    if test_case["expected_covered_user_message_id"] is None:
        assert materialized_payload["user_message"] is None
    else:
        assert materialized_payload["user_message"] == test_case["expected_covered_user_message_content"]

    original_messages_by_id = {message.id: message for message in parsed_messages if message.id is not None}
    for message in result[summary_index + 1 :]:
        if message.role != MessageRole.USER or message.id is None:
            continue
        assert message.content == original_messages_by_id[message.id].content

    session = await db_session.get(ChatSession, "session-1")
    assert session is not None
    await db_session.refresh(session)
    assert session.context_summary_message_id == test_case["expected_summary_message_id"]
    assert session.context_summary is not None
    model_summary, covered_user_message = split_covered_user_message(session.context_summary)
    assert model_summary is not None
    assert "short model summary" in model_summary
    assert '<current_session_todo_snapshot>{"revision":7,"todos":[{"content":"latest todo","status":"in_progress"}]}</current_session_todo_snapshot>' in model_summary
    if test_case["expected_covered_user_message_id"] is None:
        assert covered_user_message is None
    else:
        assert covered_user_message is not None
        assert covered_user_message.message_id == test_case["expected_covered_user_message_id"]
        assert covered_user_message.content == test_case["expected_covered_user_message_content"]

    if case_name in {"complete_reverse", "complete_after_background_result"}:
        summary_environment = json.loads(summary_message.environment_prompt or "{}")
        assert summary_environment["environment"]["runtime_context"] == "runtime context"
        assert summary_environment["platform_constraints"] == "platform constraint"
        assert summary_message.guidance_prompt == "guidance prompt"

    db_session.expire_all()
    assert _message_snapshot(await _load_checkpoint_rows(db_session)) == raw_snapshot


@pytest.mark.asyncio
async def test_tool_batch_checkpoint_refines_with_covered_user_runtime_context(
    db_session: AsyncSession,
    monkeypatch,
):
    covered_user = _checkpoint_user_message(1, "processed request")
    fixed_environment = "fixed environment " * 430
    covered_user.environment_prompt = json.dumps(
        {
            "environment": {"runtime_context": fixed_environment},
            "platform_constraints": "platform constraint",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    tool_result = _tool_result(3, "call-1")
    tool_result.content = json.dumps(
        {
            "role": MessageRole.TOOL,
            "tool_call_id": "call-1",
            "content": "raw evidence " * 1200,
        },
        ensure_ascii=False,
    )
    await _seed_checkpoint_case(
        db_session,
        [covered_user, _tool_call(2, "call-1"), tool_result],
        monkeypatch,
    )
    raw_rows = await _load_checkpoint_rows(db_session)
    parsed_messages = parse_db_messages_to_internal(raw_rows)
    candidate = "candidate detail " * 420
    candidate_stage = SimpleNamespace(content=candidate, stage_key="budget-checkpoint-test")

    async def generate_summary(_db, **_kwargs):
        return GeneratedSummaryResult(
            content=candidate,
            message_count=2,
            completed_stage=candidate_stage,
        )

    async def refine_completed_summary_stage(_db, *, lower_stage, **_kwargs):
        return SimpleNamespace(content="refined verified evidence", stage=lower_stage)

    monkeypatch.setattr(service_module, "generate_snapshot_summary_result", generate_summary)
    monkeypatch.setattr(reduction_module, "refine_completed_summary_stage", refine_completed_summary_stage)

    result = await checkpoint_module.apply_context_summary_checkpoint(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=1),
        cfg=SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=90)),
        messages=[InternalMessage(role=MessageRole.SYSTEM, content="system prompt"), *parsed_messages],
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=1,
        context_window_k=2,
        max_tokens=128,
        tools=None,
        model_id="gpt-4o-mini",
        protocol="openai",
        force=True,
    )

    summary_message = next(message for message in result if message.conversation_summary is not None)
    summary_meta = summary_message.conversation_summary
    assert summary_meta is not None
    expected_summary = 'refined verified evidence\n\n<current_session_todo_snapshot>{"revision":7,"todos":[{"content":"latest todo","status":"in_progress"}]}</current_session_todo_snapshot>'
    assert summary_meta.content == expected_summary
    assert summary_message.content == "processed request"
    materialized_summary = materialize_user_environment_prompts([summary_message.model_copy(deep=True)])[0]
    materialized_payload = json.loads(materialized_summary.content)
    assert materialized_payload["conversation_summary"] == expected_summary
    assert materialized_payload["user_message"] == "processed request"
    assert materialized_payload["environment"]["runtime_context"] == fixed_environment
    assert materialized_payload["platform_constraints"] == "platform constraint"
    assert materialized_payload["platform_guidance"] == "guidance prompt"

    trigger_tokens = calculate_context_summary_trigger_tokens(
        context_window_k=2,
        max_tokens=128,
        threshold_percent=90,
    )
    assert (
        estimate_request_context_tokens(
            materialize_user_environment_prompts(result),
            None,
            model_id="gpt-4o-mini",
            protocol="openai",
        )
        <= trigger_tokens
    )

    session = await db_session.get(ChatSession, "session-1")
    assert session is not None
    await db_session.refresh(session)
    assert session.context_summary_message_id == 3
    assert session.context_summary is not None
    assert "refined verified evidence" in session.context_summary
    assert [row.id for row in await _load_checkpoint_rows(db_session)] == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("multimodal_kind", ["attachments", "parts"])
async def test_user_checkpoint_preserves_multimodal_current_user_content(
    db_session: AsyncSession,
    monkeypatch,
    multimodal_kind: str,
):
    test_case = _build_checkpoint_case("multimodal", multimodal_kind)
    await _seed_checkpoint_case(db_session, test_case["rows"], monkeypatch)
    raw_rows = await _load_checkpoint_rows(db_session)
    raw_snapshot = _message_snapshot(raw_rows)
    parsed_messages = parse_db_messages_to_internal(raw_rows)
    original_current = next(message for message in parsed_messages if message.id == 7)

    async def generate_summary(_db, **_kwargs):
        return GeneratedSummaryResult(
            content="short model summary",
            message_count=3,
            completed_stage=SimpleNamespace(stage_key="multimodal-checkpoint-test"),
        )

    monkeypatch.setattr(service_module, "generate_snapshot_summary_result", generate_summary)

    result = await checkpoint_module.apply_context_summary_checkpoint(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=1),
        cfg=SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=90)),
        messages=[InternalMessage(role=MessageRole.SYSTEM, content="system prompt"), *parsed_messages],
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=test_case["fixed_upper_message_id"],
        context_window_k=8,
        max_tokens=128,
        tools=None,
        force=True,
    )

    retained_current = next(message for message in result if message.id == 7)
    assert retained_current.content == original_current.content
    assert retained_current.attachments == original_current.attachments
    assert [message.id for message in result if message.id in test_case["expected_retained_ids"]] == test_case["expected_retained_ids"]

    session = await db_session.get(ChatSession, "session-1")
    assert session is not None
    await db_session.refresh(session)
    assert session.context_summary_message_id == 6
    assert session.context_summary is not None
    _model_summary, covered_user_message = split_covered_user_message(session.context_summary)
    assert covered_user_message is not None
    assert covered_user_message.message_id == 3
    assert covered_user_message.content == "previous request"

    db_session.expire_all()
    assert _message_snapshot(await _load_checkpoint_rows(db_session)) == raw_snapshot


@pytest.mark.asyncio
async def test_user_checkpoint_does_not_cross_unprocessed_user_before_tool_result(
    db_session: AsyncSession,
    monkeypatch,
):
    pending_user = _message(3, MessageRole.USER, content="pending request")
    pending_user.is_processed = False
    await _seed_checkpoint_case(
        db_session,
        [
            _message(1, MessageRole.USER, content="processed request"),
            _tool_call(2, "call-1"),
            pending_user,
            _tool_result(4, "call-1"),
        ],
        monkeypatch,
    )

    raw_rows = await _load_checkpoint_rows(db_session)
    raw_snapshot = _message_snapshot(raw_rows)
    processed_rows = [row for row in raw_rows if row.is_processed]
    assert [row.id for row in processed_rows] == [1, 2, 4]
    input_messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system prompt"),
        *parse_db_messages_to_internal(processed_rows),
    ]
    original_messages = [message.model_copy(deep=True) for message in input_messages]

    session = await db_session.get(ChatSession, "session-1")
    assert session is not None
    original_summary_state = (
        session.context_summary,
        session.context_summary_message_id,
        session.context_summary_revision,
        session.context_content_revision,
    )

    result = await checkpoint_module.apply_context_summary_checkpoint(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=1),
        cfg=SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=90)),
        messages=input_messages,
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=1,
        context_window_k=8,
        max_tokens=128,
        tools=None,
        force=True,
    )

    assert result == original_messages

    await db_session.refresh(session)
    assert (
        session.context_summary,
        session.context_summary_message_id,
        session.context_summary_revision,
        session.context_content_revision,
    ) == original_summary_state

    db_session.expire_all()
    stored_rows = await _load_checkpoint_rows(db_session)
    assert _message_snapshot(stored_rows) == raw_snapshot
    assert next(row for row in stored_rows if row.id == 3).is_processed is False


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
    db_session: AsyncSession,
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
        db_session,
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
    summary_meta = result[1].conversation_summary
    assert summary_meta is not None
    assert "新累计总结" in summary_meta.content
    assert result[-1].id == 4


@pytest.mark.asyncio
async def test_checkpoint_reuses_persisted_snapshot_and_preserves_bounded_tool_body(monkeypatch, db_session: AsyncSession):
    captured = {}
    todo_snapshot = '<current_session_todo_snapshot>{"revision":7,"todos":[{"content":"verify","status":"in_progress"}]}</current_session_todo_snapshot>'
    watch_snapshot = '<current_terminal_watch_snapshot>{"type":"terminal_watch_state","terminals":[{"terminal_session_id":"term-1"}]}</current_terminal_watch_snapshot>'
    retained_tool_content = f"result\n\n{todo_snapshot}\n\n{watch_snapshot}"

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(content=f"compacted\n\n{todo_snapshot}", message_id=3)

    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(id=1, role=MessageRole.USER, content="old"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="tool call"),
        InternalMessage(id=3, role=MessageRole.TOOL, tool_call_id="call-1", content="boundary result"),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="next tool call"),
        InternalMessage(id=5, role=MessageRole.TOOL, tool_call_id="call-2", content=retained_tool_content),
    ]
    original_messages = [message.model_copy(deep=True) for message in messages]

    result = await checkpoint_module.apply_context_summary_checkpoint(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=object(),
        cfg=object(),
        messages=messages,
        trigger_mode=ContextSummaryTriggerMode.TOOL_RESULT,
        fixed_upper_message_id=3,
        context_window_k=8,
        max_tokens=512,
        tools=None,
    )

    summary_message = next(message for message in result if message.conversation_summary is not None)
    summary_meta = summary_message.conversation_summary
    assert summary_meta is not None
    assert todo_snapshot in summary_meta.content
    retained_tool = next(message for message in result if message.role == MessageRole.TOOL)
    fixed_tool = next(message for message in captured["fixed_request_messages"] if message.role == MessageRole.TOOL)
    assert retained_tool.content == retained_tool_content
    assert fixed_tool.content == retained_tool_content
    assert messages == original_messages


@pytest.mark.asyncio
async def test_checkpoint_uses_provider_baseline_even_when_todo_snapshot_exists(monkeypatch, db_session: AsyncSession):
    captured = {}
    todo_snapshot = '<current_session_todo_snapshot>{"revision":16,"todos":[{"content":"verify","status":"in_progress"}]}</current_session_todo_snapshot>'
    session = ChatSession(
        session_id="session-1",
        uid="user-1",
        context_summary_revision=1,
        context_content_revision=0,
    )
    db_session.add(session)
    await db_session.commit()

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(content=None, message_id=None)

    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    tool_content = f"result\n\n{todo_snapshot}"
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(id=1, role=MessageRole.USER, content="old"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="answer"),
        InternalMessage(id=3, role=MessageRole.TOOL, tool_call_id="call-1", content=tool_content),
        InternalMessage(id=4, role=MessageRole.USER, content="current"),
    ]

    previous_metadata = {
        "channel_id": 9,
        "input_tokens": 344_000,
        "input_tokens_source": "provider",
        "model_id": "gpt-5.6-luna",
        "protocol": "openai_responses",
        "context_summary_revision": 1,
        "context_content_revision": 0,
    }
    result = await checkpoint_module.apply_context_summary_checkpoint(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=object(),
        cfg=object(),
        messages=messages,
        trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        fixed_upper_message_id=4,
        context_window_k=250,
        max_tokens=20_480,
        tools=[],
        channel_id=9,
        model_id="gpt-5.6-luna",
        protocol="openai_responses",
        previous_llm_request_metadata=previous_metadata,
    )

    assert captured["confirmed_input_tokens"] == 344_000
    assert captured["reserved_tokens"] == 0
    assert [(message.id, message.content) for message in captured["fixed_request_messages"]] == [
        (None, "system"),
        (4, "current"),
    ]
    assert [(message.id, message.content) for message in result] == [(message.id, message.content) for message in messages]
    retained_tool = next(message for message in result if message.role == MessageRole.TOOL)
    assert retained_tool.content == tool_content


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
    summary_meta = result[1].conversation_summary
    assert summary_meta is not None
    assert "旧总结" in summary_meta.content
    assert result[-1].content == merged_content
    assert all(message.content != "追加A" for message in result)


@pytest.mark.asyncio
async def test_checkpoint_refreshes_same_work_content_version_when_provider_metadata_exists(monkeypatch):
    captured = {}
    previous_metadata = {
        "channel_id": 13,
        "input_tokens": 7500,
        "input_tokens_source": "provider",
        "model_id": "grok-4.5",
        "protocol": "openai",
        "context_summary_revision": 5,
        "context_content_revision": 7,
    }
    session = SimpleNamespace(
        context_summary_revision=2,
        context_content_revision=3,
        llm_request_metadata=None,
    )

    class FakeDb:
        def __init__(self):
            self.refreshed_sessions = []

        async def refresh(self, refreshed_session):
            self.refreshed_sessions.append(refreshed_session)
            refreshed_session.context_summary_revision = 5
            refreshed_session.context_content_revision = 7

    db = FakeDb()

    async def get_session(_db, _session_id):
        return session

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(content=None, message_id=None)

    monkeypatch.setattr(checkpoint_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)

    await checkpoint_module.apply_context_summary_checkpoint(
        db,
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
        channel_id=13,
        model_id="grok-4.5",
        protocol="openai",
        previous_llm_request_metadata=previous_metadata,
    )

    assert db.refreshed_sessions == [session]
    assert captured["confirmed_input_tokens"] == 7500


@pytest.mark.asyncio
async def test_checkpoint_preserves_logical_batches_when_physical_ids_run_in_reverse(monkeypatch):
    captured = {}

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(content="combined summary", message_id=2)

    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    logical_batches = [
        InternalMessage(id=92, role=MessageRole.USER, content="batch-b-first"),
        InternalMessage(id=90, role=MessageRole.USER, content="batch-b-second"),
        InternalMessage(id=82, role=MessageRole.USER, content="batch-c-first"),
        InternalMessage(id=80, role=MessageRole.USER, content="batch-c-second"),
    ]
    result = await asyncio.wait_for(
        checkpoint_module.apply_context_summary_checkpoint(
            object(),
            session_id="session-1",
            uid="user-1",
            profile=object(),
            cfg=object(),
            messages=[
                InternalMessage(role=MessageRole.SYSTEM, content="system"),
                InternalMessage(id=1, role=MessageRole.USER, content="old request"),
                InternalMessage(id=2, role=MessageRole.ASSISTANT, content="old answer"),
                *logical_batches,
            ],
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=80,
            context_window_k=8,
            max_tokens=512,
            tools=None,
        ),
        timeout=1,
    )

    assert captured["fixed_upper_message_id"] == 80
    assert [message.id for message in captured["fixed_request_messages"]] == [None, 92, 90, 82, 80]
    assert [message.id for message in result] == [None, None, 92, 90, 82, 80]
    assert [message.content for message in result[-4:]] == [
        "batch-b-first",
        "batch-b-second",
        "batch-c-first",
        "batch-c-second",
    ]


@pytest.mark.asyncio
async def test_persist_context_summary_rejects_stale_content_revision_then_accepts_retry(db_session: AsyncSession, monkeypatch):
    session = ChatSession(
        session_id="session-cas",
        uid="user-1",
        context_summary="prior summary",
        context_summary_message_id=8,
        context_summary_revision=4,
        context_content_revision=7,
    )
    db_session.add(session)
    await db_session.commit()
    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    expected_content_revisions = []
    update_context_summary = service_module.session_crud.update_context_summary

    async def trace_update_context_summary(*args, **kwargs):
        expected_content_revisions.append(kwargs["expected_content_revision"])
        return await update_context_summary(*args, **kwargs)

    monkeypatch.setattr(service_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(service_module.session_crud, "update_context_summary", trace_update_context_summary)
    await service_module.session_crud.bump_context_content_revision(
        db_session,
        session_id="session-cas",
        uid="user-1",
    )

    stale_updated = await asyncio.wait_for(
        service_module.persist_context_summary(
            session_id="session-cas",
            uid="user-1",
            expected_message_id=8,
            expected_revision=4,
            expected_content_revision=7,
            summary="stale summary",
            message_id=12,
        ),
        timeout=1,
    )
    retried_updated = await asyncio.wait_for(
        service_module.persist_context_summary(
            session_id="session-cas",
            uid="user-1",
            expected_message_id=None,
            expected_revision=5,
            expected_content_revision=8,
            summary="fresh summary",
            message_id=12,
        ),
        timeout=1,
    )
    await db_session.refresh(session)

    assert stale_updated is False
    assert retried_updated is True
    assert expected_content_revisions == [7, 8]
    assert session.context_summary == "fresh summary"
    assert session.context_summary.count("fresh summary") == 1
    assert session.context_summary_message_id == 12
    assert session.context_summary_revision == 6
    assert session.context_content_revision == 8


@pytest.mark.asyncio
async def test_context_summary_lease_loss_after_candidate_generation_never_persists_old_result(db_session: AsyncSession, monkeypatch):
    connection = await db_session.connection()
    await connection.run_sync(SessionTodoPlan.__table__.create, checkfirst=True)
    session = ChatSession(
        session_id="session-lease",
        uid="user-1",
        context_summary="prior summary",
        context_summary_message_id=8,
        context_summary_revision=4,
        context_content_revision=7,
    )
    db_session.add(session)
    await db_session.commit()
    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    lease_active = True
    persist_calls = []
    usage_call_count = 0

    async def build_snapshot(*_args, **_kwargs):
        return ContextSummarySnapshot(
            expected_summary_message_id=8,
            snapshot_before_id=13,
            snapshot_max_message_id=12,
            persistent_summary_target_id=12,
            recent_round_start_ids=(13,),
            frozen_user_message_ids=(),
            recent_messages=(),
            content_revision=7,
        )

    async def measure_history(*_args, **_kwargs):
        return 100, 2

    async def generate_summary(*_args, **_kwargs):
        nonlocal lease_active
        lease_active = False
        return GeneratedSummaryResult(
            content="candidate generated before lease loss",
            message_count=4,
            completed_stage=SimpleNamespace(content="candidate generated before lease loss"),
        )

    def calc_usage(*_args, **_kwargs):
        nonlocal usage_call_count
        usage_call_count += 1
        return {
            "context_window_tokens": 1024,
            "output_tokens": 128,
            "safety_tokens": 0,
            "input_budget": 896,
            "threshold_percent": 50,
            "summary_tokens": 1,
            "history_tokens": 100,
            "tools_tokens": 0,
            "current_message_tokens": 0,
            "history_message_count": 2,
            "reserved_tokens": 0,
            "required_tokens": 100 if usage_call_count == 1 else 1,
            "summary_trigger_tokens": 10,
            "compression_goal_tokens": 10,
        }

    async def check_work_validity():
        return lease_active

    async def persist_old_candidate(**kwargs):
        persist_calls.append(kwargs)
        return True

    async def cleanup_work(_work_dedupe_key):
        return None

    monkeypatch.setattr(service_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(service_module, "build_context_summary_snapshot", build_snapshot)
    monkeypatch.setattr(service_module, "measure_snapshot_history", measure_history)
    monkeypatch.setattr(service_module, "generate_snapshot_summary_result", generate_summary)
    monkeypatch.setattr(service_module, "calc_token_usage", calc_usage)
    monkeypatch.setattr(service_module, "persist_context_summary", persist_old_candidate)
    monkeypatch.setattr(service_module, "cleanup_context_summary_work_safely", cleanup_work)

    with pytest.raises(ContextSummaryWorkInvalidError):
        await asyncio.wait_for(
            service_module.ensure_context_summary(
                db_session,
                session_id="session-lease",
                uid="user-1",
                profile=SimpleNamespace(id=1),
                cfg=SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=50)),
                before_id=13,
                current_message="",
                context_window_k=1,
                max_tokens=128,
                reserved_tokens=0,
                work_validity_checker=check_work_validity,
                force=True,
            ),
            timeout=1,
        )
    await db_session.refresh(session)

    assert persist_calls == []
    assert session.context_summary == "prior summary"
    assert session.context_summary_message_id == 8
    assert session.context_summary_revision == 4
    assert session.context_content_revision == 7
