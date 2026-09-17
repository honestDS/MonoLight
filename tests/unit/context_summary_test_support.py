from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, func, select

from app.core.crud.context_summary.stage import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
    context_summary_stage_crud,
)
from app.core.utils.time import get_local_time
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryStage,
)
from app.models.message import Message, MessageRole


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    Message.__table__,
                    ContextSummaryStage.__table__,
                    ContextSummaryFragment.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def make_stage(
    *,
    work_dedupe_key: str = "work-key",
    stage_key: str = "stage-0",
    model_key: str = "model-key",
    expected_fragment_count: int = 2,
    persistent_summary_target_id: int | None = None,
    created_at=None,
) -> ContextSummaryStage:
    target_id = persistent_summary_target_id or expected_fragment_count * 10
    return ContextSummaryStage(
        uid="user-1",
        session_id="session-1",
        work_id=1,
        work_dedupe_key=work_dedupe_key,
        snapshot_key="snapshot-key",
        stage_key=stage_key,
        lower_stage_key=None,
        model_key=model_key,
        channel_id=10,
        model_id="summary-model",
        context_window_k=128,
        max_output_tokens=4096,
        safety_margin_tokens=512,
        expected_summary_message_id=None,
        expected_summary_revision=0,
        snapshot_max_message_id=max(100, target_id),
        persistent_summary_target_id=target_id,
        expected_fragment_count=expected_fragment_count,
        created_at=created_at or get_local_time(),
    )


def make_fragment(
    *,
    fragment_index: int,
    work_dedupe_key: str = "work-key",
    stage_key: str = "stage-0",
    model_key: str = "model-key",
    message_start_id: int | None = None,
    message_end_id: int | None = None,
    created_at=None,
) -> ContextSummaryFragment:
    return ContextSummaryFragment(
        dedupe_key=build_context_summary_fragment_dedupe_key(
            work_dedupe_key=work_dedupe_key,
            stage_key=stage_key,
            model_key=model_key,
            fragment_index=fragment_index,
        ),
        uid="user-1",
        session_id="session-1",
        work_id=1,
        work_dedupe_key=work_dedupe_key,
        snapshot_key="snapshot-key",
        stage_key=stage_key,
        model_key=model_key,
        fragment_index=fragment_index,
        message_start_id=message_start_id or fragment_index * 10 + 1,
        message_end_id=message_end_id or fragment_index * 10 + 10,
        channel_id=10,
        model_id="summary-model",
        token_count=20,
        content=f"fragment-{fragment_index}",
        created_at=created_at or get_local_time(),
    )


async def count_rows(
    db: AsyncSession,
    model: type[ContextSummaryStage] | type[ContextSummaryFragment],
) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def create_messages(
    db: AsyncSession,
    message_ids: list[int],
    *,
    session_id: str = "session-1",
    uid: str = "user-1",
) -> None:
    db.add_all(
        [
            Message(
                id=message_id,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.USER,
                content=f"message-{message_id}",
            )
            for message_id in message_ids
        ]
    )
    await db.commit()


async def write_complete_stage(
    db: AsyncSession,
    *,
    message_ids: list[int] | None = None,
) -> None:
    await create_messages(db, message_ids or list(range(1, 21)))
    await context_summary_stage_crud.create_stage(
        db,
        stage=make_stage(),
    )
    for fragment_index in range(2):
        _, created = await context_summary_fragment_crud.write_ordered(
            db,
            fragment=make_fragment(fragment_index=fragment_index),
        )
        assert created is True
