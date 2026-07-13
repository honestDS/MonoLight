from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, func, select

from app.core.crud.context_summary_stage import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
    context_summary_stage_crud,
)
from app.core.utils.time import get_local_time
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryFragmentStatus,
    ContextSummaryStage,
    ContextSummaryStageStatus,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    ContextSummaryStage.__table__,
                    ContextSummaryFragment.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _make_stage(
    *,
    work_dedupe_key: str = "work-key",
    stage_key: str = "stage-0",
    model_key: str = "model-key",
    expected_fragment_count: int = 2,
    created_at=None,
) -> ContextSummaryStage:
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
        snapshot_max_message_id=100,
        persistent_summary_target_id=80,
        expected_fragment_count=expected_fragment_count,
        created_at=created_at or get_local_time(),
    )


def _make_fragment(
    *,
    fragment_index: int,
    work_dedupe_key: str = "work-key",
    stage_key: str = "stage-0",
    model_key: str = "model-key",
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
        message_start_id=fragment_index * 10 + 1,
        message_end_id=fragment_index * 10 + 10,
        channel_id=10,
        model_id="summary-model",
        token_count=20,
        content=f"fragment-{fragment_index}",
        created_at=created_at or get_local_time(),
    )


async def _count_rows(
    db: AsyncSession,
    model: type[ContextSummaryStage] | type[ContextSummaryFragment],
) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_stage_creation_is_idempotent(db_session: AsyncSession):
    first, first_created = await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(),
    )
    duplicate, duplicate_created = await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(),
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert await _count_rows(db_session, ContextSummaryStage) == 1


@pytest.mark.asyncio
async def test_ordered_fragment_write_counts_once_and_completes_atomically(
    db_session: AsyncSession,
):
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(),
    )

    out_of_order, out_of_order_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(fragment_index=1),
    )
    assert out_of_order is None
    assert out_of_order_created is False

    first, first_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(fragment_index=0),
    )
    duplicate, duplicate_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(fragment_index=0),
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate is not None
    assert duplicate.id == first.id
    assert (
        await context_summary_stage_crud.mark_completed(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
        )
        is False
    )

    second, second_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(fragment_index=1),
    )
    assert second is not None
    assert second_created is True
    assert (
        await context_summary_stage_crud.mark_completed(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
        )
        is True
    )
    assert (
        await context_summary_stage_crud.mark_completed(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
        )
        is False
    )

    stage = await context_summary_stage_crud.get_by_identity(
        db_session,
        work_dedupe_key="work-key",
        stage_key="stage-0",
    )
    assert stage is not None
    assert stage.succeeded_fragment_count == 2
    assert stage.status == ContextSummaryStageStatus.COMPLETED
    assert stage.completed_at is not None
    assert await _count_rows(db_session, ContextSummaryFragment) == 2


@pytest.mark.asyncio
async def test_fragment_write_rejects_wrong_identity_and_invalid_dedupe(
    db_session: AsyncSession,
):
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(expected_fragment_count=1),
    )

    wrong_model = _make_fragment(fragment_index=0, model_key="other-model")
    rejected, created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=wrong_model,
    )
    assert rejected is None
    assert created is False

    wrong_dedupe = _make_fragment(fragment_index=0)
    wrong_dedupe.dedupe_key = "not-the-deterministic-key"
    rejected, created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=wrong_dedupe,
    )
    assert rejected is None
    assert created is False

    stage = await context_summary_stage_crud.get_by_identity(
        db_session,
        work_dedupe_key="work-key",
        stage_key="stage-0",
    )
    assert stage is not None
    assert stage.succeeded_fragment_count == 0
    assert await _count_rows(db_session, ContextSummaryFragment) == 0


@pytest.mark.asyncio
async def test_failed_or_invalidated_stage_rejects_late_fragments(
    db_session: AsyncSession,
):
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(expected_fragment_count=2),
    )
    _, created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(fragment_index=0),
    )
    assert created is True

    assert (
        await context_summary_stage_crud.mark_failed(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
            error="fragment failed",
        )
        is True
    )
    assert (
        await context_summary_stage_crud.invalidate(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
        )
        is True
    )

    late_fragment, late_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(fragment_index=1),
    )
    assert late_fragment is None
    assert late_created is False

    stage = await context_summary_stage_crud.get_by_identity(
        db_session,
        work_dedupe_key="work-key",
        stage_key="stage-0",
    )
    fragment = await context_summary_fragment_crud.get_by_dedupe_key(
        db_session,
        dedupe_key=build_context_summary_fragment_dedupe_key(
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
            fragment_index=0,
        ),
    )
    assert stage is not None
    assert stage.status == ContextSummaryStageStatus.INVALIDATED
    assert stage.succeeded_fragment_count == 1
    assert fragment is not None
    assert fragment.status == ContextSummaryFragmentStatus.INVALIDATED


@pytest.mark.asyncio
async def test_cleanup_by_work_deletes_fragments_then_stages_in_batches(
    db_session: AsyncSession,
):
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(expected_fragment_count=3),
    )
    for fragment_index in range(3):
        _, created = await context_summary_fragment_crud.write_ordered(
            db_session,
            fragment=_make_fragment(fragment_index=fragment_index),
        )
        assert created is True

    assert (
        await context_summary_stage_crud.cleanup_by_work(
            db_session,
            work_dedupe_key="work-key",
            batch_size=2,
        )
        == 2
    )
    assert await _count_rows(db_session, ContextSummaryFragment) == 1
    assert await _count_rows(db_session, ContextSummaryStage) == 1

    assert (
        await context_summary_stage_crud.cleanup_by_work(
            db_session,
            work_dedupe_key="work-key",
            batch_size=2,
        )
        == 1
    )
    assert (
        await context_summary_stage_crud.cleanup_by_work(
            db_session,
            work_dedupe_key="work-key",
            batch_size=2,
        )
        == 1
    )
    assert (
        await context_summary_stage_crud.cleanup_by_work(
            db_session,
            work_dedupe_key="work-key",
            batch_size=2,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_cleanup_expired_is_bounded_and_keeps_current_records(
    db_session: AsyncSession,
):
    expired_at = get_local_time() - timedelta(days=2)
    current_at = get_local_time()

    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(
            work_dedupe_key="expired-work",
            expected_fragment_count=2,
            created_at=expired_at,
        ),
    )
    for fragment_index in range(2):
        _, created = await context_summary_fragment_crud.write_ordered(
            db_session,
            fragment=_make_fragment(
                fragment_index=fragment_index,
                work_dedupe_key="expired-work",
                created_at=expired_at,
            ),
        )
        assert created is True

    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(
            work_dedupe_key="current-work",
            expected_fragment_count=1,
            created_at=current_at,
        ),
    )
    _, created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(
            fragment_index=0,
            work_dedupe_key="current-work",
            created_at=current_at,
        ),
    )
    assert created is True

    cutoff = get_local_time() - timedelta(days=1)
    assert (
        await context_summary_stage_crud.cleanup_expired(
            db_session,
            before=cutoff,
            batch_size=1,
        )
        == 1
    )
    assert (
        await context_summary_stage_crud.cleanup_expired(
            db_session,
            before=cutoff,
            batch_size=1,
        )
        == 1
    )
    assert (
        await context_summary_stage_crud.cleanup_expired(
            db_session,
            before=cutoff,
            batch_size=1,
        )
        == 1
    )
    assert (
        await context_summary_stage_crud.cleanup_expired(
            db_session,
            before=cutoff,
            batch_size=1,
        )
        == 0
    )

    remaining_stage = await context_summary_stage_crud.get_by_identity(
        db_session,
        work_dedupe_key="current-work",
        stage_key="stage-0",
    )
    assert remaining_stage is not None
    assert (await _count_rows(db_session, ContextSummaryStage)) == 1
    assert (await _count_rows(db_session, ContextSummaryFragment)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [0, 1001])
async def test_cleanup_rejects_invalid_batch_size(
    db_session: AsyncSession,
    batch_size: int,
):
    with pytest.raises(ValueError):
        await context_summary_stage_crud.cleanup_by_work(
            db_session,
            work_dedupe_key="work-key",
            batch_size=batch_size,
        )
    with pytest.raises(ValueError):
        await context_summary_stage_crud.cleanup_expired(
            db_session,
            before=get_local_time(),
            batch_size=batch_size,
        )
