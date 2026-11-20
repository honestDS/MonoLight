from __future__ import annotations

import asyncio
import json
from functools import partial
from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.constants import (
    GOAL_EXECUTION_PHASE_RUNNING,
    TOOL_WORKFLOW_FOREGROUND,
)
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.exceptions import LLMContextLengthException, LLMException
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import service as context_summary_service
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.boundary import ContextSummaryTriggerMode
from app.core.utils.context_summary.common import ContextSummaryState
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.core.utils.dispatcher.frozen_context import compact_frozen_context_messages
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage, ContextSummaryStageStatus
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType, StoredConversationSummary
from app.models.profile import OtherConfig, ProfileConfig, ToolConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.providers.llm.client import LLMClient, estimate_request_context_tokens
from tests.integration.context.context_summary_workflow_support import _context_summary_database as _context_summary_database
from tests.integration.context.context_summary_workflow_support import _seed_summary_history
from tests.memory_recall_support import memory_recall_payload

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")
http_transport_module = import_module("app.transformers.http_transport")


@pytest.mark.asyncio
async def test_persisted_context_summary_is_reused_as_history_boundary(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_summary_history(context_summary_database)

    persisted = await context_summary_service.persist_context_summary(
        session_id="summary-session",
        uid="summary-user",
        expected_message_id=None,
        expected_revision=0,
        expected_content_revision=0,
        summary=StoredConversationSummary(conversation_summary="compressed old turns").model_dump_json(exclude_none=True),
        message_id=2,
    )
    assert persisted is True

    stale_write = await context_summary_service.persist_context_summary(
        session_id="summary-session",
        uid="summary-user",
        expected_message_id=None,
        expected_revision=0,
        expected_content_revision=0,
        summary=StoredConversationSummary(conversation_summary="stale replacement").model_dump_json(exclude_none=True),
        message_id=3,
    )
    assert stale_write is False

    async def build_system_prompt(_db, _profile, **_kwargs):
        return "stable system prompt"

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)

    async with context_summary_database() as db:
        state = await context_summary_service.get_context_summary_state(
            db,
            session_id="summary-session",
            uid="summary-user",
        )
        messages = await prepare_module.prepare_messages(
            db,
            "summary-session",
            "summary-user",
            SimpleNamespace(),
            SimpleNamespace(),
            None,
            "",
            False,
            context_window_k=64,
            max_tokens=512,
        )

    assert state.content is not None
    assert StoredConversationSummary.model_validate_json(state.content).conversation_summary == "compressed old turns"
    assert state.message_id == 2
    assert state.revision == 1
    assert state.content_revision == 0
    assert messages[0].role == MessageRole.SYSTEM
    assert messages[0].content == "stable system prompt"
    assert messages[1].role == MessageRole.USER
    summary_meta = messages[1].conversation_summary
    assert summary_meta is not None
    assert summary_meta.content == "compressed old turns"
    assert summary_meta.through_message_id == 2
    assert summary_meta.covered_user_message_id is None
    assert messages[1].content is None
    assert [message.id for message in messages[2:]] == [3, 4]
    assert [message.content for message in messages[2:]] == ["recent question", "recent answer"]

    async with context_summary_database() as db:
        foreign_state = await context_summary_service.get_context_summary_state(
            db,
            session_id="summary-session",
            uid="other-user",
        )
    assert foreign_state.content is None
    assert foreign_state.message_id is None
    assert foreign_state.revision == 0


@pytest.mark.asyncio
async def test_real_summary_stage_persists_fragment_and_completion(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_summary_history(context_summary_database)
    model = ContextSummaryModelSnapshot(
        channel_id=11,
        channel_name="integration-summary",
        model_id="summary-model",
        protocol="openai",
        base_url="https://summary.invalid",
        api_key="secret",
        priority=1,
        context_window_tokens=65_536,
        max_output_tokens=1024,
        temperature=0.2,
        top_p=None,
        safety_margin_tokens=0,
        input_budget_tokens=64_512,
    )
    model_calls: list[str] = []

    async def call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        model_calls.append(prompt)
        return "compressed integration summary"

    monkeypatch.setattr(context_summary_stage, "call_context_summary_model", call_model)

    async with context_summary_database() as db:
        snapshot = await build_context_summary_snapshot(
            db,
            session_id="summary-session",
            uid="summary-user",
            expected_summary_message_id=None,
            before_id=5,
            target_message_id=2,
            content_revision=0,
        )
        generated = await context_summary_stage.generate_snapshot_summary_with_model(
            db,
            session_id="summary-session",
            uid="summary-user",
            profile=SimpleNamespace(id=1),
            cfg=ProfileConfig.model_validate({}),
            snapshot=snapshot,
            existing_summary=None,
            existing_summary_revision=0,
            safety_margin_tokens=0,
            model=model,
        )

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())

    assert generated.content == "compressed integration summary"
    assert generated.message_count == 2
    assert generated.completed_stage is not None
    assert len(model_calls) == 1
    assert len(stages) == 1
    assert stages[0].status == ContextSummaryStageStatus.COMPLETED
    assert stages[0].expected_fragment_count == 1
    assert stages[0].succeeded_fragment_count == 1
    assert stages[0].persistent_summary_target_id == 2
    assert len(fragments) == 1
    assert fragments[0].fragment_index == 0
    assert fragments[0].message_start_id == 1
    assert fragments[0].message_end_id == 2
    assert fragments[0].content == "compressed integration summary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "covered_user_content",
    [
        pytest.param(None, id="without-covered-user"),
        pytest.param(
            '中文用户原文\n包含“引号”、"双引号"和<xml attr="值">样式文本</xml>',
            id="with-covered-user",
        ),
    ],
)
async def test_frozen_context_compaction_refines_over_budget_single_summary(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    covered_user_content: str | None,
) -> None:
    covered_user_message_id = 1 if covered_user_content is not None else None
    original_summary_text = "entry " * 6500
    original_summary = StoredConversationSummary(
        conversation_summary=original_summary_text,
        user_message=covered_user_content,
        covered_user_message_id=covered_user_message_id,
    ).model_dump_json(exclude_none=True)
    first_summary_response = "entry " * 3200
    first_summary = first_summary_response.strip()
    short_summary = "short summary"
    task_model_id = "task-model"
    task_protocol = "openai"
    task_channel_id = 11
    summary_message = ContextSummaryState(content=original_summary, message_id=1).as_message()
    assert summary_message is not None
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        summary_message,
    ]
    summary_model = ContextSummaryModelSnapshot(
        channel_id=task_channel_id,
        channel_name="integration-summary",
        model_id="summary-model",
        protocol="openai",
        base_url="https://summary.invalid",
        api_key="secret",
        priority=1,
        context_window_tokens=16_000,
        max_output_tokens=6_000,
        temperature=0.2,
        top_p=None,
        safety_margin_tokens=256,
        input_budget_tokens=9_744,
    )
    model_prompts: list[str] = []

    async def select_model(*_args, **_kwargs):
        return summary_model

    async def generate_model(**kwargs):
        prompt = kwargs["messages"][0].content
        assert isinstance(prompt, str)
        model_prompts.append(prompt)
        content = first_summary_response if len(model_prompts) == 1 else short_summary
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
            model=kwargs["model_id"],
            usage={"completion_tokens": 8},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", select_model)
    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    cfg = ProfileConfig.model_validate({})
    profile = SimpleNamespace(id=1)

    async with context_summary_database() as db:
        compacted = await compact_frozen_context_messages(
            db,
            session_id="frozen-task-session",
            uid="frozen-task-user",
            profile=profile,
            cfg=cfg,
            messages=messages,
            context_window_k=4,
            max_tokens=512,
            tools=[],
            model_id=task_model_id,
            protocol=task_protocol,
            channel_id=task_channel_id,
            work_id=123,
        )

    target_tokens = calculate_context_summary_trigger_tokens(
        context_window_k=4,
        max_tokens=512,
        threshold_percent=90,
    )

    def request_tokens(candidate: list[InternalMessage]) -> int:
        return estimate_request_context_tokens(
            materialize_user_environment_prompts(candidate),
            [],
            model_id=task_model_id,
            protocol=task_protocol,
            channel_id=task_channel_id,
        )

    first_summary_content = StoredConversationSummary(
        conversation_summary=first_summary,
        user_message=covered_user_content,
        covered_user_message_id=covered_user_message_id,
    ).model_dump_json(exclude_none=True)
    first_summary_message = ContextSummaryState(content=first_summary_content, message_id=1).as_message()
    assert first_summary_message is not None
    first_candidate = [messages[0], first_summary_message]

    assert target_tokens == 2_908
    compacted_summary_meta = compacted[1].conversation_summary
    assert compacted_summary_meta is not None
    assert compacted_summary_meta.content == short_summary
    assert compacted_summary_meta.through_message_id == 1
    assert compacted_summary_meta.covered_user_message_id == covered_user_message_id
    assert compacted[1].content == covered_user_content
    assert request_tokens(compacted) <= target_tokens
    assert request_tokens(first_candidate) > target_tokens
    assert len(model_prompts) == 2
    conversation_payload = json.loads(model_prompts[0])
    assert conversation_payload["instructions"].startswith("Compress the conversation history")
    assert conversation_payload["conversation_summary"] is None
    conversation_messages = conversation_payload["message_chunks"]
    expected_summary = original_summary_text
    assert conversation_messages == [
        {
            "role": MessageRole.USER.value,
            "user_message": covered_user_content,
            "conversation_summary": expected_summary,
        }
    ]
    assert "message_id" not in conversation_messages[0]
    assert "part" not in conversation_messages[0]
    assert "through_message_id" not in conversation_messages[0]
    assert "covered_user_message_id" not in conversation_messages[0]
    refinement_payload = json.loads(model_prompts[1])
    assert refinement_payload["instructions"].startswith("Further compress the summary below")
    assert refinement_payload["conversation_summary"] == first_summary


@pytest.mark.asyncio
async def test_checkpoint_summary_threshold_uses_persisted_provider_usage_not_local_history_estimate(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_summary_history(context_summary_database)
    generation_calls = []

    async def generate_summary(*_args, **_kwargs):
        generation_calls.append(True)
        return context_summary_stage.GeneratedSummaryResult(
            content="provider-triggered summary",
            message_count=2,
            completed_stage=SimpleNamespace(),
        )

    monkeypatch.setattr(
        context_summary_service,
        "generate_snapshot_summary_result",
        generate_summary,
    )

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        history = list((await db.execute(select(Message).where(Message.session_id == "summary-session").order_by(Message.id))).scalars())
        history[0].content = "historical question"
        history[1].content = "very large historical answer " * 2000
        session.llm_request_metadata = {
            "channel_id": 11,
            "input_tokens": 100,
            "input_tokens_source": "provider",
            "model_id": "gateway-model",
            "protocol": "openai",
            "context_summary_revision": 0,
            "context_content_revision": 0,
        }
        await db.commit()

    request_messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable system prompt"),
        *[
            InternalMessage(
                id=message.id,
                role=message.role,
                content=message.content,
            )
            for message in history
        ],
    ]
    cfg = SimpleNamespace(
        other=OtherConfig(context_summary_threshold_percent=50),
    )

    async with context_summary_database() as db:
        unchanged = await apply_context_summary_checkpoint(
            db,
            session_id="summary-session",
            uid="summary-user",
            profile=SimpleNamespace(id=1),
            cfg=cfg,
            messages=request_messages,
            trigger_mode=context_summary_service.ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=3,
            context_window_k=8,
            max_tokens=512,
            tools=None,
            channel_id=11,
            model_id="gateway-model",
            protocol="openai",
        )

    assert generation_calls == []
    assert [message.id for message in unchanged] == [message.id for message in request_messages]

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        session.llm_request_metadata = {
            **session.llm_request_metadata,
            "input_tokens": 5000,
        }
        await db.commit()

    async with context_summary_database() as db:
        wrong_channel = await apply_context_summary_checkpoint(
            db,
            session_id="summary-session",
            uid="summary-user",
            profile=SimpleNamespace(id=1),
            cfg=cfg,
            messages=request_messages,
            trigger_mode=context_summary_service.ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=3,
            context_window_k=8,
            max_tokens=512,
            tools=None,
            channel_id=12,
            model_id="gateway-model",
            protocol="openai",
        )

    assert generation_calls == []
    assert [message.id for message in wrong_channel] == [message.id for message in request_messages]

    async with context_summary_database() as db:
        compacted = await apply_context_summary_checkpoint(
            db,
            session_id="summary-session",
            uid="summary-user",
            profile=SimpleNamespace(id=1),
            cfg=cfg,
            messages=request_messages,
            trigger_mode=context_summary_service.ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=3,
            context_window_k=8,
            max_tokens=512,
            tools=None,
            channel_id=11,
            model_id="gateway-model",
            protocol="openai",
        )

    assert generation_calls == [True]
    compacted_summary_meta = compacted[1].conversation_summary
    assert compacted_summary_meta is not None
    assert compacted_summary_meta.content == "provider-triggered summary"
    assert compacted_summary_meta.through_message_id == 2
    assert compacted_summary_meta.covered_user_message_id == 1
    assert compacted[1].content == "historical question"
    assert [message.id for message in compacted[2:]] == [3, 4]

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        assert session.context_summary is not None
        stored_summary = StoredConversationSummary.model_validate_json(session.context_summary)
        assert stored_summary.conversation_summary == "provider-triggered summary"
        assert stored_summary.user_message == "historical question"
        assert stored_summary.covered_user_message_id == 1
        assert session.context_summary_message_id == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "failure", "cancel"], ids=["success", "failure", "cancel"])
async def test_context_summary_lifecycle_persists_revision_in_stream_events(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    await _seed_summary_history(context_summary_database)
    async with context_summary_database() as db:
        history = list((await db.execute(select(Message).where(Message.session_id == "summary-session").order_by(Message.id))).scalars())
        history[1].content = "old answer " * 80
        work = SessionReplyWorkItem(
            id=1,
            uid="summary-user",
            session_id="summary-session",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="1",
            dedupe_key="summary-lifecycle-work",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="summary-worker",
            lock_until=9_999_999_999,
            attempt_count=1,
            max_attempts=2,
            available_at=0,
        )
        db.add(work)
        await db.commit()

    stream_state = executor_interactive_module._InteractiveWorkStreamEventState(
        work=work,
        worker_id="summary-worker",
        next_sequence=1,
        dequeued_request_ids=set(),
        turn_end_content_by_response_id={},
        tool_names_by_response_id={},
    )
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", context_summary_database)
    lifecycle_callback = partial(executor_interactive_module._publish_interactive_work_stream_event, stream_state)

    async def generate_summary(*_args, **_kwargs):
        if mode == "success":
            return context_summary_stage.GeneratedSummaryResult(
                content="short summary",
                message_count=2,
                completed_stage=SimpleNamespace(),
            )
        if mode == "failure":
            raise LLMException(message="summary model unavailable")
        raise asyncio.CancelledError()

    monkeypatch.setattr(context_summary_service, "generate_snapshot_summary_result", generate_summary)

    request_messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable system prompt"),
        *[
            InternalMessage(
                id=message.id,
                role=message.role,
                content=message.content,
            )
            for message in history
        ],
    ]
    cfg = SimpleNamespace(
        other=SimpleNamespace(context_summary_threshold_percent=50),
    )

    async def run_summary() -> ContextSummaryState:
        async with context_summary_database() as db:
            return await context_summary_service.ensure_context_summary(
                db,
                session_id="summary-session",
                uid="summary-user",
                profile=SimpleNamespace(id=1),
                cfg=cfg,
                before_id=3,
                current_message="",
                context_window_k=8,
                max_tokens=512,
                reserved_tokens=0,
                tools=None,
                fixed_request_messages=[request_messages[0], *request_messages[3:]],
                summary_source_messages=request_messages,
                trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
                fixed_upper_message_id=3,
                channel_id=11,
                model_id="gateway-model",
                protocol="openai",
                lifecycle_event_callback=lifecycle_callback,
                force=True,
            )

    if mode == "success":
        summary_state = await run_summary()
        assert summary_state.content is not None
        assert StoredConversationSummary.model_validate_json(summary_state.content).conversation_summary == "short summary"
        assert summary_state.message_id == 2
        assert summary_state.revision == 1
        assert summary_state.content_revision == 0
    elif mode == "failure":
        with pytest.raises(LLMException):
            await run_summary()
    else:
        with pytest.raises(asyncio.CancelledError):
            await run_summary()

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        restored_work = await db.get(SessionReplyWorkItem, 1)
        stream_events = await session_reply_stream_event_crud.list_after_sequence(
            db,
            work_id=1,
            after_sequence_no=0,
        )

    assert restored_work is not None
    assert (restored_work.uid, restored_work.session_id, restored_work.profile_id, restored_work.sequence_no) == (
        "summary-user",
        "summary-session",
        1,
        1,
    )
    assert (restored_work.status, restored_work.locked_by, restored_work.lock_until) == (
        SessionReplyWorkStatus.RUNNING,
        "summary-worker",
        9_999_999_999,
    )
    assert [event.sequence_no for event in stream_events] == [1, 2]
    assert [event.event["type"] for event in stream_events] == ["context_summary_start", "context_summary_end"]
    end_event = stream_events[-1]
    assert end_event.work_id == 1
    assert end_event.event["session_id"] == "summary-session"
    assert end_event.event["work_id"] == 1
    assert end_event.event["work_sequence_no"] == 1
    assert end_event.event["event_sequence_no"] == 2

    if mode == "success":
        assert session.context_summary is not None
        assert StoredConversationSummary.model_validate_json(session.context_summary).conversation_summary == "short summary"
        assert session.context_summary_message_id == 2
        assert session.context_summary_revision == 1
        assert session.context_content_revision == 0
        assert end_event.event["context_summary_revision"] == 1
        assert end_event.event["context_content_revision"] == 0
    else:
        assert session.context_summary is None
        assert session.context_summary_message_id is None
        assert session.context_summary_revision == 0
        assert session.context_content_revision == 0
        assert "context_summary_revision" not in end_event.event
        assert "context_content_revision" not in end_event.event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_summary_failure", "provider_keeps_overflowing"),
    [(False, False), (True, False), (False, True)],
    ids=["summary-succeeds", "user-retry-after-summary-failure", "summary-succeeds-provider-still-overflows"],
)
async def test_provider_overflow_persists_summary_before_same_channel_retry(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    first_summary_failure: bool,
    provider_keeps_overflowing: bool,
) -> None:
    items_tail_marker = "RECALL_ITEMS_TAIL_MARKER_9C15D2"
    items_body = ("unified recall item with archived engineering details. " * 120) + items_tail_marker
    managed_tail_marker = "MANAGED_KNOWLEDGE_MAINTENANCE_MARKER_7F3A91"
    managed_body = ("managed knowledge recall record with durable facts. " * 120) + managed_tail_marker
    user_tail_marker = "USER_KNOWLEDGE_TAIL_MARKER_4B82E6"
    user_body = ("user knowledge recall record with personal preferences. " * 120) + user_tail_marker
    chat_history_tail_marker = "CHAT_HISTORY_TAIL_MARKER_6D03AF"
    chat_history_body = ("recalled chat history record with prior decisions. " * 120) + chat_history_tail_marker
    assert all(len(body) > 4000 for body in (items_body, managed_body, user_body, chat_history_body))
    tool_call_id = "managed-recall-call-1"
    managed_tool_call = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id=tool_call_id,
                name="manage_memory_and_knowledge",
                arguments={"operation": "recall", "query": "durable project facts"},
            )
        ],
    )
    recall_payload = memory_recall_payload(current_session_id="recovery-session")
    recall_payload["memories"][0]["content"] = items_body
    recall_payload["knowledge_base"][0]["content"] = managed_body
    recall_payload["knowledge_base"][1]["content"] = user_body
    recall_payload["chat_history"] = recall_payload["chat_history"][:1]
    recall_payload["chat_history"][0]["content"] = chat_history_body
    managed_tool_result = InternalMessage(
        role=MessageRole.TOOL,
        content=json.dumps(recall_payload, ensure_ascii=False),
        tool_call_id=tool_call_id,
        tool_execution_status="succeeded",
    )

    async with context_summary_database() as db:
        db.add(ChatSession(session_id="recovery-session", uid="recovery-user"))
        db.add_all(
            [
                Message(id=1, session_id="recovery-session", uid="recovery-user", profile_id=1, role=MessageRole.USER, content="old question", is_processed=True),
                Message(id=2, session_id="recovery-session", uid="recovery-user", profile_id=1, role=MessageRole.ASSISTANT, content="old answer"),
                Message(id=3, session_id="recovery-session", uid="recovery-user", profile_id=1, role=MessageRole.USER, content="current question", is_processed=True),
                Message(
                    id=4,
                    session_id="recovery-session",
                    uid="recovery-user",
                    profile_id=1,
                    role=MessageRole.ASSISTANT,
                    type=MessageType.TOOL_CALL,
                    content=managed_tool_call.model_dump_json(),
                ),
                Message(
                    id=5,
                    session_id="recovery-session",
                    uid="recovery-user",
                    profile_id=1,
                    role=MessageRole.TOOL,
                    type=MessageType.TOOL_RESULT,
                    content=managed_tool_result.model_dump_json(),
                ),
            ]
        )
        await db.commit()

    summary_calls = 0

    async def generate_summary(*_args, **_kwargs):
        nonlocal summary_calls
        summary_calls += 1
        if first_summary_failure and summary_calls == 1:
            raise LLMException("summary model unavailable")
        return context_summary_stage.GeneratedSummaryResult(
            content="overflow recovery summary",
            message_count=4,
            completed_stage=SimpleNamespace(content="overflow recovery summary", refinement_index=0),
        )

    monkeypatch.setattr(
        context_summary_service,
        "generate_snapshot_summary_result",
        generate_summary,
    )

    class Channel:
        id = 11
        name = "recovery-channel"
        base_url = "https://example.invalid"

        @staticmethod
        def get_decrypted_api_key():
            return "secret"

    class Logger:
        def bind(self, **_kwargs):
            return self

        def warning(self, *_args, **_kwargs):
            return None

    attempts: list[list[InternalMessage]] = []

    async def generate_response(**kwargs):
        attempts.append([message.model_copy(deep=True) for message in kwargs["messages"]])
        if provider_keeps_overflowing or len(attempts) == 1 or (first_summary_failure and len(attempts) == 2):
            raise LLMContextLengthException(provider_message="maximum context length exceeded")
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="recovered"),
            model=kwargs["model_id"],
            usage={"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 20},
        )

    fallback_calls = 0

    async def unexpected_channel_fallback(*_args, **_kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        if first_summary_failure or provider_keeps_overflowing:
            return None
        raise AssertionError("same-channel summary recovery must run before channel fallback")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate_response)
    monkeypatch.setattr(interactive_generation_module, "select_channel", unexpected_channel_fallback)

    async def build_state(db):
        messages = await prepare_module.prepare_messages(
            db,
            "recovery-session",
            "recovery-user",
            SimpleNamespace(id=1),
            SimpleNamespace(other=OtherConfig(context_summary_threshold_percent=90)),
            None,
            "",
            False,
            context_window_k=64,
            max_tokens=512,
        )
        return SimpleNamespace(
            db=db,
            uid="recovery-user",
            session_id="recovery-session",
            profile=SimpleNamespace(id=1),
            cfg=SimpleNamespace(tool=ToolConfig(), other=OtherConfig(context_summary_threshold_percent=90)),
            messages=messages,
            checkpoint_state=SimpleNamespace(
                upper_message_id=3,
                total_output_tokens=0,
                session_total_input_tokens=0,
                session_total_cached_tokens=0,
                session_total_output_tokens=0,
                execution_phase=GOAL_EXECUTION_PHASE_RUNNING,
            ),
            tool_workflow=TOOL_WORKFLOW_FOREGROUND,
            context_summary_work_validity_checker=None,
            context_summary_lifecycle_callback=None,
            context_frozen=False,
            chat_params={
                "temperature": None,
                "top_p": None,
                "reasoning_effort": None,
                "max_tokens": 512,
                "chat_timeout": 60,
                "context_window_k": 64,
            },
            model_entry={"model_id": "gateway-model", "usage": "CHAT", "protocol": "OPENAI"},
            chat_channel=object(),
            chat_cursor_key="1:CHAT",
            chat_channel_obj=Channel(),
            channel_rule=SimpleNamespace(priority=1),
            latest_llm_request_metadata=None,
            current_turn=1,
            goal_mode=False,
            stream_event_callback=None,
            stream_llm_request=None,
            request_metadata_callback=None,
            expose_tool_call_content=True,
            show_tool_calls=True,
            dispatcher_mode="non_stream",
            dispatch_logger=Logger(),
            img_understanding=False,
            audio_understanding=False,
            video_understanding=False,
        )

    async def build_system_prompt(_db, _profile, **_kwargs):
        return "stable system prompt"

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)

    def assert_recall_payload(content: str | None) -> None:
        assert content is not None
        payload = json.loads(content)["tool_result"]
        assert [item["content"] for item in payload["memories"]] == [items_body]
        assert "items" not in payload
        assert payload["current_session_id"] == "recovery-session"
        assert [(item["source_type"], item["content"]) for item in payload["knowledge_base"]] == [("managed_knowledge", managed_body), ("user_knowledge", user_body)]
        assert [item["content"] for item in payload["chat_history"]] == [chat_history_body]

    def assert_tool_context(requests: list[list[InternalMessage]]) -> None:
        assert requests
        for request in requests:
            tool_call_message = next(message for message in request if message.tool_calls and any(call.id == tool_call_id for call in message.tool_calls))
            tool_result_message = next(message for message in request if message.tool_call_id == tool_call_id)
            tool_call = next(call for call in tool_call_message.tool_calls if call.id == tool_call_id)
            assert tool_call.name == "manage_memory_and_knowledge"
            assert tool_call.arguments["operation"] == "recall"
            assert_recall_payload(tool_result_message.content)

    def assert_summary_context(request: list[InternalMessage]) -> None:
        summary_payloads: list[dict] = []
        for message in request:
            if message.role != MessageRole.USER or not isinstance(message.content, str):
                continue
            try:
                materialized = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if isinstance(materialized, dict) and materialized.get("conversation_summary") == "overflow recovery summary":
                summary_payloads.append(materialized)
        assert len(summary_payloads) == 1
        summary_payload = summary_payloads[0]
        assert summary_payload["conversation_summary"] == "overflow recovery summary"
        assert summary_payload["user_message"] == "current question"

    result = None
    state = None
    if first_summary_failure:
        async with context_summary_database() as db:
            state = await build_state(db)
            with pytest.raises(LLMContextLengthException):
                await interactive_generation_module.generate_interactive_turn(
                    state,
                    current_tools=[],
                    response_id="response-1",
                )

        assert fallback_calls == 1
        assert summary_calls == 1
        assert len(attempts) == 1
        assert_tool_context(attempts)

        async with context_summary_database() as db:
            session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "recovery-session"))).scalar_one()
            original_messages = list((await db.execute(select(Message).where(Message.session_id == "recovery-session").order_by(Message.id))).scalars())

        assert session.context_summary is None
        assert session.context_summary_message_id is None
        assert [message.id for message in original_messages] == [1, 2, 3, 4, 5]
        assert original_messages[3].role == MessageRole.ASSISTANT
        assert original_messages[3].type == MessageType.TOOL_CALL
        assert original_messages[4].role == MessageRole.TOOL
        assert original_messages[4].type == MessageType.TOOL_RESULT
        assert original_messages[3].content == managed_tool_call.model_dump_json()
        assert original_messages[4].content == managed_tool_result.model_dump_json()

        async with context_summary_database() as db:
            state = await build_state(db)
            result = await interactive_generation_module.generate_interactive_turn(
                state,
                current_tools=[],
                response_id="response-2",
            )
            session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "recovery-session"))).scalar_one()
    elif provider_keeps_overflowing:
        async with context_summary_database() as db:
            state = await build_state(db)
            with pytest.raises(LLMContextLengthException):
                await interactive_generation_module.generate_interactive_turn(
                    state,
                    current_tools=[],
                    response_id="response-1",
                )
            session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "recovery-session"))).scalar_one()
    else:
        async with context_summary_database() as db:
            state = await build_state(db)
            result = await interactive_generation_module.generate_interactive_turn(
                state,
                current_tools=[],
                response_id="response-1",
            )
            session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "recovery-session"))).scalar_one()

    if provider_keeps_overflowing:
        assert result is None
        assert len(attempts) == 2
        assert summary_calls == 1
        assert fallback_calls == 1
    else:
        assert result.message.content == "recovered"
        assert len(attempts) == (3 if first_summary_failure else 2)
        assert summary_calls == (2 if first_summary_failure else 1)
        assert fallback_calls == (1 if first_summary_failure else 0)
    assert_tool_context(attempts[:-1])
    assert_summary_context(attempts[-1])
    assert all(not any("overflow recovery summary" in (message.content or "") for message in request) for request in attempts[:-1])
    assert any("overflow recovery summary" in (message.content or "") for message in attempts[-1])
    assert state is not None
    assert state.checkpoint_state.upper_message_id == 3
    state_summary_meta = state.messages[1].conversation_summary
    assert state_summary_meta is not None
    assert state_summary_meta.content == "overflow recovery summary"
    assert state_summary_meta.through_message_id == 5
    assert state_summary_meta.covered_user_message_id == 3
    assert state.messages[1].content == "current question"
    assert [message.id for message in state.messages[2:]] == []
    assert session.context_summary is not None
    stored_summary = StoredConversationSummary.model_validate_json(session.context_summary)
    assert stored_summary.conversation_summary == "overflow recovery summary"
    assert stored_summary.user_message == "current question"
    assert stored_summary.covered_user_message_id == 3
    assert session.context_summary_message_id == 5

    async with context_summary_database() as db:
        persisted_messages = list((await db.execute(select(Message).where(Message.session_id == "recovery-session").order_by(Message.id))).scalars())
    assert [message.id for message in persisted_messages] == [1, 2, 3, 4, 5]
    assert [message.content for message in persisted_messages] == [
        "old question",
        "old answer",
        "current question",
        managed_tool_call.model_dump_json(),
        managed_tool_result.model_dump_json(),
    ]
    assert persisted_messages[3].type == MessageType.TOOL_CALL
    assert persisted_messages[4].type == MessageType.TOOL_RESULT
    persisted_tool_call = InternalMessage.model_validate_json(persisted_messages[3].content)
    persisted_tool_result = InternalMessage.model_validate_json(persisted_messages[4].content)
    assert persisted_tool_call.tool_calls[0].id == persisted_tool_result.tool_call_id == tool_call_id
    assert persisted_tool_call.tool_calls[0].arguments["operation"] == "recall"
    assert_recall_payload(materialize_user_environment_prompts([persisted_tool_result])[0].content)
