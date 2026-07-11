import asyncio

import pytest

from app.core.background_tasks.scheduler import ScheduledTaskScheduler
from app.models.profile import ProfileConfig


@pytest.mark.asyncio
async def test_scheduled_reply_slots_respect_profile_concurrency_limit():
    scheduler = ScheduledTaskScheduler()

    await scheduler._acquire_reply_slot(profile_id=1, max_concurrency=2)
    await scheduler._acquire_reply_slot(profile_id=1, max_concurrency=2)

    third_slot_acquired = asyncio.Event()

    async def acquire_third_slot():
        await scheduler._acquire_reply_slot(profile_id=1, max_concurrency=2)
        third_slot_acquired.set()

    waiting_task = asyncio.create_task(acquire_third_slot())
    await asyncio.sleep(0)

    assert not third_slot_acquired.is_set()
    assert scheduler._running_replies_by_profile == {1: 2}

    await scheduler._release_reply_slot(profile_id=1)
    await asyncio.wait_for(third_slot_acquired.wait(), timeout=1)

    assert scheduler._running_replies_by_profile == {1: 2}

    await scheduler._release_reply_slot(profile_id=1)
    await scheduler._release_reply_slot(profile_id=1)
    await waiting_task

    assert scheduler._running_replies_by_profile == {}


@pytest.mark.asyncio
async def test_scheduled_reply_slots_are_counted_separately_by_profile():
    scheduler = ScheduledTaskScheduler()

    await scheduler._acquire_reply_slot(profile_id=1, max_concurrency=1)
    await asyncio.wait_for(scheduler._acquire_reply_slot(profile_id=2, max_concurrency=1), timeout=1)

    assert scheduler._running_replies_by_profile == {1: 1, 2: 1}

    await scheduler._release_reply_slot(profile_id=1)
    await scheduler._release_reply_slot(profile_id=2)

    assert scheduler._running_replies_by_profile == {}


def test_task_concurrency_defaults_are_applied_to_existing_profile_configs():
    cfg = ProfileConfig.model_validate({"tool": {}})

    assert cfg.tool.background_task_max_concurrency == 2
    assert cfg.tool.scheduled_task_max_concurrency == 4
