import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.utils.context_summary import service as service_module
from app.core.utils.context_summary.boundary import ContextSummaryTriggerMode
from app.core.utils.context_summary.common import (
    ContextSummaryState,
    ContextSummaryWorkInvalidError,
)
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot
from app.core.utils.context_summary.stage import GeneratedSummaryResult
from app.core.utils.dispatcher import context_summary_checkpoint as checkpoint_module
from app.models.message import InternalMessage, MessageRole, StoredConversationSummary
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan


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
            content=StoredConversationSummary(conversation_summary="新累计总结").model_dump_json(exclude_none=True),
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
    assert summary_meta.content == "新累计总结"
    assert result[-1].id == 4


@pytest.mark.asyncio
async def test_checkpoint_reuses_persisted_snapshot_and_preserves_bounded_tool_body(monkeypatch, db_session: AsyncSession):
    captured = {}
    todo_snapshot = {"revision": 7, "todos": [{"content": "verify", "status": "in_progress"}]}
    watch_snapshot = {"type": "terminal_watch_state", "terminals": [{"terminal_session_id": "term-1"}]}
    retained_tool_content = "result"

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(
            content=StoredConversationSummary(
                conversation_summary="compacted",
                current_session_todo=todo_snapshot,
            ).model_dump_json(exclude_none=True),
            message_id=3,
        )

    monkeypatch.setattr(checkpoint_module, "ensure_context_summary", ensure_summary)
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(id=1, role=MessageRole.USER, content="old"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="tool call"),
        InternalMessage(id=3, role=MessageRole.TOOL, tool_call_id="call-1", content="boundary result"),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="next tool call"),
        InternalMessage(
            id=5,
            role=MessageRole.TOOL,
            tool_call_id="call-2",
            content=retained_tool_content,
            current_session_todo=todo_snapshot,
            current_terminal_watch=watch_snapshot,
        ),
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
    assert summary_meta.content == "compacted"
    assert summary_message.current_session_todo == todo_snapshot
    retained_tool = next(message for message in result if message.role == MessageRole.TOOL)
    fixed_tool = next(message for message in captured["fixed_request_messages"] if message.role == MessageRole.TOOL)
    assert retained_tool.content == retained_tool_content
    assert retained_tool.current_session_todo == todo_snapshot
    assert retained_tool.current_terminal_watch == watch_snapshot
    assert fixed_tool.content == retained_tool_content
    assert fixed_tool.current_session_todo == todo_snapshot
    assert fixed_tool.current_terminal_watch == watch_snapshot
    assert messages == original_messages


@pytest.mark.asyncio
async def test_checkpoint_uses_provider_baseline_even_when_todo_snapshot_exists(monkeypatch, db_session: AsyncSession):
    captured = {}
    todo_snapshot = {"revision": 16, "todos": [{"content": "verify", "status": "in_progress"}]}
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
    tool_content = "result"
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(id=1, role=MessageRole.USER, content="old"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="answer"),
        InternalMessage(
            id=3,
            role=MessageRole.TOOL,
            tool_call_id="call-1",
            content=tool_content,
            current_session_todo=todo_snapshot,
        ),
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
    assert retained_tool.current_session_todo == todo_snapshot


@pytest.mark.asyncio
async def test_checkpoint_keeps_an_entire_merged_user_batch_after_its_earliest_physical_message(
    monkeypatch,
):
    captured = {}

    async def ensure_summary(_db, **kwargs):
        captured.update(kwargs)
        return ContextSummaryState(
            content=StoredConversationSummary(conversation_summary="旧总结").model_dump_json(exclude_none=True),
            message_id=2,
        )

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
        return ContextSummaryState(
            content=StoredConversationSummary(conversation_summary="combined summary").model_dump_json(exclude_none=True),
            message_id=2,
        )

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
    prior_summary = StoredConversationSummary(conversation_summary="prior summary").model_dump_json(exclude_none=True)
    stale_summary = StoredConversationSummary(conversation_summary="stale summary").model_dump_json(exclude_none=True)
    fresh_summary = StoredConversationSummary(conversation_summary="fresh summary").model_dump_json(exclude_none=True)
    session = ChatSession(
        session_id="session-cas",
        uid="user-1",
        context_summary=prior_summary,
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
            summary=stale_summary,
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
            summary=fresh_summary,
            message_id=12,
        ),
        timeout=1,
    )
    await db_session.refresh(session)

    assert stale_updated is False
    assert retried_updated is True
    assert expected_content_revisions == [7, 8]
    assert session.context_summary == fresh_summary
    stored_summary = StoredConversationSummary.model_validate_json(session.context_summary)
    assert stored_summary.conversation_summary == "fresh summary"
    assert session.context_summary.count("fresh summary") == 1
    assert session.context_summary_message_id == 12
    assert session.context_summary_revision == 6
    assert session.context_content_revision == 8


@pytest.mark.asyncio
async def test_context_summary_lease_loss_after_candidate_generation_never_persists_old_result(db_session: AsyncSession, monkeypatch):
    connection = await db_session.connection()
    await connection.run_sync(SessionTodoPlan.__table__.create, checkfirst=True)
    prior_summary = StoredConversationSummary(conversation_summary="prior summary").model_dump_json(exclude_none=True)
    session = ChatSession(
        session_id="session-lease",
        uid="user-1",
        context_summary=prior_summary,
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
    assert session.context_summary == prior_summary
    stored_summary = StoredConversationSummary.model_validate_json(session.context_summary)
    assert stored_summary.conversation_summary == "prior summary"
    assert session.context_summary_message_id == 8
    assert session.context_summary_revision == 4
    assert session.context_content_revision == 7
