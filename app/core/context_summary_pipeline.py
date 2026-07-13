import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

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


def balanced_fragment_target_tokens(total_tokens: int, max_fragment_tokens: int) -> int:
    if total_tokens <= 0:
        raise ValueError("total_tokens must be positive")
    if max_fragment_tokens <= 0:
        raise ValueError("max_fragment_tokens must be positive")
    fragment_count = max(1, (total_tokens + max_fragment_tokens - 1) // max_fragment_tokens)
    return min(
        max_fragment_tokens,
        max(1, (total_tokens + fragment_count - 1) // fragment_count),
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
        raise ValueError("expected_fragment_count must be positive")
    if not 0 <= first_fragment_index < expected_fragment_count:
        raise ValueError(
            "first_fragment_index must identify a planned fragment",
        )
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if input_queue_capacity <= 0:
        raise ValueError("input_queue_capacity must be positive")
    if result_queue_capacity <= 0:
        raise ValueError("result_queue_capacity must be positive")
    if reorder_window <= 0:
        raise ValueError("reorder_window must be positive")

    input_queue: asyncio.Queue[SummaryFragmentInput | None] = asyncio.Queue(
        maxsize=input_queue_capacity,
    )
    result_queue: asyncio.Queue[SummaryFragmentResult] = asyncio.Queue(
        maxsize=result_queue_capacity,
    )
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
                raise RuntimeError(
                    "Context summary fragments must be produced in continuous order",
                )
            if produced_count >= expected_fragment_count:
                raise RuntimeError(
                    "Context summary produced more fragments than planned",
                )
            await outstanding_slots.acquire()
            await input_queue.put(fragment)
            produced_count += 1
            max_input_queue_size = max(
                max_input_queue_size,
                input_queue.qsize(),
            )

        if produced_count != expected_fragment_count:
            raise RuntimeError(
                "Context summary produced fragment count does not match the plan",
            )
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
                    raise RuntimeError(
                        "Context summary result index does not match its input",
                    )
                await result_queue.put(result)
                max_result_queue_size = max(
                    max_result_queue_size,
                    result_queue.qsize(),
                )
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
                    raise RuntimeError(
                        "Context summary returned a duplicate fragment",
                    )
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
            raise RuntimeError(
                "Context summary retained unexpected out-of-order results",
            )

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
