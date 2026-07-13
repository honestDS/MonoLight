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


def _make_stage(
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


def _make_fragment(
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


async def _count_rows(
    db: AsyncSession,
    model: type[ContextSummaryStage] | type[ContextSummaryFragment],
) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def _create_messages(
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


async def _write_complete_stage(
    db: AsyncSession,
    *,
    message_ids: list[int] | None = None,
) -> None:
    await _create_messages(db, message_ids or list(range(1, 21)))
    await context_summary_stage_crud.create_stage(
        db,
        stage=_make_stage(),
    )
    for fragment_index in range(2):
        _, created = await context_summary_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(fragment_index=fragment_index),
        )
        assert created is True


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
    await _create_messages(db_session, list(range(1, 21)))
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
async def test_completed_fragment_page_uses_fixed_lower_stage_and_message_cursor(
    db_session: AsyncSession,
):
    await _create_messages(db_session, list(range(1, 51)))
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(
            expected_fragment_count=5,
            persistent_summary_target_id=50,
        ),
    )
    for fragment_index in range(5):
        _, created = await context_summary_fragment_crud.write_ordered(
            db_session,
            fragment=_make_fragment(fragment_index=fragment_index),
        )
        assert created is True
    assert (
        await context_summary_stage_crud.mark_completed(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
        )
        is True
    )

    first_page = await context_summary_stage_crud.get_completed_fragment_page(
        db_session,
        work_dedupe_key="work-key",
        lower_stage_key="stage-0",
        limit=2,
    )
    second_page = await context_summary_stage_crud.get_completed_fragment_page(
        db_session,
        work_dedupe_key="work-key",
        lower_stage_key="stage-0",
        page_after_message_id=20,
        limit=2,
    )
    final_page = await context_summary_stage_crud.get_completed_fragment_page(
        db_session,
        work_dedupe_key="work-key",
        lower_stage_key="stage-0",
        page_after_message_id=40,
        limit=2,
    )

    assert first_page is not None
    assert first_page.stage.stage_key == "stage-0"
    assert [fragment.fragment_index for fragment in first_page.fragments] == [0, 1]
    assert second_page is not None
    assert [fragment.fragment_index for fragment in second_page.fragments] == [2, 3]
    assert final_page is not None
    assert [fragment.fragment_index for fragment in final_page.fragments] == [4]
    assert (
        await context_summary_stage_crud.get_completed_fragment_page(
            db_session,
            work_dedupe_key="work-key",
            lower_stage_key="other-stage",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage_status",
    [
        ContextSummaryStageStatus.RUNNING,
        ContextSummaryStageStatus.FAILED,
        ContextSummaryStageStatus.INVALIDATED,
    ],
)
async def test_completed_fragment_page_rejects_unfinished_lower_stage(
    db_session: AsyncSession,
    stage_status: ContextSummaryStageStatus,
):
    stage = _make_stage(expected_fragment_count=1)
    stage.status = stage_status
    if stage_status != ContextSummaryStageStatus.RUNNING:
        stage.error = "stage unavailable"
    db_session.add(stage)
    db_session.add(_make_fragment(fragment_index=0))
    await db_session.commit()

    page = await context_summary_stage_crud.get_completed_fragment_page(
        db_session,
        work_dedupe_key="work-key",
        lower_stage_key="stage-0",
    )

    assert page is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_after_message_id", "limit"),
    [
        (0, 200),
        (None, 0),
        (None, 501),
    ],
)
async def test_completed_fragment_page_rejects_invalid_pagination(
    db_session: AsyncSession,
    page_after_message_id: int | None,
    limit: int,
):
    with pytest.raises(ValueError):
        await context_summary_stage_crud.get_completed_fragment_page(
            db_session,
            work_dedupe_key="work-key",
            lower_stage_key="stage-0",
            page_after_message_id=page_after_message_id,
            limit=limit,
        )


@pytest.mark.asyncio
async def test_completion_validation_accepts_global_message_id_gaps(
    db_session: AsyncSession,
):
    await _create_messages(db_session, [1, 2, 11, 12])
    await _create_messages(
        db_session,
        list(range(3, 11)),
        session_id="other-session",
    )
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=_make_stage(persistent_summary_target_id=12),
    )
    first, first_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(
            fragment_index=0,
            message_start_id=1,
            message_end_id=2,
        ),
    )
    second, second_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=_make_fragment(
            fragment_index=1,
            message_start_id=11,
            message_end_id=12,
        ),
    )

    assert first is not None
    assert first_created is True
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("fragment_index", 2),
        ("message_start_id", 10),
        ("message_start_id", 12),
        ("message_end_id", 19),
        ("model_key", "other-model"),
        ("channel_id", 11),
        ("model_id", "other-summary-model"),
        ("snapshot_key", "other-snapshot"),
        ("status", ContextSummaryFragmentStatus.INVALIDATED),
    ],
)
async def test_completion_validation_rejects_invalid_fragment_set(
    db_session: AsyncSession,
    field_name: str,
    invalid_value,
):
    await _write_complete_stage(db_session)
    second = await context_summary_fragment_crud.get_by_dedupe_key(
        db_session,
        dedupe_key=build_context_summary_fragment_dedupe_key(
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
            fragment_index=1,
        ),
    )
    assert second is not None
    setattr(second, field_name, invalid_value)
    db_session.add(second)
    await db_session.commit()

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
    assert stage.status == ContextSummaryStageStatus.RUNNING
    assert stage.completed_at is None


@pytest.mark.asyncio
async def test_completion_validation_rejects_missing_fragment(
    db_session: AsyncSession,
):
    await _write_complete_stage(db_session)
    second = await context_summary_fragment_crud.get_by_dedupe_key(
        db_session,
        dedupe_key=build_context_summary_fragment_dedupe_key(
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
            fragment_index=1,
        ),
    )
    assert second is not None
    await db_session.delete(second)
    await db_session.commit()

    assert (
        await context_summary_stage_crud.mark_completed(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
        )
        is False
    )


@pytest.mark.asyncio
async def test_completion_validation_rejects_non_running_stage(
    db_session: AsyncSession,
):
    await _write_complete_stage(db_session)
    assert (
        await context_summary_stage_crud.mark_failed(
            db_session,
            work_dedupe_key="work-key",
            stage_key="stage-0",
            model_key="model-key",
            error="cancelled before completion",
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
    assert stage.status == ContextSummaryStageStatus.FAILED
    assert stage.completed_at is None


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
