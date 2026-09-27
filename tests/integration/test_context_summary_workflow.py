from __future__ import annotations

from collections.abc import AsyncIterator
from importlib import import_module
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.exceptions import LLMContextLengthException
from app.core.prompts import CONTEXT_SUMMARY_WRAPPER
from app.core.utils.context_summary import cleanup as context_summary_cleanup
from app.core.utils.context_summary import merge as context_summary_merge
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import service as context_summary_service
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage, ContextSummaryStageStatus
from app.models.message import InternalMessage, InternalResponse, Message, MessageRole
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan
from tests.database_support import clone_sqlite_schema

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")


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
    assert messages[1].content == CONTEXT_SUMMARY_WRAPPER.format(
        through_message_id=2,
        content="compressed old turns",
    )
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
    assert compacted[1].content == CONTEXT_SUMMARY_WRAPPER.format(
        through_message_id=2,
        content="provider-triggered summary",
    )
    assert [message.id for message in compacted[2:]] == [3, 4]

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        assert session.context_summary == "provider-triggered summary"
        assert session.context_summary_message_id == 2


@pytest.mark.asyncio
async def test_provider_overflow_persists_summary_before_same_channel_retry(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with context_summary_database() as db:
        db.add(ChatSession(session_id="recovery-session", uid="recovery-user"))
        db.add_all(
            [
                Message(id=1, session_id="recovery-session", uid="recovery-user", profile_id=1, role=MessageRole.USER, content="old question"),
                Message(id=2, session_id="recovery-session", uid="recovery-user", profile_id=1, role=MessageRole.ASSISTANT, content="old answer"),
                Message(id=3, session_id="recovery-session", uid="recovery-user", profile_id=1, role=MessageRole.USER, content="current question"),
            ]
        )
        await db.commit()

    async def generate_summary(*_args, **_kwargs):
        return context_summary_stage.GeneratedSummaryResult(
            content="overflow recovery summary",
            message_count=2,
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

    attempts = []

    async def generate_response(**kwargs):
        attempts.append([message.content for message in kwargs["messages"]])
        if len(attempts) == 1:
            raise LLMContextLengthException(provider_message="maximum context length exceeded")
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="recovered"),
            model=kwargs["model_id"],
            usage={"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 20},
        )

    async def unexpected_channel_fallback(*_args, **_kwargs):
        raise AssertionError("same-channel summary recovery must run before channel fallback")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate_response)
    monkeypatch.setattr(interactive_generation_module, "select_channel", unexpected_channel_fallback)

    async with context_summary_database() as db:
        state = SimpleNamespace(
            db=db,
            uid="recovery-user",
            session_id="recovery-session",
            profile=SimpleNamespace(id=1),
            cfg=SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=90)),
            messages=[
                InternalMessage(role=MessageRole.SYSTEM, content="stable system prompt"),
                InternalMessage(id=1, role=MessageRole.USER, content="old question"),
                InternalMessage(id=2, role=MessageRole.ASSISTANT, content="old answer"),
                InternalMessage(id=3, role=MessageRole.USER, content="current question"),
            ],
            checkpoint_state=SimpleNamespace(
                upper_message_id=3,
                total_output_tokens=0,
                session_total_input_tokens=0,
                session_total_cached_tokens=0,
                session_total_output_tokens=0,
            ),
            context_summary_work_validity_checker=None,
            context_summary_lifecycle_callback=None,
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
            stream_event_callback=None,
            request_metadata_callback=None,
            expose_tool_call_content=True,
            show_tool_calls=True,
            dispatcher_mode="non_stream",
            dispatch_logger=Logger(),
            img_understanding=False,
            audio_understanding=False,
            video_understanding=False,
        )

        result = await interactive_generation_module.generate_interactive_turn(
            state,
            current_tools=[],
            response_id="response-1",
        )
        await db.refresh((await db.execute(select(ChatSession).where(ChatSession.session_id == "recovery-session"))).scalar_one())
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "recovery-session"))).scalar_one()

    assert result.message.content == "recovered"
    assert len(attempts) == 2
    assert all("overflow recovery summary" not in str(content) for content in attempts[0])
    assert any("overflow recovery summary" in str(content) for content in attempts[1])
    assert session.context_summary == "overflow recovery summary"
    assert session.context_summary_message_id == 2
