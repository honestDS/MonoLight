import asyncio
import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

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
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session import ChatSession


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[Message.__table__, ChatSession.__table__],
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
async def test_checkpoint_refreshes_same_work_content_version_when_provider_metadata_exists(monkeypatch):
    captured = {}
    previous_metadata = {
        "input_tokens": 7500,
        "input_tokens_source": "provider",
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

    def estimate_incremental(_messages, _tools, metadata, **kwargs):
        captured["estimate_metadata"] = metadata
        captured["estimate_kwargs"] = kwargs
        return kwargs["context_summary_revision"] * 1000 + kwargs["context_content_revision"]

    monkeypatch.setattr(checkpoint_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    monkeypatch.setattr(checkpoint_module, "estimate_incremental_input_tokens", estimate_incremental)

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
        model_id="grok-4.5",
        protocol="openai",
        previous_llm_request_metadata=previous_metadata,
    )

    assert db.refreshed_sessions == [session]
    assert captured["estimate_metadata"] is previous_metadata
    assert captured["estimate_kwargs"]["context_summary_revision"] == 5
    assert captured["estimate_kwargs"]["context_content_revision"] == 7
    assert captured["required_input_tokens_override"] == 5007


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
            ),
            timeout=1,
        )
    await db_session.refresh(session)

    assert persist_calls == []
    assert session.context_summary == "prior summary"
    assert session.context_summary_message_id == 8
    assert session.context_summary_revision == 4
    assert session.context_content_revision == 7
