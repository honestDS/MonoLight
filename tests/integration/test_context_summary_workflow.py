from __future__ import annotations

from collections.abc import AsyncIterator
from importlib import import_module
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.prompts import CONTEXT_SUMMARY_WRAPPER
from app.core.utils.context_summary import merge as context_summary_merge
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import service as context_summary_service
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage, ContextSummaryStageStatus
from app.models.message import Message, MessageRole
from app.models.session import ChatSession

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")


@pytest_asyncio.fixture
async def context_summary_database(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "context-summary-workflow.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    ChatSession.__table__,
                    Message.__table__,
                    ContextSummaryStage.__table__,
                    ContextSummaryFragment.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(context_summary_service, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(context_summary_stage, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(context_summary_reduction, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(context_summary_merge, "AsyncSessionLocal", session_factory)
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
