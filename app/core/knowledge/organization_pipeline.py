from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.core.constants import (
    KNOWLEDGE_ORGANIZATION_FRAGMENT_CONCURRENCY,
    KNOWLEDGE_ORGANIZATION_INPUT_QUEUE_CAPACITY,
    KNOWLEDGE_ORGANIZATION_REORDER_WINDOW,
    KNOWLEDGE_ORGANIZATION_RESULT_QUEUE_CAPACITY,
)
from app.core.i18n import t
from app.core.knowledge.organization_types import KnowledgeOrganizationPipelineStats

__all__ = [
    "run_bounded_knowledge_organization_pipeline",
    "maybe_await",
]


def _item_index(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    fragment_index = getattr(value, "fragment_index", None)
    if isinstance(fragment_index, int) and not isinstance(fragment_index, bool):
        return fragment_index
    if isinstance(value, tuple) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
        return value[0]
    raise ValueError(t("organization pipeline item has no integer index"))


async def run_bounded_knowledge_organization_pipeline[InputT, ResultT](
    *,
    inputs: AsyncIterator[InputT],
    expected_count: int,
    process: Callable[[InputT], Awaitable[ResultT]],
    persist: Callable[[ResultT], Awaitable[None]],
    first_index: int = 0,
    concurrency: int = KNOWLEDGE_ORGANIZATION_FRAGMENT_CONCURRENCY,
    input_queue_capacity: int = KNOWLEDGE_ORGANIZATION_INPUT_QUEUE_CAPACITY,
    result_queue_capacity: int = KNOWLEDGE_ORGANIZATION_RESULT_QUEUE_CAPACITY,
    reorder_window: int = KNOWLEDGE_ORGANIZATION_REORDER_WINDOW,
) -> KnowledgeOrganizationPipelineStats:
    if expected_count <= 0 or not 0 <= first_index <= expected_count:
        raise ValueError(t("invalid organization pipeline range"))
    if concurrency <= 0 or input_queue_capacity <= 0 or result_queue_capacity <= 0 or reorder_window <= 0:
        raise ValueError(t("invalid organization pipeline capacity"))
    if first_index == expected_count:
        return KnowledgeOrganizationPipelineStats(0, 0, 0, 0)

    input_queue: asyncio.Queue[InputT | None] = asyncio.Queue(maxsize=input_queue_capacity)
    result_queue: asyncio.Queue[ResultT] = asyncio.Queue(maxsize=result_queue_capacity)
    outstanding_slots = asyncio.Semaphore(reorder_window + 1)
    active_tasks = 0
    max_active_tasks = 0
    max_input_queue_size = 0
    max_result_queue_size = 0
    max_reorder_size = 0

    async def produce() -> None:
        nonlocal max_input_queue_size
        produced_count = first_index
        async for item in inputs:
            if _item_index(item) != produced_count or produced_count >= expected_count:
                raise RuntimeError(t("organization input fragment order mismatch"))
            await outstanding_slots.acquire()
            await input_queue.put(item)
            produced_count += 1
            max_input_queue_size = max(max_input_queue_size, input_queue.qsize())
        if produced_count != expected_count:
            raise RuntimeError(t("organization input fragment count mismatch"))
        for _ in range(concurrency):
            await input_queue.put(None)

    async def work() -> None:
        nonlocal active_tasks, max_active_tasks, max_result_queue_size
        while True:
            item = await input_queue.get()
            try:
                if item is None:
                    return
                active_tasks += 1
                max_active_tasks = max(max_active_tasks, active_tasks)
                try:
                    result = await process(item)
                finally:
                    active_tasks -= 1
                if _item_index(result) != _item_index(item):
                    raise RuntimeError(t("organization result fragment index mismatch"))
                await result_queue.put(result)
                max_result_queue_size = max(max_result_queue_size, result_queue.qsize())
            finally:
                input_queue.task_done()

    async def persist_in_order() -> None:
        nonlocal max_reorder_size
        next_index = first_index
        reorder_buffer: dict[int, ResultT] = {}
        while next_index < expected_count:
            result = await result_queue.get()
            try:
                result_index = _item_index(result)
                if result_index < next_index or result_index in reorder_buffer:
                    raise RuntimeError(t("organization result fragment duplicated"))
                reorder_buffer[result_index] = result
                max_reorder_size = max(max_reorder_size, sum(index > next_index for index in reorder_buffer))
                while next_index in reorder_buffer:
                    ordered = reorder_buffer.pop(next_index)
                    await persist(ordered)
                    outstanding_slots.release()
                    next_index += 1
            finally:
                result_queue.task_done()
        if reorder_buffer:
            raise RuntimeError(t("organization reorder buffer not empty"))

    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(produce())
        for _ in range(concurrency):
            task_group.create_task(work())
        task_group.create_task(persist_in_order())

    return KnowledgeOrganizationPipelineStats(
        max_active_tasks=max_active_tasks,
        max_input_queue_size=max_input_queue_size,
        max_result_queue_size=max_result_queue_size,
        max_reorder_size=max_reorder_size,
    )


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
