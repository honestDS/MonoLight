import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.context_summary_stage import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
    context_summary_stage_crud,
)
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryFragmentStatus,
    ContextSummaryStageStatus,
)
from tests.unit.context_summary_stage_test_support import (
    count_rows,
    create_messages,
    make_fragment,
    make_stage,
    write_complete_stage,
)

pytest_plugins = ("tests.unit.context_summary_stage_fixture",)


@pytest.mark.asyncio
async def test_completion_validation_accepts_global_message_id_gaps(
    db_session: AsyncSession,
):
    await create_messages(db_session, [1, 2, 11, 12])
    await create_messages(
        db_session,
        list(range(3, 11)),
        session_id="other-session",
    )
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(persistent_summary_target_id=12),
    )
    first, first_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=make_fragment(
            fragment_index=0,
            message_start_id=1,
            message_end_id=2,
        ),
    )
    second, second_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=make_fragment(
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
    await write_complete_stage(db_session)
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
    await write_complete_stage(db_session)
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
    await write_complete_stage(db_session)
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
        stage=make_stage(expected_fragment_count=1),
    )

    wrong_model = make_fragment(fragment_index=0, model_key="other-model")
    rejected, created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=wrong_model,
    )
    assert rejected is None
    assert created is False

    wrong_dedupe = make_fragment(fragment_index=0)
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
    assert await count_rows(db_session, ContextSummaryFragment) == 0


@pytest.mark.asyncio
async def test_failed_or_invalidated_stage_rejects_late_fragments(
    db_session: AsyncSession,
):
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(expected_fragment_count=2),
    )
    _, created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=make_fragment(fragment_index=0),
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
        fragment=make_fragment(fragment_index=1),
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
