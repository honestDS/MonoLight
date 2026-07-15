import asyncio

import pytest

from app.core import constants
from app.core.i18n import t
from app.core.utils.context_summary.pipeline import (
    SummaryFragmentInput,
    SummaryFragmentResult,
    balanced_fragment_target_tokens,
    run_bounded_fragment_pipeline,
)


def test_balanced_fragment_target_uses_minimum_safe_fragment_count():
    assert balanced_fragment_target_tokens(100, 30) == 25
    assert balanced_fragment_target_tokens(91, 30) == 23
    assert balanced_fragment_target_tokens(30, 30) == 30


@pytest.mark.parametrize(
    ("total_tokens", "max_fragment_tokens"),
    [(0, 10), (10, 0), (-1, 10), (10, -1)],
)
def test_balanced_fragment_target_rejects_invalid_budgets(
    total_tokens,
    max_fragment_tokens,
):
    with pytest.raises(ValueError):
        balanced_fragment_target_tokens(
            total_tokens,
            max_fragment_tokens,
        )


@pytest.mark.asyncio
async def test_pipeline_limits_concurrency_and_reorders_before_persisting():
    fragment_count = 20
    first_fragment_gate = asyncio.Event()
    first_fragment_started = asyncio.Event()
    completed_out_of_order = asyncio.Event()
    active_tasks = 0
    max_active_tasks = 0
    started_indices: list[int] = []
    completed_indices: list[int] = []
    persisted_indices: list[int] = []
    yielded_count = 0

    async def fragments():
        nonlocal yielded_count
        for index in range(fragment_count):
            yielded_count += 1
            yield SummaryFragmentInput(
                fragment_index=index,
                message_start_id=index * 2 + 1,
                message_end_id=index * 2 + 2,
                token_count=100,
                content=f"fragment-{index}",
            )

    async def process(fragment):
        nonlocal active_tasks, max_active_tasks
        active_tasks += 1
        max_active_tasks = max(max_active_tasks, active_tasks)
        started_indices.append(fragment.fragment_index)
        try:
            if fragment.fragment_index == 0:
                first_fragment_started.set()
                await first_fragment_gate.wait()
            else:
                await asyncio.sleep(0)
                completed_indices.append(fragment.fragment_index)
                if len(completed_indices) >= 3:
                    completed_out_of_order.set()
            return SummaryFragmentResult(
                fragment_index=fragment.fragment_index,
                message_start_id=fragment.message_start_id,
                message_end_id=fragment.message_end_id,
                content=f"summary-{fragment.fragment_index}",
                token_count=10,
            )
        finally:
            active_tasks -= 1

    async def persist(result):
        persisted_indices.append(result.fragment_index)
        await asyncio.sleep(0)

    pipeline_task = asyncio.create_task(
        run_bounded_fragment_pipeline(
            fragments=fragments(),
            expected_fragment_count=fragment_count,
            process_fragment=process,
            persist_fragment=persist,
            concurrency=4,
            input_queue_capacity=2,
            result_queue_capacity=2,
            reorder_window=3,
        )
    )

    await asyncio.wait_for(first_fragment_started.wait(), timeout=1)
    await asyncio.wait_for(completed_out_of_order.wait(), timeout=1)
    await asyncio.sleep(0)

    assert persisted_indices == []
    assert set(started_indices) == {0, 1, 2, 3}
    assert yielded_count <= 5

    first_fragment_gate.set()
    stats = await asyncio.wait_for(pipeline_task, timeout=2)

    assert persisted_indices == list(range(fragment_count))
    assert max_active_tasks <= 4
    assert stats.max_active_tasks <= 4
    assert stats.max_input_queue_size <= 2
    assert stats.max_result_queue_size <= 2
    assert stats.max_reorder_size == 3


@pytest.mark.asyncio
async def test_pipeline_persists_completed_prefix_before_the_stage_finishes():
    last_fragment_gate = asyncio.Event()
    first_fragment_persisted = asyncio.Event()
    persisted_indices: list[int] = []

    async def fragments():
        for index in range(3):
            yield SummaryFragmentInput(
                fragment_index=index,
                message_start_id=index * 2 + 1,
                message_end_id=index * 2 + 2,
                token_count=100,
                content=f"fragment-{index}",
            )

    async def process(fragment):
        if fragment.fragment_index == 2:
            await last_fragment_gate.wait()
        return SummaryFragmentResult(
            fragment_index=fragment.fragment_index,
            message_start_id=fragment.message_start_id,
            message_end_id=fragment.message_end_id,
            content=f"summary-{fragment.fragment_index}",
            token_count=10,
        )

    async def persist(result):
        persisted_indices.append(result.fragment_index)
        if result.fragment_index == 0:
            first_fragment_persisted.set()

    pipeline_task = asyncio.create_task(
        run_bounded_fragment_pipeline(
            fragments=fragments(),
            expected_fragment_count=3,
            process_fragment=process,
            persist_fragment=persist,
            concurrency=3,
            input_queue_capacity=2,
            result_queue_capacity=2,
            reorder_window=2,
        )
    )

    await asyncio.wait_for(first_fragment_persisted.wait(), timeout=1)
    assert pipeline_task.done() is False
    assert persisted_indices[:1] == [0]

    last_fragment_gate.set()
    await asyncio.wait_for(pipeline_task, timeout=1)

    assert persisted_indices == [0, 1, 2]


@pytest.mark.asyncio
async def test_pipeline_resumes_after_an_already_persisted_prefix():
    processed_indices: list[int] = []
    persisted_indices: list[int] = []

    async def fragments():
        for index in range(2, 5):
            yield SummaryFragmentInput(
                fragment_index=index,
                message_start_id=index * 2 + 1,
                message_end_id=index * 2 + 2,
                token_count=100,
                content=f"fragment-{index}",
            )

    async def process(fragment):
        processed_indices.append(fragment.fragment_index)
        return SummaryFragmentResult(
            fragment_index=fragment.fragment_index,
            message_start_id=fragment.message_start_id,
            message_end_id=fragment.message_end_id,
            content=f"summary-{fragment.fragment_index}",
            token_count=10,
        )

    async def persist(result):
        persisted_indices.append(result.fragment_index)

    await run_bounded_fragment_pipeline(
        fragments=fragments(),
        expected_fragment_count=5,
        process_fragment=process,
        first_fragment_index=2,
        persist_fragment=persist,
        concurrency=2,
    )

    assert sorted(processed_indices) == [2, 3, 4]
    assert persisted_indices == [2, 3, 4]


@pytest.mark.asyncio
async def test_pipeline_rejects_non_contiguous_fragment_plan():
    async def fragments():
        yield SummaryFragmentInput(
            fragment_index=1,
            message_start_id=1,
            message_end_id=2,
            token_count=10,
            content="fragment",
        )

    async def process(fragment):
        return SummaryFragmentResult(
            fragment_index=fragment.fragment_index,
            message_start_id=fragment.message_start_id,
            message_end_id=fragment.message_end_id,
            content="summary",
            token_count=1,
        )

    async def persist(_result):
        return None

    with pytest.raises(ExceptionGroup) as exc_info:
        await run_bounded_fragment_pipeline(
            fragments=fragments(),
            expected_fragment_count=1,
            process_fragment=process,
            persist_fragment=persist,
        )

    assert any(
        str(error) == t(constants.ERR_CONTEXT_SUMMARY_FRAGMENT_ORDER_INVALID)
        for error in exc_info.value.exceptions
    )


@pytest.mark.asyncio
async def test_pipeline_cancels_inflight_tasks_after_fragment_failure():
    blocking_started = asyncio.Event()
    blocking_cancelled = asyncio.Event()
    persisted_indices: list[int] = []

    async def fragments():
        for index in range(2):
            yield SummaryFragmentInput(
                fragment_index=index,
                message_start_id=index + 1,
                message_end_id=index + 1,
                token_count=100,
                content=f"fragment-{index}",
            )

    async def process(fragment):
        if fragment.fragment_index == 0:
            await blocking_started.wait()
            raise RuntimeError("selected model failed")

        blocking_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            blocking_cancelled.set()
            raise

    async def persist(result):
        persisted_indices.append(result.fragment_index)

    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(
            run_bounded_fragment_pipeline(
                fragments=fragments(),
                expected_fragment_count=2,
                process_fragment=process,
                persist_fragment=persist,
                concurrency=2,
            ),
            timeout=1,
        )

    assert blocking_cancelled.is_set()
    assert persisted_indices == []
