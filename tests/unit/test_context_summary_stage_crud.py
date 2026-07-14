import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.context_summary_stage import (
    context_summary_fragment_crud,
    context_summary_stage_crud,
)
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryStage,
    ContextSummaryStageStatus,
)
from tests.unit.context_summary_stage_test_support import (
    count_rows,
    create_messages,
    make_fragment,
    make_stage,
)

pytest_plugins = ("tests.unit.context_summary_stage_fixture",)


@pytest.mark.asyncio
async def test_stage_creation_is_idempotent(db_session: AsyncSession):
    first, first_created = await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(),
    )
    duplicate, duplicate_created = await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(),
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert await count_rows(db_session, ContextSummaryStage) == 1


@pytest.mark.asyncio
async def test_ordered_fragment_write_counts_once_and_completes_atomically(
    db_session: AsyncSession,
):
    await create_messages(db_session, list(range(1, 21)))
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(),
    )

    out_of_order, out_of_order_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=make_fragment(fragment_index=1),
    )
    assert out_of_order is None
    assert out_of_order_created is False

    first, first_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=make_fragment(fragment_index=0),
    )
    duplicate, duplicate_created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=make_fragment(fragment_index=0),
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
        fragment=make_fragment(fragment_index=1),
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
    assert await count_rows(db_session, ContextSummaryFragment) == 2


@pytest.mark.asyncio
async def test_completed_fragment_page_uses_fixed_lower_stage_and_message_cursor(
    db_session: AsyncSession,
):
    await create_messages(db_session, list(range(1, 51)))
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(
            expected_fragment_count=5,
            persistent_summary_target_id=50,
        ),
    )
    for fragment_index in range(5):
        _, created = await context_summary_fragment_crud.write_ordered(
            db_session,
            fragment=make_fragment(fragment_index=fragment_index),
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
    stage = make_stage(expected_fragment_count=1)
    stage.status = stage_status
    if stage_status != ContextSummaryStageStatus.RUNNING:
        stage.error = "stage unavailable"
    db_session.add(stage)
    db_session.add(make_fragment(fragment_index=0))
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
