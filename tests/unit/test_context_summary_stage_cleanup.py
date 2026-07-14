from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.context_summary_stage import (
    context_summary_fragment_crud,
    context_summary_stage_crud,
)
from app.core.utils.time import get_local_time
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryStage,
)
from tests.unit.context_summary_stage_test_support import (
    count_rows,
    make_fragment,
    make_stage,
)

pytest_plugins = ("tests.unit.context_summary_stage_fixture",)


@pytest.mark.asyncio
async def test_cleanup_by_work_deletes_fragments_then_stages_in_batches(
    db_session: AsyncSession,
):
    await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(expected_fragment_count=3),
    )
    for fragment_index in range(3):
        _, created = await context_summary_fragment_crud.write_ordered(
            db_session,
            fragment=make_fragment(fragment_index=fragment_index),
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
    assert await count_rows(db_session, ContextSummaryFragment) == 1
    assert await count_rows(db_session, ContextSummaryStage) == 1

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
        stage=make_stage(
            work_dedupe_key="expired-work",
            expected_fragment_count=2,
            created_at=expired_at,
        ),
    )
    for fragment_index in range(2):
        _, created = await context_summary_fragment_crud.write_ordered(
            db_session,
            fragment=make_fragment(
                fragment_index=fragment_index,
                work_dedupe_key="expired-work",
                created_at=expired_at,
            ),
        )
        assert created is True

    await context_summary_stage_crud.create_stage(
        db_session,
        stage=make_stage(
            work_dedupe_key="current-work",
            expected_fragment_count=1,
            created_at=current_at,
        ),
    )
    _, created = await context_summary_fragment_crud.write_ordered(
        db_session,
        fragment=make_fragment(
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
    assert (await count_rows(db_session, ContextSummaryStage)) == 1
    assert (await count_rows(db_session, ContextSummaryFragment)) == 1


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
