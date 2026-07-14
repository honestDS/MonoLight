from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.context_summary_stage import (
    context_summary_fragment_crud,
    context_summary_stage_crud,
)
from app.core.utils.context_summary import cleanup as cleanup_module
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
async def test_cleanup_context_summary_work_runs_batches_until_empty(monkeypatch):
    batch_counts = iter([200, 17, 0])
    cleanup_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def cleanup_by_work(db, *, work_dedupe_key, batch_size=200):
        assert isinstance(db, FakeSession)
        cleanup_calls.append((work_dedupe_key, batch_size))
        return next(batch_counts)

    monkeypatch.setattr(cleanup_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        cleanup_module.context_summary_stage_crud,
        "cleanup_by_work",
        cleanup_by_work,
    )

    deleted_count = await cleanup_module.cleanup_context_summary_work("work-key")

    assert deleted_count == 217
    assert cleanup_calls == [
        ("work-key", 200),
        ("work-key", 200),
        ("work-key", 200),
    ]


@pytest.mark.asyncio
async def test_cleanup_expired_context_summary_stages_runs_until_empty(monkeypatch):
    batch_counts = iter([200, 1, 0])
    before_values = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def cleanup_expired(db, *, before, batch_size=200):
        assert isinstance(db, FakeSession)
        before_values.append((before, batch_size))
        return next(batch_counts)

    monkeypatch.setattr(cleanup_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        cleanup_module.context_summary_stage_crud,
        "cleanup_expired",
        cleanup_expired,
    )

    deleted_count = await cleanup_module.cleanup_expired_context_summary_stages(
        retention_hours=24,
    )

    assert deleted_count == 201
    assert len(before_values) == 3
    assert len({before for before, _batch_size in before_values}) == 1
    assert all(batch_size == 200 for _before, batch_size in before_values)


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
