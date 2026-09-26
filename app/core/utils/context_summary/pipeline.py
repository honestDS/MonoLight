import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass

from app.core.constants import (
    ERR_CONTEXT_SUMMARY_CHUNK_OVER_BUDGET,
    ERR_CONTEXT_SUMMARY_FRAGMENT_COUNT_EXCEEDED,
    ERR_CONTEXT_SUMMARY_FRAGMENT_COUNT_MISMATCH,
    ERR_CONTEXT_SUMMARY_FRAGMENT_DUPLICATED,
    ERR_CONTEXT_SUMMARY_FRAGMENT_INDEX_UNPLANNED,
    ERR_CONTEXT_SUMMARY_FRAGMENT_ORDER_INVALID,
    ERR_CONTEXT_SUMMARY_REORDER_RESULTS_RETAINED,
    ERR_CONTEXT_SUMMARY_RESULT_INDEX_MISMATCH,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.i18n import t

CONTEXT_SUMMARY_FRAGMENT_CONCURRENCY = 4
CONTEXT_SUMMARY_INPUT_QUEUE_CAPACITY = 8
CONTEXT_SUMMARY_RESULT_QUEUE_CAPACITY = 8
CONTEXT_SUMMARY_REORDER_WINDOW = 8


@dataclass(frozen=True)
class SummaryFragmentInput:
    fragment_index: int
    message_start_id: int
    message_end_id: int
    token_count: int
    content: str
    existing_summary: str | None = None


@dataclass(frozen=True)
class SummaryFragmentResult:
    fragment_index: int
    message_start_id: int
    message_end_id: int
    content: str
    token_count: int


@dataclass(frozen=True)
class SummaryFragmentPlan:
    unit_counts: tuple[int, ...]
    token_counts: tuple[int, ...]

    @property
    def fragment_count(self) -> int:
        return len(self.unit_counts)


@dataclass(frozen=True)
class SummaryPipelineStats:
    max_active_tasks: int
    max_input_queue_size: int
    max_result_queue_size: int
    max_reorder_size: int


ProcessFragment = Callable[
    [SummaryFragmentInput],
    Awaitable[SummaryFragmentResult],
]
PersistFragment = Callable[[SummaryFragmentResult], Awaitable[None]]


def build_balanced_fragment_plan(
    unit_token_counts: Iterable[int],
    *,
    max_fragment_tokens: int,
) -> SummaryFragmentPlan:
    if max_fragment_tokens <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="max_fragment_tokens"))

    token_counts = tuple(unit_token_counts)
    if not token_counts:
        return SummaryFragmentPlan(unit_counts=(), token_counts=())
    for token_count in token_counts:
        if token_count <= 0:
            raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="unit_token_count"))
        if token_count > max_fragment_tokens:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_CHUNK_OVER_BUDGET))

    unit_count = len(token_counts)
    next_fragment_start = [0] * unit_count
    window_end = 0
    window_tokens = 0
    for window_start in range(unit_count):
        while window_end < unit_count and window_tokens + token_counts[window_end] <= max_fragment_tokens:
            window_tokens += token_counts[window_end]
            window_end += 1
        next_fragment_start[window_start] = window_end
        window_tokens -= token_counts[window_start]

    minimum_fragments_from = [0] * (unit_count + 1)
    for index in range(unit_count - 1, -1, -1):
        minimum_fragments_from[index] = 1 + minimum_fragments_from[next_fragment_start[index]]

    fragment_count = minimum_fragments_from[0]
    planned_unit_counts: list[int] = []
    planned_token_counts: list[int] = []
    start = 0
    remaining_tokens = sum(token_counts)

    for fragments_left in range(fragment_count, 1, -1):
        dynamic_target = remaining_tokens / fragments_left
        running_tokens = 0
        best_end: int | None = None
        best_tokens = 0
        best_distance = float("inf")
        latest_end = unit_count - (fragments_left - 1)

        for end in range(start + 1, latest_end + 1):
            running_tokens += token_counts[end - 1]
            if running_tokens > max_fragment_tokens:
                break
            if minimum_fragments_from[end] > fragments_left - 1:
                continue

            distance = abs(running_tokens - dynamic_target)
            if distance < best_distance or (distance == best_distance and running_tokens > best_tokens):
                best_end = end
                best_tokens = running_tokens
                best_distance = distance

            if running_tokens >= dynamic_target and best_end is not None:
                break

        if best_end is None:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_COUNT_MISMATCH))

        planned_unit_counts.append(best_end - start)
        planned_token_counts.append(best_tokens)
        start = best_end
        remaining_tokens -= best_tokens

    final_tokens = remaining_tokens
    if start >= unit_count or final_tokens > max_fragment_tokens:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_COUNT_MISMATCH))
    planned_unit_counts.append(unit_count - start)
    planned_token_counts.append(final_tokens)

    return SummaryFragmentPlan(
        unit_counts=tuple(planned_unit_counts),
        token_counts=tuple(planned_token_counts),
    )


async def run_bounded_fragment_pipeline(
    *,
    fragments: AsyncIterator[SummaryFragmentInput],
    expected_fragment_count: int,
    process_fragment: ProcessFragment,
    first_fragment_index: int = 0,
    persist_fragment: PersistFragment,
    concurrency: int = CONTEXT_SUMMARY_FRAGMENT_CONCURRENCY,
    input_queue_capacity: int = CONTEXT_SUMMARY_INPUT_QUEUE_CAPACITY,
    result_queue_capacity: int = CONTEXT_SUMMARY_RESULT_QUEUE_CAPACITY,
    reorder_window: int = CONTEXT_SUMMARY_REORDER_WINDOW,
) -> SummaryPipelineStats:
    if expected_fragment_count <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="expected_fragment_count"))
    if not 0 <= first_fragment_index < expected_fragment_count:
        raise ValueError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_INDEX_UNPLANNED))
    if concurrency <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="concurrency"))
    if input_queue_capacity <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="input_queue_capacity"))
    if result_queue_capacity <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="result_queue_capacity"))
    if reorder_window <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="reorder_window"))

    input_queue: asyncio.Queue[SummaryFragmentInput | None] = asyncio.Queue(maxsize=input_queue_capacity)
    result_queue: asyncio.Queue[SummaryFragmentResult] = asyncio.Queue(maxsize=result_queue_capacity)
    outstanding_slots = asyncio.Semaphore(reorder_window + 1)
    active_tasks = 0
    max_active_tasks = 0
    max_input_queue_size = 0
    max_result_queue_size = 0
    max_reorder_size = 0

    async def produce() -> None:
        nonlocal max_input_queue_size
        produced_count = first_fragment_index
        async for fragment in fragments:
            if fragment.fragment_index != produced_count:
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_ORDER_INVALID))
            if produced_count >= expected_fragment_count:
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_COUNT_EXCEEDED))
            await outstanding_slots.acquire()
            await input_queue.put(fragment)
            produced_count += 1
            max_input_queue_size = max(max_input_queue_size, input_queue.qsize())

        if produced_count != expected_fragment_count:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_COUNT_MISMATCH))
        for _ in range(concurrency):
            await input_queue.put(None)

    async def work() -> None:
        nonlocal active_tasks, max_active_tasks, max_result_queue_size
        while True:
            fragment = await input_queue.get()
            try:
                if fragment is None:
                    return
                active_tasks += 1
                max_active_tasks = max(max_active_tasks, active_tasks)
                try:
                    result = await process_fragment(fragment)
                finally:
                    active_tasks -= 1
                if result.fragment_index != fragment.fragment_index:
                    raise RuntimeError(t(ERR_CONTEXT_SUMMARY_RESULT_INDEX_MISMATCH))
                await result_queue.put(result)
                max_result_queue_size = max(max_result_queue_size, result_queue.qsize())
            finally:
                input_queue.task_done()

    async def persist_in_order() -> None:
        nonlocal max_reorder_size
        next_fragment_index = first_fragment_index
        reorder_buffer: dict[int, SummaryFragmentResult] = {}
        while next_fragment_index < expected_fragment_count:
            result = await result_queue.get()
            try:
                if result.fragment_index < next_fragment_index or result.fragment_index in reorder_buffer:
                    raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_DUPLICATED))
                reorder_buffer[result.fragment_index] = result
                waiting_count = sum(index > next_fragment_index for index in reorder_buffer)
                max_reorder_size = max(max_reorder_size, waiting_count)

                while next_fragment_index in reorder_buffer:
                    ordered_result = reorder_buffer.pop(next_fragment_index)
                    await persist_fragment(ordered_result)
                    outstanding_slots.release()
                    next_fragment_index += 1
            finally:
                result_queue.task_done()

        if reorder_buffer:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_REORDER_RESULTS_RETAINED))

    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(produce())
        for _ in range(concurrency):
            task_group.create_task(work())
        task_group.create_task(persist_in_order())

    return SummaryPipelineStats(
        max_active_tasks=max_active_tasks,
        max_input_queue_size=max_input_queue_size,
        max_result_queue_size=max_result_queue_size,
        max_reorder_size=max_reorder_size,
    )
