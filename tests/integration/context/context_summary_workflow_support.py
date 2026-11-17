from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.utils.context_summary import cleanup as context_summary_cleanup
from app.core.utils.context_summary import merge as context_summary_merge
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import service as context_summary_service
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage
from app.models.message import Message, MessageRole
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.models.session_todo import SessionTodoPlan
from tests.database_support import clone_sqlite_schema


@pytest_asyncio.fixture(name="context_summary_database")
async def _context_summary_database(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
            SessionReplyWorkItem.__table__,
            SessionReplyStreamEvent.__table__,
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
