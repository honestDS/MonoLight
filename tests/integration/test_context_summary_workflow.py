from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.core.constants import (
    CONTEXT_SUMMARY_MIN_REFINEMENT_REDUCTION_TOKENS,
    ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED,
    ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED,
    FROZEN_CONTEXT_SUMMARY_WORK_KEY_PREFIX,
    GOAL_EXECUTION_PHASE_RUNNING,
    TOOL_WORKFLOW_FOREGROUND,
)
from app.core.crud.context_summary.fragment import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
)
from app.core.crud.context_summary.stage import context_summary_stage_crud
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.exceptions import LLMContextLengthException, LLMException, LLMRequestRejectedException
from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.core.utils.context_summary import cleanup as context_summary_cleanup
from app.core.utils.context_summary import merge as context_summary_merge
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import service as context_summary_service
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.boundary import ContextSummaryTriggerMode
from app.core.utils.context_summary.common import ContextSummaryOutputBudgetError, ContextSummaryState
from app.core.utils.context_summary.model_call import call_context_summary_model, estimate_context_summary_prompt_tokens
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot, select_context_summary_model
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.core.utils.context_summary.user_message_block import append_covered_user_message, split_covered_user_message
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.core.utils.dispatcher.frozen_context import compact_frozen_context_messages
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.dispatcher.session_todo_snapshot import parse_session_todo_snapshot
from app.core.utils.llm_request_policy import build_upstream_rejection, serialize_llm_json_request
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage, ContextSummaryStageStatus
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import ProfileConfig, ToolConfig
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan
from app.providers.llm.client import LLMClient, estimate_request_context_tokens
from app.transformers.openai.base import BaseOpenAITransformer
from tests.database_support import clone_sqlite_schema
from tests.integration.context_summary_test_support import make_fragment, make_stage
from tests.memory_recall_support import memory_recall_payload

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")
channel_router_module = import_module("app.core.channel.router")
http_transport_module = import_module("app.transformers.http_transport")


@pytest_asyncio.fixture
async def context_summary_database(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "context-summary-workflow.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await clone_sqlite_schema(
        database_path,
        tables=[
            ChatSession.__table__,
            Message.__table__,
            ContextSummaryStage.__table__,
            ContextSummaryFragment.__table__,
            SessionTodoPlan.__table__,
        ],
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(context_summary_service, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(context_summary_stage, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(context_summary_reduction, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(context_summary_merge, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(context_summary_cleanup, "AsyncSessionLocal", session_factory)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _seed_summary_history(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as db:
        db.add(ChatSession(session_id="summary-session", uid="summary-user"))
        db.add_all(
            [
                Message(id=1, session_id="summary-session", uid="summary-user", profile_id=1, role=MessageRole.USER, content="old question"),
                Message(id=2, session_id="summary-session", uid="summary-user", profile_id=1, role=MessageRole.ASSISTANT, content="old answer"),
                Message(id=3, session_id="summary-session", uid="summary-user", profile_id=1, role=MessageRole.USER, content="recent question"),
                Message(id=4, session_id="summary-session", uid="summary-user", profile_id=1, role=MessageRole.ASSISTANT, content="recent answer"),
            ]
        )
        await db.commit()


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
        summary="compressed old turns",
        message_id=2,
    )
    assert persisted is True

    stale_write = await context_summary_service.persist_context_summary(
        session_id="summary-session",
        uid="summary-user",
        expected_message_id=None,
        expected_revision=0,
        expected_content_revision=0,
        summary="stale replacement",
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

    assert state.content == "compressed old turns"
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
            cfg=SimpleNamespace(channel=SimpleNamespace(context_summary_channel=object())),
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
    original_summary = append_covered_user_message(
        original_summary_text,
        message_id=covered_user_message_id,
        content=covered_user_content,
    )
    assert original_summary is not None
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

    first_summary_content = append_covered_user_message(
        first_summary,
        message_id=covered_user_message_id,
        content=covered_user_content,
    )
    assert first_summary_content is not None
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
    conversation_section = model_prompts[0].split("Conversation segment to compress:\n", 1)[1]
    conversation_section = conversation_section.split("\n\nReturn only the updated summary using the output template.", 1)[0]
    conversation_messages = [json.loads(line) for line in conversation_section.splitlines() if line]
    expected_summary = original_summary_text
    if covered_user_content is not None:
        expected_summary = original_summary_text.rstrip()
    assert conversation_messages == [
        {
            "role": MessageRole.USER.value,
            "user_message": covered_user_content,
            "conversation_summary": expected_summary,
        }
    ]
    assert "through_message_id" not in conversation_section
    assert "covered_user_message_id" not in conversation_section
    assert model_prompts[1].startswith("Further compress the summary below")
    assert first_summary in model_prompts[1]


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
        history[0].content = "very large historical question " * 2000
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
        other=SimpleNamespace(context_summary_threshold_percent=50),
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
    assert compacted_summary_meta.covered_user_message_id is None
    assert compacted[1].content is None
    assert [message.id for message in compacted[2:]] == [3, 4]

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        assert session.context_summary == "provider-triggered summary"
        assert session.context_summary_message_id == 2


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
            SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=90)),
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
            cfg=SimpleNamespace(tool=ToolConfig(), other=SimpleNamespace(context_summary_threshold_percent=90)),
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
        payload = json.loads(content)
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
    model_summary, covered = split_covered_user_message(session.context_summary)
    assert model_summary == "overflow recovery summary"
    assert covered is not None
    assert covered.message_id == 3
    assert covered.content == "current question"
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
    assert_recall_payload(persisted_tool_result.content)


def _summary_rejection_models() -> dict[int, ContextSummaryModelSnapshot]:
    primary = ContextSummaryModelSnapshot(
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
    return {
        1: primary,
        2: replace(
            primary,
            channel_id=12,
            channel_name="integration-summary-fallback",
            model_id="summary-model-fallback",
            priority=2,
        ),
    }


def _raise_summary_request_rejection(
    kind: str,
    *,
    model: ContextSummaryModelSnapshot,
    prompt: str,
) -> None:
    if kind == "local":
        payload = {
            "model": model.model_id,
            "messages": [{"role": "user", "content": prompt}],
        }
        serialize_llm_json_request(payload, max_size_bytes=0)
        raise AssertionError("serialize_llm_json_request should have rejected the request")

    rejection = build_upstream_rejection(status=413, detail="<html>private</html>")
    assert rejection is not None
    raise rejection


@pytest.mark.asyncio
async def test_context_summary_local_request_rejection_invalidates_stage_without_fallback(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_summary_history(context_summary_database)
    models = _summary_rejection_models()
    selected_priorities: list[set[int]] = []
    model_calls: list[ContextSummaryModelSnapshot] = []

    async def select_model(*_args, excluded_priorities=None, **_kwargs):
        excluded = set(excluded_priorities or ())
        selected_priorities.append(excluded)
        return next((models[priority] for priority in (1, 2) if priority not in excluded), None)

    async def call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        model_calls.append(model)
        _raise_summary_request_rejection("local", model=model, prompt=prompt)

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", select_model)
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
        with pytest.raises(LLMRequestRejectedException) as exc_info:
            await context_summary_stage.generate_snapshot_summary_result(
                db,
                session_id="summary-session",
                uid="summary-user",
                profile=SimpleNamespace(id=1),
                cfg=SimpleNamespace(channel=SimpleNamespace(context_summary_channel=object())),
                snapshot=snapshot,
                existing_summary=None,
                existing_summary_revision=0,
                safety_margin_tokens=0,
            )

    rejection = exc_info.value
    assert rejection.source == "local"
    assert rejection.allow_channel_fallback is False
    assert len(model_calls) == 1
    assert [model.priority for model in model_calls] == [1]
    assert selected_priorities == [set()]

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()

    assert len(stages) == 1
    assert stages[0].status == ContextSummaryStageStatus.INVALIDATED
    assert stages[0].succeeded_fragment_count == 0
    assert fragments == []
    assert session.context_summary is None
    assert session.context_summary_message_id is None
    assert session.context_summary_revision == 0
    assert session.context_content_revision == 0


@pytest.mark.asyncio
async def test_context_summary_upstream_request_rejection_exhausts_channels(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_summary_history(context_summary_database)
    models = _summary_rejection_models()
    selected_priorities: list[set[int]] = []
    model_calls: list[ContextSummaryModelSnapshot] = []

    async def select_model(*_args, excluded_priorities=None, **_kwargs):
        excluded = set(excluded_priorities or ())
        selected_priorities.append(excluded)
        return next((models[priority] for priority in (1, 2) if priority not in excluded), None)

    async def call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        model_calls.append(model)
        _raise_summary_request_rejection("upstream", model=model, prompt=prompt)

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", select_model)
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
        with pytest.raises(LLMRequestRejectedException) as exc_info:
            await context_summary_stage.generate_snapshot_summary_result(
                db,
                session_id="summary-session",
                uid="summary-user",
                profile=SimpleNamespace(id=1),
                cfg=SimpleNamespace(channel=SimpleNamespace(context_summary_channel=object())),
                snapshot=snapshot,
                existing_summary=None,
                existing_summary_revision=0,
                safety_margin_tokens=0,
            )

    rejection = exc_info.value
    friendly_message = rejection.render_message()
    assert rejection.source == "upstream"
    assert rejection.upstream_status == 413
    assert rejection.allow_channel_fallback is True
    assert "413" in friendly_message
    assert "<html>private</html>" not in friendly_message
    assert len(model_calls) == 2
    assert [model.priority for model in model_calls] == [1, 2]
    assert selected_priorities == [set(), {1}, {1, 2}]

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()

    assert len(stages) == 2
    assert all(stage.status == ContextSummaryStageStatus.INVALIDATED for stage in stages)
    assert all(stage.succeeded_fragment_count == 0 for stage in stages)
    assert fragments == []
    assert session.context_summary is None
    assert session.context_summary_message_id is None
    assert session.context_summary_revision == 0
    assert session.context_content_revision == 0


@pytest.mark.asyncio
async def test_context_summary_upstream_rejection_falls_back_to_completed_summary_stage(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_summary_history(context_summary_database)
    models = _summary_rejection_models()
    selected_priorities: list[set[int]] = []
    model_calls: list[ContextSummaryModelSnapshot] = []

    async def select_model(*_args, excluded_priorities=None, **_kwargs):
        excluded = set(excluded_priorities or ())
        selected_priorities.append(excluded)
        return next((models[priority] for priority in (1, 2) if priority not in excluded), None)

    async def call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        model_calls.append(model)
        if model.priority == 1:
            _raise_summary_request_rejection("upstream", model=model, prompt=prompt)
        return "short summary"

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", select_model)
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
        generated = await context_summary_stage.generate_snapshot_summary_result(
            db,
            session_id="summary-session",
            uid="summary-user",
            profile=SimpleNamespace(id=1),
            cfg=SimpleNamespace(channel=SimpleNamespace(context_summary_channel=object())),
            snapshot=snapshot,
            existing_summary=None,
            existing_summary_revision=0,
            safety_margin_tokens=0,
        )

    assert generated.content == "short summary"
    assert generated.completed_stage is not None
    assert len(model_calls) == 2
    assert [model.priority for model in model_calls] == [1, 2]
    assert selected_priorities == [set(), {1}]

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())

    assert len(stages) == 2
    assert sorted(stage.status for stage in stages) == [
        ContextSummaryStageStatus.COMPLETED,
        ContextSummaryStageStatus.INVALIDATED,
    ]
    assert len(fragments) == 1
    assert fragments[0].content == "short summary"
    assert fragments[0].status.value == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES", "DEEPSEEK", "DEEPSEEK_RESPONSES"])
@pytest.mark.parametrize("reasoning_effort", ["low", "none", None], ids=["low", "none", "unset"])
async def test_context_summary_rule_budget_and_reasoning_reach_upstream_request(
    protocol: str,
    reasoning_effort: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": 99,
                            "model_id": "chat-model",
                            "reasoning_effort": "high",
                            "priority": 1,
                            "weight": 1,
                        }
                    ]
                },
                "context_summary_channel": {
                    "rules": [
                        {
                            "channel_id": 11,
                            "model_id": "summary-model",
                            "reasoning_effort": reasoning_effort,
                            "priority": 1,
                            "weight": 1,
                        }
                    ]
                },
            },
            "security": {},
            "tool": {},
            "other": {},
        }
    )

    class SummaryChannel:
        id = 11
        name = "integration-summary"
        usage_key = "0123456789abcdef0123456789abcdef"
        is_active = True
        model_ids = [
            {
                "model_id": "summary-model",
                "usage": "CHAT",
                "protocol": protocol,
                "context_window_k": 64,
                "max_tokens": 20_480,
                "temperature": 0.8,
                "reasoning_effort": None,
            }
        ]
        base_url = "https://summary.invalid/v1"

        @staticmethod
        def get_decrypted_api_key() -> str:
            return "secret"

    channel = SummaryChannel()
    channel_lookups: list[int] = []

    async def get_channel(_db, channel_id: int):
        channel_lookups.append(channel_id)
        return channel if channel_id == 11 else None

    async def next_channel_index(_cursor_key: str, _length: int) -> int:
        return 0

    upstream_calls: list[tuple[str, dict]] = []

    async def capture_upstream_json(*_args, **kwargs):
        upstream_calls.append((kwargs["url"], kwargs["payload"]))
        if protocol in {"OPENAI", "DEEPSEEK"}:
            return {
                "id": "chatcmpl-summary",
                "model": "summary-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "complete summary"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        return {
            "id": "resp-summary",
            "status": "completed",
            "model": "summary-model",
            "output": [
                {
                    "id": "msg-summary",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "complete summary", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setattr(channel_router_module.channel_crud, "get", get_channel)
    monkeypatch.setattr(channel_router_module.channel_cursor_crud, "next_index", next_channel_index)
    monkeypatch.setattr(BaseOpenAITransformer, "_post_json", capture_upstream_json)
    monkeypatch.setattr(http_transport_module, "post_json", capture_upstream_json)

    model = await select_context_summary_model(
        None,
        profile_id=7,
        channel_config=profile.channel.context_summary_channel,
        safety_margin_tokens=256,
    )
    assert model is not None
    assert model.channel_id == 11
    assert model.model_id == "summary-model"
    assert model.reasoning_effort == reasoning_effort
    assert model.context_window_tokens == 64_000
    assert model.max_output_tokens == 20_480
    assert model.input_budget_tokens == 64_000 - 20_480 - 256

    summary = await call_context_summary_model(model=model, prompt="Summarize the conversation")

    assert summary == "complete summary"
    assert profile.channel.chat_channel.rules[0].reasoning_effort == "high"
    assert channel_lookups == [11]
    assert len(upstream_calls) == 1

    url, payload = upstream_calls[0]
    is_chat_protocol = protocol in {"OPENAI", "DEEPSEEK"}
    assert url.endswith("/chat/completions" if is_chat_protocol else "/responses")
    assert payload["model"] == "summary-model"
    if protocol in {"OPENAI", "OPENAI_RESPONSES"} and reasoning_effort is not None:
        assert "temperature" not in payload
    else:
        assert payload["temperature"] == 0.8
    if is_chat_protocol:
        assert payload["max_tokens"] == 20_480
        assert "max_output_tokens" not in payload
    else:
        assert payload["max_output_tokens"] == 20_480
        assert "max_tokens" not in payload

    if protocol == "OPENAI":
        assert "thinking" not in payload
        assert "reasoning" not in payload
        if reasoning_effort is None:
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == reasoning_effort
    elif protocol == "OPENAI_RESPONSES":
        assert "thinking" not in payload
        assert "reasoning_effort" not in payload
        if reasoning_effort is None:
            assert "reasoning" not in payload
        else:
            expected_reasoning = {"effort": reasoning_effort}
            if reasoning_effort == "low":
                expected_reasoning["summary"] = "auto"
            assert payload["reasoning"] == expected_reasoning
    elif protocol == "DEEPSEEK":
        assert "reasoning" not in payload
        if reasoning_effort == "low":
            assert payload["thinking"] == {"type": "enabled"}
            assert payload["reasoning_effort"] == "low"
        elif reasoning_effort == "none":
            assert payload["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in payload
        else:
            assert "thinking" not in payload
            assert "reasoning_effort" not in payload
    else:
        assert "thinking" not in payload
        assert "reasoning_effort" not in payload
        if reasoning_effort is None:
            assert "reasoning" not in payload
        else:
            assert payload["reasoning"] == {"effort": reasoning_effort}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["fragment", "refinement"])
@pytest.mark.parametrize("failed_content", [None, "partial summary"], ids=["empty-content", "partial-content"])
@pytest.mark.parametrize("fallback_succeeds", [False, True])
async def test_context_summary_output_budget_failure_preserves_saved_summary_and_cleans_work(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    failed_content: str | None,
    fallback_succeeds: bool,
) -> None:
    await _seed_summary_history(context_summary_database)
    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        session.context_summary = "stable saved summary"
        session.context_summary_message_id = 2
        session.context_summary_revision = 3
        session.context_content_revision = 7
        history = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == "summary-session",
                        Message.id.in_([3, 4]),
                    )
                )
            ).scalars()
        )
        for message in history:
            message.content = "history evidence " * 2000
        db.add(
            Message(
                id=5,
                session_id="summary-session",
                uid="summary-user",
                profile_id=1,
                role=MessageRole.USER,
                content="current question",
            )
        )
        await db.commit()

    models = {
        priority: replace(
            model,
            max_output_tokens=20_480,
            input_budget_tokens=65_536 - 20_480,
            reasoning_effort="low",
        )
        for priority, model in _summary_rejection_models().items()
    }
    model_calls: list[tuple[str, str, str]] = []

    async def select_model(*_args, excluded_priorities=None, **_kwargs):
        excluded = set(excluded_priorities or ())
        return next((models[priority] for priority in (1, 2) if priority not in excluded), None)

    async def generate_model(*_args, **kwargs):
        model_id = kwargs["model_id"]
        prompt = kwargs["messages"][0].content
        is_refinement = prompt.startswith("Further compress the summary below")
        phase = "refinement" if is_refinement else "fragment"
        model_calls.append((model_id, phase, prompt))
        is_failure_phase = phase == failure_phase
        is_primary = model_id == "summary-model"

        if is_failure_phase and (is_primary or not fallback_succeeds):
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content=failed_content,
                    reasoning_content="non-sensitive test reasoning",
                ),
                model=model_id,
                usage={"completion_tokens": 20_480},
                finish_reason="length",
            )

        content = "fact " * 2000 if failure_phase == "refinement" and phase == "fragment" else "complete fallback summary"
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
            model=model_id,
            usage={"completion_tokens": 8},
            finish_reason="stop",
        )

    cleanup_observations: list[tuple[list[ContextSummaryStage], list[ContextSummaryFragment]]] = []
    original_cleanup = context_summary_service.cleanup_context_summary_work_safely

    async def observe_before_cleanup(work_dedupe_key: str) -> None:
        async with context_summary_database() as observer_db:
            stages = list((await observer_db.execute(select(ContextSummaryStage))).scalars().all())
            fragments = list((await observer_db.execute(select(ContextSummaryFragment))).scalars().all())
        cleanup_observations.append((stages, fragments))
        await original_cleanup(work_dedupe_key)

    for module in (context_summary_stage, context_summary_service, context_summary_reduction):
        monkeypatch.setattr(module, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)
    monkeypatch.setattr(context_summary_service, "cleanup_context_summary_work_safely", observe_before_cleanup)

    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(context_summary_channel=object()),
        other=SimpleNamespace(context_summary_threshold_percent=50),
    )
    ensure_kwargs = {
        "session_id": "summary-session",
        "uid": "summary-user",
        "profile": profile,
        "cfg": cfg,
        "before_id": 6,
        "current_message": "current question",
        "context_window_k": 4,
        "max_tokens": 512,
        "reserved_tokens": 0,
        "safety_margin_tokens": 0,
        "trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE,
        "fixed_upper_message_id": 5,
        "force": True,
    }

    if fallback_succeeds:
        async with context_summary_database() as db:
            generated_state = await context_summary_service.ensure_context_summary(db, **ensure_kwargs)
        assert generated_state.content == "complete fallback summary"
        assert generated_state.message_id == 4
        assert generated_state.revision == 4
        assert generated_state.content_revision == 7
    else:
        async with context_summary_database() as db:
            with pytest.raises(ContextSummaryOutputBudgetError) as exc_info:
                await context_summary_service.ensure_context_summary(db, **ensure_kwargs)
        assert exc_info.value.message == ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED

    failed_phase_calls = [call for call in model_calls if call[1] == failure_phase]
    assert [model_id for model_id, _phase, _prompt in failed_phase_calls] == [
        "summary-model",
        "summary-model-fallback",
    ]
    assert len(cleanup_observations) == 1
    observed_stages, observed_fragments = cleanup_observations[0]
    expected_failed_models = ["summary-model"]
    if not fallback_succeeds:
        expected_failed_models.append("summary-model-fallback")
    failed_stages = [stage for stage in observed_stages if stage.model_id in expected_failed_models and stage.status == ContextSummaryStageStatus.INVALIDATED and ((stage.lower_stage_key is None) == (failure_phase == "fragment"))]
    assert sorted(stage.model_id for stage in failed_stages) == sorted(expected_failed_models)
    failed_stage_keys = {stage.stage_key for stage in failed_stages}
    assert all(fragment.stage_key not in failed_stage_keys for fragment in observed_fragments)
    assert all("non-sensitive test reasoning" not in fragment.content for fragment in observed_fragments)
    if failed_content is not None:
        assert all(failed_content not in fragment.content for fragment in observed_fragments)

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()

    assert stages == []
    assert fragments == []
    if fallback_succeeds:
        assert session.context_summary == "complete fallback summary"
        assert session.context_summary_message_id == 4
        assert session.context_summary_revision == 4
        assert session.context_content_revision == 7
    else:
        assert session.context_summary == "stable saved summary"
        assert session.context_summary_message_id == 2
        assert session.context_summary_revision == 3
        assert session.context_content_revision == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_content", [None, "partial summary"], ids=["empty-content", "partial-content"])
@pytest.mark.parametrize("fallback_succeeds", [False, True])
async def test_context_summary_reduction_budget_failure_invalidates_and_falls_back(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failed_content: str | None,
    fallback_succeeds: bool,
) -> None:
    await _seed_summary_history(context_summary_database)
    work_dedupe_key = "budget-reduction-work"
    lower_stage_key = "budget-lower-stage"
    lower_model_key = "budget-lower-model"
    snapshot_key = "budget-reduction-snapshot"
    lower_stage = ContextSummaryStage(
        uid="summary-user",
        session_id="summary-session",
        work_id=1,
        work_dedupe_key=work_dedupe_key,
        snapshot_key=snapshot_key,
        stage_key=lower_stage_key,
        lower_stage_key=None,
        model_key=lower_model_key,
        channel_id=11,
        model_id="lower-summary-model",
        context_window_k=64,
        max_output_tokens=20_480,
        safety_margin_tokens=0,
        expected_summary_message_id=None,
        expected_summary_revision=0,
        expected_content_revision=0,
        snapshot_max_message_id=4,
        persistent_summary_target_id=4,
        expected_fragment_count=2,
        succeeded_fragment_count=2,
        status=ContextSummaryStageStatus.COMPLETED,
    )
    lower_contents = [f"completed lower summary {index} " * 50 for index in range(2)]
    lower_fragments = [
        ContextSummaryFragment(
            dedupe_key=build_context_summary_fragment_dedupe_key(
                work_dedupe_key=work_dedupe_key,
                stage_key=lower_stage_key,
                model_key=lower_model_key,
                fragment_index=index,
            ),
            uid="summary-user",
            session_id="summary-session",
            work_id=1,
            work_dedupe_key=work_dedupe_key,
            snapshot_key=snapshot_key,
            stage_key=lower_stage_key,
            model_key=lower_model_key,
            fragment_index=index,
            message_start_id=1 + index * 2,
            message_end_id=2 + index * 2,
            channel_id=11,
            model_id="lower-summary-model",
            token_count=100,
            content=content,
        )
        for index, content in enumerate(lower_contents)
    ]
    async with context_summary_database() as db:
        db.add(lower_stage)
        db.add_all(lower_fragments)
        await db.commit()

    models = {
        priority: replace(
            model,
            max_output_tokens=20_480,
            input_budget_tokens=65_536 - 20_480,
        )
        for priority, model in _summary_rejection_models().items()
    }
    model_requests: list[tuple[str, int]] = []

    async def select_model(*_args, excluded_priorities=None, **_kwargs):
        excluded = set(excluded_priorities or ())
        return next((models[priority] for priority in (1, 2) if priority not in excluded), None)

    async def generate_model(*_args, **kwargs):
        model_id = kwargs["model_id"]
        model_requests.append((model_id, kwargs["max_tokens"]))
        if model_id == "summary-model" or not fallback_succeeds:
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=failed_content),
                model=model_id,
                usage={"completion_tokens": 20_480},
                finish_reason="length",
            )
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="complete merged summary"),
            model=model_id,
            usage={"completion_tokens": 8},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(channel=SimpleNamespace(context_summary_channel=object()))
    result = None
    if fallback_succeeds:
        async with context_summary_database() as db:
            result = await context_summary_reduction.reduce_completed_summary_stage_result(
                db,
                profile=profile,
                cfg=cfg,
                initial_stage=lower_stage,
                safety_margin_tokens=0,
            )
        assert result.content == "complete merged summary"
    else:
        async with context_summary_database() as db:
            with pytest.raises(ContextSummaryOutputBudgetError) as exc_info:
                await context_summary_reduction.reduce_completed_summary_stage_result(
                    db,
                    profile=profile,
                    cfg=cfg,
                    initial_stage=lower_stage,
                    safety_margin_tokens=0,
                )
        assert exc_info.value.message == ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED

    assert model_requests == [
        ("summary-model", 20_480),
        ("summary-model-fallback", 20_480),
    ]

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())

    persisted_lower_stage = next(stage for stage in stages if stage.stage_key == lower_stage_key)
    assert persisted_lower_stage.status == ContextSummaryStageStatus.COMPLETED
    assert persisted_lower_stage.succeeded_fragment_count == 2
    persisted_lower_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == lower_stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    assert [fragment.content for fragment in persisted_lower_fragments] == lower_contents
    assert all(fragment.status.value == "completed" for fragment in persisted_lower_fragments)

    reduction_stages = [stage for stage in stages if stage.lower_stage_key == lower_stage_key]
    reduction_stages_by_model = {stage.model_id: stage for stage in reduction_stages}
    assert set(reduction_stages_by_model) == {"summary-model", "summary-model-fallback"}
    assert reduction_stages_by_model["summary-model"].status == ContextSummaryStageStatus.INVALIDATED
    assert reduction_stages_by_model["summary-model-fallback"].status == (ContextSummaryStageStatus.COMPLETED if fallback_succeeds else ContextSummaryStageStatus.INVALIDATED)
    reduction_fragments = [fragment for fragment in fragments if fragment.stage_key != lower_stage_key]
    if fallback_succeeds:
        assert len(reduction_fragments) == 1
        assert reduction_fragments[0].stage_key == result.stage.stage_key
        assert reduction_fragments[0].content == "complete merged summary"
        assert reduction_fragments[0].status.value == "completed"
    else:
        assert reduction_fragments == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["session-history", "frozen-task"], ids=["session-history", "frozen-task"])
async def test_context_summary_reduction_splits_repeated_range_and_completes(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    first_content = "first source " + "fact " * 700 + " tail evidence"
    last_content = "last message evidence"
    if mode == "session-history":
        await _seed_summary_history(context_summary_database)
        uid, session_id = "summary-user", "summary-session"
        work_dedupe_key = "repeated-range-summary-work"
    else:
        uid, session_id = "frozen-user", "frozen-session"
        work_dedupe_key = f"{FROZEN_CONTEXT_SUMMARY_WORK_KEY_PREFIX}repeated-range-summary"

    lower_stage = make_stage(
        work_dedupe_key=work_dedupe_key,
        stage_key="repeated-range-source-stage",
        model_key="repeated-range-source-model",
        expected_fragment_count=2,
        persistent_summary_target_id=4,
    )
    lower_stage.uid = uid
    lower_stage.session_id = session_id
    lower_stage.snapshot_max_message_id = 4

    async with context_summary_database() as db:
        persisted_stage, created = await context_summary_stage_crud.create_stage(db, stage=lower_stage)
        assert created is True
        for fragment_index, (message_start_id, message_end_id, content) in enumerate([(1, 3, first_content), (4, 4, last_content)]):
            fragment = make_fragment(
                fragment_index=fragment_index,
                work_dedupe_key=work_dedupe_key,
                stage_key=persisted_stage.stage_key,
                model_key=persisted_stage.model_key,
                message_start_id=message_start_id,
                message_end_id=message_end_id,
            )
            fragment.uid = persisted_stage.uid
            fragment.session_id = persisted_stage.session_id
            fragment.content = content
            persisted_fragment, fragment_created = await context_summary_fragment_crud.write_ordered(
                db,
                fragment=fragment,
            )
            assert persisted_fragment is not None
            assert fragment_created is True
        await db.refresh(persisted_stage)
        assert await context_summary_stage_crud.mark_completed(
            db,
            work_dedupe_key=work_dedupe_key,
            stage_key=persisted_stage.stage_key,
            model_key=persisted_stage.model_key,
        )
        await db.refresh(persisted_stage)

    model = replace(
        _summary_rejection_models()[1],
        context_window_tokens=2_000,
        max_output_tokens=768,
        safety_margin_tokens=0,
        input_budget_tokens=1_232,
    )
    model_prompts: list[str] = []

    async def select_model(*_args, **_kwargs):
        return model

    async def generate_model(*_args, **kwargs):
        model_prompts.append(kwargs["messages"][0].content)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="condensed evidence"),
            model=kwargs["model_id"],
            usage={"completion_tokens": 2},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(channel=SimpleNamespace(context_summary_channel=object()))
    async with context_summary_database() as db:
        result = await context_summary_reduction.reduce_completed_summary_stage_result(
            db,
            profile=profile,
            cfg=cfg,
            initial_stage=persisted_stage,
            safety_margin_tokens=0,
        )

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())

    source_stage = next(stage for stage in stages if stage.stage_key == "repeated-range-source-stage")
    first_reduction = next(stage for stage in stages if stage.lower_stage_key == source_stage.stage_key)
    first_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == first_reduction.stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    first_ranges = [(fragment.message_start_id, fragment.message_end_id) for fragment in first_fragments]
    assert source_stage.status == ContextSummaryStageStatus.COMPLETED
    assert source_stage.succeeded_fragment_count == 2
    assert first_reduction.status == ContextSummaryStageStatus.COMPLETED
    assert len(first_fragments) == first_reduction.expected_fragment_count
    assert first_ranges.count((1, 3)) >= 2
    assert first_ranges.count((4, 4)) == 1
    assert set(first_ranges) <= {(1, 3), (4, 4)}

    final_stage = next(stage for stage in stages if stage.stage_key == result.stage.stage_key)
    final_fragments = [fragment for fragment in fragments if fragment.stage_key == final_stage.stage_key]
    assert final_stage.status == ContextSummaryStageStatus.COMPLETED
    assert final_stage.expected_fragment_count == 1
    assert final_stage.succeeded_fragment_count == 1
    assert len(final_fragments) == 1
    assert (final_fragments[0].message_start_id, final_fragments[0].message_end_id) == (1, 4)
    assert result.content == final_fragments[0].content == "condensed evidence"
    assert any("first source " in prompt for prompt in model_prompts)
    assert any("tail evidence" in prompt for prompt in model_prompts)
    assert any(last_content in prompt for prompt in model_prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "no-progress", "insufficient"], ids=["success", "no-progress", "insufficient"])
async def test_context_summary_checkpoint_refines_against_complete_request_budget(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    await _seed_summary_history(context_summary_database)
    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        session.context_summary = "stable saved summary"
        session.context_summary_message_id = 2
        session.context_summary_revision = 3
        session.context_content_revision = 7
        history = list((await db.execute(select(Message).where(Message.session_id == "summary-session", Message.id.in_([3, 4])))).scalars())
        for message in history:
            message.content = "history evidence " * 2000
        db.add(
            Message(
                id=5,
                session_id="summary-session",
                uid="summary-user",
                profile_id=1,
                role=MessageRole.USER,
                content="current question",
            )
        )
        await db.commit()

    summary_model = replace(
        _summary_rejection_models()[1],
        max_output_tokens=2_048,
        input_budget_tokens=65_536 - 2_048,
    )
    first_summary = ("fact " * 1_400).strip()
    summary_1_200 = ("fact " * 1_200).strip()
    near_limit_summary = ("fact " * 725).strip()
    slightly_smaller_summary = ("fact " * 721).strip()
    short_summary = "short summary"
    model_prompts: list[str] = []
    response_sequences = {
        "success": (first_summary, summary_1_200, near_limit_summary, short_summary),
        "insufficient": (first_summary, near_limit_summary, slightly_smaller_summary),
    }

    async def select_model(*_args, excluded_priorities=None, **_kwargs):
        excluded = set(excluded_priorities or ())
        return None if summary_model.priority in excluded else summary_model

    async def generate_model(**kwargs):
        prompt = kwargs["messages"][0].content
        model_prompts.append(prompt)
        response_index = len(model_prompts) - 1
        if mode == "no-progress":
            content = first_summary
        else:
            responses = response_sequences[mode]
            if response_index >= len(responses):
                raise AssertionError("summary refinement attempted another round")
            content = responses[response_index]
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
            model=kwargs["model_id"],
            usage={"completion_tokens": 1},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", select_model)
    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    system_message = InternalMessage(
        role=MessageRole.SYSTEM,
        content=("context fact policy requires preserving verified history and current task constraints " * 7).strip(),
    )
    current_message = InternalMessage(id=5, role=MessageRole.USER, content="current question")
    request_model_id = summary_model.model_id
    request_protocol = summary_model.protocol
    request_channel_id = summary_model.channel_id

    def request_tokens(messages: list[InternalMessage]) -> int:
        return estimate_request_context_tokens(
            materialize_user_environment_prompts(messages),
            [],
            model_id=request_model_id,
            protocol=request_protocol,
            channel_id=request_channel_id,
        )

    def candidate_messages(summary: str) -> list[InternalMessage]:
        summary_message = ContextSummaryState(content=summary, message_id=4).as_message()
        assert summary_message is not None
        return [
            system_message,
            summary_message,
            current_message,
        ]

    target_tokens = calculate_context_summary_trigger_tokens(
        context_window_k=2,
        max_tokens=256,
        threshold_percent=50,
    )
    near_limit_tokens = estimate_tokens(near_limit_summary, model_id=request_model_id, protocol=request_protocol)
    slightly_smaller_tokens = estimate_tokens(slightly_smaller_summary, model_id=request_model_id, protocol=request_protocol)
    assert target_tokens == 744
    assert near_limit_tokens <= target_tokens
    assert slightly_smaller_tokens <= target_tokens
    assert 0 < near_limit_tokens - slightly_smaller_tokens < CONTEXT_SUMMARY_MIN_REFINEMENT_REDUCTION_TOKENS
    assert request_tokens(candidate_messages(near_limit_summary)) > target_tokens
    assert request_tokens(candidate_messages(slightly_smaller_summary)) > target_tokens

    checkpoint_kwargs = {
        "session_id": "summary-session",
        "uid": "summary-user",
        "profile": SimpleNamespace(id=1),
        "cfg": SimpleNamespace(
            channel=SimpleNamespace(context_summary_channel=object()),
            other=SimpleNamespace(context_summary_threshold_percent=50),
        ),
        "messages": [system_message, current_message],
        "trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE,
        "fixed_upper_message_id": 5,
        "context_window_k": 2,
        "max_tokens": 256,
        "tools": [],
        "channel_id": request_channel_id,
        "model_id": request_model_id,
        "protocol": request_protocol,
        "force": True,
    }

    async def run_checkpoint():
        async with context_summary_database() as db:
            return await apply_context_summary_checkpoint(db, **checkpoint_kwargs)

    if mode == "success":
        checkpointed = await run_checkpoint()
        assert request_tokens(checkpointed) <= target_tokens
        checkpointed_summary_meta = checkpointed[1].conversation_summary
        assert checkpointed_summary_meta is not None
        assert checkpointed_summary_meta.content == short_summary
        assert checkpointed_summary_meta.through_message_id == 4
        assert checkpointed_summary_meta.covered_user_message_id is None
        assert checkpointed[1].content is None
        assert checkpointed[2].id == 5
        assert len(model_prompts) == 4
    else:
        with pytest.raises(LLMException) as exc_info:
            await run_checkpoint()
        assert exc_info.value.message == ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED
        if mode == "no-progress":
            assert len(model_prompts) > 1
            assert all(prompt.startswith("Further compress the summary below") for prompt in model_prompts[1:])
        else:
            assert len(model_prompts) == 3
            assert near_limit_summary in model_prompts[2]

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()

    if mode == "success":
        assert session.context_summary == short_summary
        assert session.context_summary_message_id == 4
        assert session.context_summary_revision == 4
    else:
        assert session.context_summary == "stable saved summary"
        assert session.context_summary_message_id == 2
        assert session.context_summary_revision == 3
    assert session.context_content_revision == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_mode", ["threshold", "forced"], ids=["threshold", "forced"])
@pytest.mark.parametrize(
    "main_protocol",
    ["openai", "openai_responses", "deepseek", "deepseek_responses"],
    ids=["openai", "openai-responses", "deepseek", "deepseek-responses"],
)
async def test_context_summary_chunks_tool_results_and_preserves_continuation(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    entry_mode: str,
    main_protocol: str,
) -> None:
    uid = "continuation-user"
    session_id = "continuation-session"
    main_channel_id = 21
    main_model_id = "main-model"
    summary_model = ContextSummaryModelSnapshot(
        channel_id=11,
        channel_name="small-summary-channel",
        model_id="small-summary-model",
        protocol="openai",
        base_url="https://summary.invalid",
        api_key="secret",
        priority=1,
        context_window_tokens=4_096,
        max_output_tokens=512,
        temperature=0.0,
        top_p=None,
        safety_margin_tokens=0,
        input_budget_tokens=3_584,
    )
    evidence_markers = ["TOOL_EVIDENCE_ALPHA_2F", "TOOL_EVIDENCE_BETA_7C", "TOOL_EVIDENCE_GAMMA_9A"]
    continuation_marker = "TOOL_EVIDENCE_DELTA_4D"
    db_user_content = "Continue the verified task from the first objective."
    full_user_content = f"{db_user_content}\n\nThe second objective must preserve every verified evidence marker."
    guidance_prompt = "guidance-continuation-marker"
    user_environment_prompt = json.dumps(
        {
            "environment": {"runtime_context": "runtime-continuation-marker"},
            "response_settings": {"markdown": True, "max_output_tokens": 512},
            "platform_constraints": "platform-continuation-marker",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    todo_v1 = [{"content": "preserve the verified tool evidence", "status": "in_progress"}]
    todo_v2 = [
        {"content": "preserve the verified tool evidence", "status": "in_progress"},
        {"content": "continue with the next tool result", "status": "pending"},
    ]
    tool_calls = [
        InternalToolCall(
            id=f"continuation-call-{index}",
            name="collect_evidence",
            arguments={"evidence_index": index},
        )
        for index in range(1, 4)
    ]
    tool_call_message = InternalMessage(
        id=2,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        tool_calls=tool_calls,
    )
    tool_results: list[InternalMessage] = []
    tool_result_rows: list[Message] = []
    for index, (tool_call, marker) in enumerate(zip(tool_calls, evidence_markers), start=3):
        result_content = json.dumps(
            {"result": ("x " * 3_200) + marker},
            ensure_ascii=False,
        )
        result_message = InternalMessage(
            id=index,
            role=MessageRole.TOOL,
            type=MessageType.TOOL_RESULT,
            content=result_content,
            tool_call_id=tool_call.id,
            tool_execution_status="succeeded",
        )
        tool_results.append(result_message)
        tool_result_rows.append(
            Message(
                id=index,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.TOOL,
                type=MessageType.TOOL_RESULT,
                content=result_message.model_dump_json(),
                is_processed=True,
            )
        )

    single_result_limit_tokens = 4_000
    result_token_estimates = [estimate_tokens(result.content or "", model_id=main_model_id, protocol=main_protocol) for result in tool_results]
    assert all(token_count < single_result_limit_tokens for token_count in result_token_estimates)
    assert all((len(result.content or "") + 3) // 4 < single_result_limit_tokens for result in tool_results)
    assert sum(result_token_estimates) > 8_000 - 512

    provider_metadata = {
        "input_tokens": 7_000,
        "input_tokens_source": "provider",
        "channel_id": main_channel_id,
        "model_id": main_model_id,
        "protocol": main_protocol,
        "context_summary_revision": 0,
        "context_content_revision": 0,
    }
    if entry_mode == "threshold":
        assert provider_metadata["input_tokens"] >= calculate_context_summary_trigger_tokens(
            context_window_k=8,
            max_tokens=512,
            threshold_percent=90,
        )
    async with context_summary_database() as db:
        db.add(
            ChatSession(
                session_id=session_id,
                uid=uid,
                llm_request_metadata=provider_metadata if entry_mode == "threshold" else None,
            )
        )
        db.add(
            SessionTodoPlan(
                session_id=session_id,
                uid=uid,
                revision=1,
                todos=todo_v1,
            )
        )
        db.add(
            Message(
                id=1,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.USER,
                content=db_user_content,
                environment_prompt=user_environment_prompt,
                guidance_prompt=guidance_prompt,
                is_processed=True,
            )
        )
        db.add(
            Message(
                id=2,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                content=tool_call_message.model_dump_json(),
                is_processed=True,
            )
        )
        db.add_all(tool_result_rows)
        await db.commit()

    summary_prompts: list[str] = []
    all_markers = [*evidence_markers, continuation_marker]

    async def _select_model(*_args, **_kwargs) -> ContextSummaryModelSnapshot:
        return summary_model

    async def _call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        assert model == summary_model
        assert estimate_context_summary_prompt_tokens(model, prompt) <= model.input_budget_tokens
        summary_prompts.append(prompt)
        prompt_markers = [marker for marker in all_markers if marker in prompt]
        if '"role":' in prompt or "<message_chunk" in prompt:
            prefix = "raw evidence context"
        else:
            prefix = "merged"
        return " ".join([prefix, *prompt_markers])

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", _select_model)
    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", _select_model)
    monkeypatch.setattr(context_summary_service, "select_context_summary_model", _select_model)
    monkeypatch.setattr(context_summary_stage, "call_context_summary_model", _call_model)

    cfg = SimpleNamespace(
        channel=SimpleNamespace(context_summary_channel=object()),
        other=SimpleNamespace(context_summary_threshold_percent=90),
    )
    profile = SimpleNamespace(id=1)

    async def _run_checkpoint(
        messages: list[InternalMessage],
        *,
        force: bool,
        previous_metadata: dict | None = None,
    ) -> list[InternalMessage]:
        async with context_summary_database() as db:
            return await apply_context_summary_checkpoint(
                db,
                session_id=session_id,
                uid=uid,
                profile=profile,
                cfg=cfg,
                messages=messages,
                trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
                fixed_upper_message_id=1,
                context_window_k=8,
                max_tokens=512,
                tools=[],
                channel_id=main_channel_id,
                model_id=main_model_id,
                protocol=main_protocol,
                previous_llm_request_metadata=previous_metadata,
                force=force,
            )

    def _summary_message(messages: list[InternalMessage]) -> InternalMessage:
        summaries = [message for message in messages if message.role == MessageRole.USER and message.conversation_summary is not None]
        assert len(summaries) == 1
        return summaries[0]

    def _assert_summary_content(
        content: str | None,
        *,
        expected_todo_revision: int,
        expected_todos: list[dict[str, str]],
        expected_markers: list[str],
    ) -> None:
        model_summary, covered = split_covered_user_message(content)
        assert model_summary is not None
        assert covered is not None
        assert covered.message_id == 1
        assert covered.content == full_user_content
        assert all(marker in model_summary for marker in expected_markers)
        todo_start = model_summary.rfind("<current_session_todo_snapshot>")
        assert todo_start >= 0
        assert parse_session_todo_snapshot(model_summary[todo_start:]) == {
            "revision": expected_todo_revision,
            "todos": expected_todos,
        }

    def _assert_summary_message(
        message: InternalMessage,
        *,
        through_message_id: int,
        expected_todo_revision: int,
        expected_todos: list[dict[str, str]],
        expected_markers: list[str],
    ) -> None:
        summary_meta = message.conversation_summary
        assert summary_meta is not None
        assert summary_meta.through_message_id == through_message_id
        assert summary_meta.covered_user_message_id == 1
        assert message.content == full_user_content
        assert all(marker in summary_meta.content for marker in expected_markers)
        todo_start = summary_meta.content.rfind("<current_session_todo_snapshot>")
        assert todo_start >= 0
        assert parse_session_todo_snapshot(summary_meta.content[todo_start:]) == {
            "revision": expected_todo_revision,
            "todos": expected_todos,
        }
        materialized_message = materialize_user_environment_prompts([message])[0]
        assert isinstance(materialized_message.content, str)
        materialized_payload = json.loads(materialized_message.content)
        assert materialized_payload["conversation_summary"] == summary_meta.content
        assert materialized_payload["user_message"] == full_user_content
        assert isinstance(message.environment_prompt, str)
        environment = json.loads(message.environment_prompt)
        assert environment["environment"]["runtime_context"] == "runtime-continuation-marker"
        assert environment["platform_constraints"] == "platform-continuation-marker"

    initial_messages = [
        InternalMessage(
            id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=full_user_content,
            environment_prompt=user_environment_prompt,
            guidance_prompt=guidance_prompt,
        ),
        tool_call_message,
        *tool_results,
    ]
    first_checkpoint = await _run_checkpoint(
        initial_messages,
        force=entry_mode == "forced",
        previous_metadata=provider_metadata if entry_mode == "threshold" else None,
    )
    first_prompt_count = len(summary_prompts)
    first_prompts = summary_prompts[:first_prompt_count]
    assert first_prompt_count > len(tool_results)
    message_chunk_pattern = re.compile(
        r'<message_chunk message_id="(?P<message_id>\d+)" role="tool" part="(?P<part>\d+)">\n'
        r"(?P<payload>.*?)\n</message_chunk>",
        re.DOTALL,
    )
    chunks_by_message_id: dict[int, list[tuple[int, str]]] = {}
    for prompt in first_prompts:
        for match in message_chunk_pattern.finditer(prompt):
            message_id = int(match.group("message_id"))
            chunks_by_message_id.setdefault(message_id, []).append((int(match.group("part")), match.group("payload")))

    assert set(chunks_by_message_id) == {result.id for result in tool_results}
    for result in tool_results:
        assert result.id is not None
        ordered_chunks = sorted(chunks_by_message_id[result.id])
        parts = [part for part, _ in ordered_chunks]
        assert len(parts) == len(set(parts))
        assert parts == list(range(len(parts)))
        parsed = json.loads("".join(payload for _, payload in ordered_chunks))
        assert parsed["content"] == result.content
        assert parsed["tool_call_id"] == result.tool_call_id
    assert len(first_checkpoint) == 1
    first_summary_message = _summary_message(first_checkpoint)
    _assert_summary_message(
        first_summary_message,
        through_message_id=5,
        expected_todo_revision=1,
        expected_todos=todo_v1,
        expected_markers=evidence_markers,
    )

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == session_id))).scalar_one()
        first_rows = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars())
    assert session.context_summary_message_id == 5
    assert session.context_summary_revision == 1
    _assert_summary_content(
        session.context_summary,
        expected_todo_revision=1,
        expected_todos=todo_v1,
        expected_markers=evidence_markers,
    )
    assert [row.content for row in first_rows] == [
        db_user_content,
        tool_call_message.model_dump_json(),
        *(result.model_dump_json() for result in tool_results),
    ]
    first_parsed = parse_db_messages_to_internal(first_rows)
    assert [message.id for message in first_parsed] == [1, 2, 3, 4, 5]
    assert first_parsed[0].content == db_user_content
    assert [message.tool_call_id for message in first_parsed[2:]] == [tool_call.id for tool_call in tool_calls]
    assert [message.content for message in first_parsed[2:]] == [result.content for result in tool_results]

    async def _build_system_prompt(_db, _profile, **_kwargs):
        return "stable system prompt"

    monkeypatch.setattr(prepare_module, "build_system_prompt", _build_system_prompt)
    async with context_summary_database() as db:
        reloaded_messages = await prepare_module.prepare_messages(
            db,
            session_id,
            uid,
            profile,
            cfg,
            None,
            "",
            False,
            context_window_k=8,
            max_tokens=512,
            processed_only=True,
        )
    assert len(reloaded_messages) == 2
    assert reloaded_messages[0].role == MessageRole.SYSTEM
    assert reloaded_messages[1].role == MessageRole.USER
    assert [message.id for message in reloaded_messages if message.id is not None] == []
    reloaded_summary = reloaded_messages[1:]
    assert len(reloaded_summary) == 1
    _assert_summary_message(
        reloaded_summary[0],
        through_message_id=5,
        expected_todo_revision=1,
        expected_todos=todo_v1,
        expected_markers=evidence_markers,
    )
    assert reloaded_summary[0].content == first_summary_message.content
    materialized_reloaded_messages = materialize_user_environment_prompts(reloaded_messages)
    assert isinstance(materialized_reloaded_messages[1].content, str)
    reloaded_user_payload = json.loads(materialized_reloaded_messages[1].content)
    assert reloaded_user_payload["conversation_summary"] == reloaded_summary[0].conversation_summary.content
    assert reloaded_user_payload["user_message"] == full_user_content
    assert reloaded_user_payload["environment"]["runtime_context"] == "runtime-continuation-marker"
    assert reloaded_user_payload["platform_constraints"] == "platform-continuation-marker"
    assert reloaded_user_payload["response_settings"] == {"markdown": True, "max_output_tokens": 512}
    assert reloaded_user_payload["platform_guidance"] == guidance_prompt

    main_upstream_payloads: list[dict] = []

    async def capture_upstream_json(*_args, **kwargs):
        main_upstream_payloads.append(kwargs["payload"])
        if main_protocol in {"openai", "deepseek"}:
            return {
                "id": "chatcmpl-main",
                "model": main_model_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "main response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        return {
            "id": "resp-main",
            "status": "completed",
            "model": main_model_id,
            "output": [
                {
                    "id": "msg-main",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "main response", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setattr(BaseOpenAITransformer, "_post_json", capture_upstream_json)
    monkeypatch.setattr(http_transport_module, "post_json", capture_upstream_json)
    main_response = await LLMClient.generate(
        api_key="test-key",
        base_url="https://main.invalid/v1",
        model_id=main_model_id,
        messages=materialized_reloaded_messages,
        tools=[],
        max_tokens=512,
        channel_id=main_channel_id,
        protocol=main_protocol,
    )

    assert main_response.message.content == "main response"
    assert len(main_upstream_payloads) == 1
    main_payload = main_upstream_payloads[0]
    assert main_payload["model"] == main_model_id
    input_key = "input" if main_protocol in {"openai_responses", "deepseek_responses"} else "messages"
    assert input_key in main_payload
    outbound_input = main_payload[input_key]
    assert isinstance(outbound_input, list)
    outbound_user_items = [item for item in outbound_input if isinstance(item, dict) and item.get("role") == MessageRole.USER.value]
    assert len(outbound_user_items) == 1
    outbound_user_content = outbound_user_items[0].get("content")
    assert isinstance(outbound_user_content, str)
    outbound_user_payload = json.loads(outbound_user_content)
    assert outbound_user_payload == reloaded_user_payload
    assert isinstance(outbound_user_payload["conversation_summary"], str)
    assert isinstance(outbound_user_payload["user_message"], str)
    assert "through_message_id" not in outbound_user_content
    assert "covered_user_message_id" not in outbound_user_content
    assert "<covered_user_message" not in outbound_user_content

    input_payload = {input_key: outbound_input}
    outbound_input_tokens = estimate_tokens(
        json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"), default=str),
        model_id=main_model_id,
        protocol=main_protocol,
    )
    estimated_input_tokens = estimate_request_context_tokens(
        materialized_reloaded_messages,
        [],
        model_id=main_model_id,
        protocol=main_protocol,
        channel_id=main_channel_id,
    )
    assert outbound_input_tokens == estimated_input_tokens

    new_tool_call = InternalToolCall(
        id="continuation-call-delta",
        name="collect_evidence",
        arguments={"evidence_index": 4},
    )
    new_tool_call_message = InternalMessage(
        id=6,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        tool_calls=[new_tool_call],
    )
    new_result = InternalMessage(
        id=7,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=json.dumps(
            {"result": f"continuation output with {continuation_marker}"},
            ensure_ascii=False,
        ),
        tool_call_id=new_tool_call.id,
        tool_execution_status="succeeded",
    )
    async with context_summary_database() as db:
        plan = await db.get(SessionTodoPlan, session_id)
        assert plan is not None
        plan.revision = 2
        plan.todos = todo_v2
        db.add(
            Message(
                id=6,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                content=new_tool_call_message.model_dump_json(),
                is_processed=True,
            )
        )
        db.add(
            Message(
                id=7,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.TOOL,
                type=MessageType.TOOL_RESULT,
                content=new_result.model_dump_json(),
                is_processed=True,
            )
        )
        await db.commit()

    second_prompt_start = len(summary_prompts)
    second_checkpoint = await _run_checkpoint(
        [*reloaded_summary, new_tool_call_message, new_result],
        force=True,
    )
    second_prompts = summary_prompts[second_prompt_start:]
    assert second_prompts
    assert any(continuation_marker in prompt for prompt in second_prompts)
    assert len(second_checkpoint) == 1
    second_summary_message = _summary_message(second_checkpoint)
    _assert_summary_message(
        second_summary_message,
        through_message_id=7,
        expected_todo_revision=2,
        expected_todos=todo_v2,
        expected_markers=[*evidence_markers, continuation_marker],
    )
    assert second_summary_message.environment_prompt == first_summary_message.environment_prompt

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == session_id))).scalar_one()
    assert session.context_summary_message_id == 7
    assert session.context_summary_revision == 2
    _assert_summary_content(
        session.context_summary,
        expected_todo_revision=2,
        expected_todos=todo_v2,
        expected_markers=[*evidence_markers, continuation_marker],
    )

    stable_prompt_count = len(summary_prompts)
    stable_checkpoint = await _run_checkpoint(second_checkpoint, force=True)
    assert len(summary_prompts) == stable_prompt_count
    assert len(stable_checkpoint) == 1
    assert stable_checkpoint[0].content == second_summary_message.content
    assert stable_checkpoint[0].environment_prompt == second_summary_message.environment_prompt

    async with context_summary_database() as db:
        final_rows = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars())
        final_stages = list((await db.execute(select(ContextSummaryStage).where(ContextSummaryStage.session_id == session_id))).scalars())
        final_fragments = list((await db.execute(select(ContextSummaryFragment).where(ContextSummaryFragment.session_id == session_id))).scalars())
    assert final_stages == []
    assert final_fragments == []
    assert [row.id for row in final_rows] == [1, 2, 3, 4, 5, 6, 7]
    assert [row.content for row in final_rows] == [
        db_user_content,
        tool_call_message.model_dump_json(),
        *(result.model_dump_json() for result in tool_results),
        new_tool_call_message.model_dump_json(),
        new_result.model_dump_json(),
    ]
    final_parsed = parse_db_messages_to_internal(final_rows)
    assert [message.id for message in final_parsed] == [1, 2, 3, 4, 5, 6, 7]
    assert final_parsed[0].content == db_user_content
    final_tool_call = next(message for message in final_parsed if message.id == 6)
    assert [tool_call.id for tool_call in final_tool_call.tool_calls or []] == [new_tool_call.id]
    final_tool_results = [message for message in final_parsed if message.role == MessageRole.TOOL]
    assert [message.id for message in final_tool_results] == [3, 4, 5, 7]
    assert [message.tool_call_id for message in final_tool_results] == [tool_call.id for tool_call in [*tool_calls, new_tool_call]]
    assert [message.content for message in final_tool_results] == [result.content for result in [*tool_results, new_result]]
