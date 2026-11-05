import asyncio
import time
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.session_reply_queue.consumer as consumer_module
import app.models  # noqa: F401
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.system_setting import SystemSetting
from app.models.user import User
from tests.integration.session_reply_queue_test_support import AsyncBarrier

UID = "agent-work-user"


@pytest_asyncio.fixture
async def agent_work_database(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        tables=[PromptLibrary.__table__, Profile.__table__, ChatSession.__table__, SessionReplySequence.__table__, SessionReplyWorkItem.__table__],
        foreign_keys=True,
        wal=True,
        name="agent-work-concurrency.sqlite3",
    ) as factory:
        async with factory() as db:
            db.add_all(
                [
                    Profile(id=1, uid=UID, name="profile-one", configs={}),
                    Profile(id=2, uid=UID, name="profile-two", configs={}),
                    ChatSession(session_id="session-1", uid=UID, profile_id=1),
                    ChatSession(session_id="session-2", uid=UID, profile_id=1),
                    ChatSession(session_id="session-3", uid=UID, profile_id=2),
                    ChatSession(session_id="session-4", uid=UID, profile_id=2),
                ]
            )
            await db.commit()
        yield factory


async def _enqueue(
    db: AsyncSession,
    session_id: str,
    profile_id: int,
    work_type: SessionReplyWorkType,
    source_type: SessionReplySourceType,
    source_id: str,
    dedupe_key: str,
    work_category: SessionReplyWorkCategory | None = None,
) -> SessionReplyWorkItem:
    work, created = await CRUDSessionReplyWorkItem().enqueue(
        db,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        work_type=work_type,
        source_type=source_type,
        source_id=source_id,
        dedupe_key=dedupe_key,
        work_category=work_category,
        commit=False,
    )
    assert created is True and work.id is not None
    return work


async def _seed(factory, specs: list[tuple]) -> list[SessionReplyWorkItem]:
    async with factory() as db:
        works = [await _enqueue(db, *spec) for spec in specs]
        await db.commit()
        return works


async def _claim(
    factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    *,
    independent_only: bool | None = None,
    agent_work_max_concurrency: int = 4,
    scheduled_profile_limits: dict[int, int] | None = None,
    subagent_profile_limits: dict[int, int] | None = None,
    exclude_work_ids: set[int] | None = None,
) -> SessionReplyWorkItem | None:
    async with factory() as db:
        return await CRUDSessionReplyWorkItem().claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
            independent_only=independent_only,
            agent_work_max_concurrency=agent_work_max_concurrency,
            scheduled_profile_limits=scheduled_profile_limits,
            subagent_profile_limits=subagent_profile_limits,
            exclude_work_ids=exclude_work_ids,
        )


def _check_claim(claim, work_id: int, category: SessionReplyWorkCategory, worker_id: str) -> SessionReplyWorkItem:
    assert claim is not None
    assert (claim.id, claim.work_category, claim.status, claim.locked_by) == (
        work_id,
        category,
        SessionReplyWorkStatus.RUNNING,
        worker_id,
    )
    assert claim.lock_until is not None
    return claim


async def _get(factory: async_sessionmaker[AsyncSession], work_id: int) -> SessionReplyWorkItem:
    async with factory() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        assert work is not None
        return work


async def _finish(factory, work_id: int, worker_id: str, status=SessionReplyWorkStatus.SUCCEEDED) -> None:
    async with factory() as db:
        assert await CRUDSessionReplyWorkItem().mark_terminal(db, work_id=work_id, worker_id=worker_id, status=status)


async def _assert_ready(factory, work_id: int, category: SessionReplyWorkCategory | None = None) -> SessionReplyWorkItem:
    work = await _get(factory, work_id)
    assert (work.status, work.locked_by, work.lock_until) == (SessionReplyWorkStatus.READY_FOR_LLM, None, None)
    if category is not None:
        assert work.work_category == category
    return work


@pytest.mark.asyncio
async def test_foreground_claims_ignore_independent_capacity_and_keep_session_order(agent_work_database):
    specs = [
        ("session-3", 2, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.SCHEDULED_TASK_RUN, "scheduled-one", "fg-independent-one"),
        ("session-2", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "parent-two", "fg-independent-two"),
        ("session-1", 1, SessionReplyWorkType.FOREGROUND_REPLY, SessionReplySourceType.USER_MESSAGE, "message-one", "fg-one"),
        ("session-1", 1, SessionReplyWorkType.FOREGROUND_REPLY, SessionReplySourceType.USER_MESSAGE, "message-two", "fg-two"),
        ("session-2", 1, SessionReplyWorkType.FOREGROUND_REPLY, SessionReplySourceType.USER_MESSAGE, "message-three", "fg-other"),
        ("session-3", 2, SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION, SessionReplySourceType.AUDIT_RECORD, "audit-one", "fg-confirmed", SessionReplyWorkCategory.FOREGROUND),
    ]
    independent_one, independent_two, foreground_one, foreground_two, other_foreground, confirmed = await _seed(agent_work_database, specs)

    _check_claim(
        await _claim(agent_work_database, "independent-worker-one", independent_only=True, agent_work_max_concurrency=1),
        independent_one.id,
        SessionReplyWorkCategory.SCHEDULED_TASK,
        "independent-worker-one",
    )
    _check_claim(
        await _claim(agent_work_database, "foreground-worker-one", independent_only=False, agent_work_max_concurrency=1),
        foreground_one.id,
        SessionReplyWorkCategory.FOREGROUND,
        "foreground-worker-one",
    )
    _check_claim(
        await _claim(agent_work_database, "foreground-worker-two", independent_only=False, agent_work_max_concurrency=1),
        other_foreground.id,
        SessionReplyWorkCategory.FOREGROUND,
        "foreground-worker-two",
    )
    confirmed_claim = _check_claim(
        await _claim(agent_work_database, "confirmed-worker", independent_only=False, agent_work_max_concurrency=1),
        confirmed.id,
        SessionReplyWorkCategory.FOREGROUND,
        "confirmed-worker",
    )
    assert confirmed_claim.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert confirmed_claim.source_type == SessionReplySourceType.AUDIT_RECORD

    assert await _claim(agent_work_database, "foreground-worker-blocked", independent_only=False, agent_work_max_concurrency=1) is None
    await _assert_ready(agent_work_database, foreground_two.id, SessionReplyWorkCategory.FOREGROUND)
    assert await _claim(agent_work_database, "independent-worker-blocked", independent_only=True, agent_work_max_concurrency=1) is None
    await _assert_ready(agent_work_database, independent_two.id, SessionReplyWorkCategory.SUBAGENT)

    await _finish(agent_work_database, foreground_one.id, "foreground-worker-one")
    _check_claim(
        await _claim(agent_work_database, "foreground-worker-three", independent_only=False, agent_work_max_concurrency=1),
        foreground_two.id,
        SessionReplyWorkCategory.FOREGROUND,
        "foreground-worker-three",
    )
    await _finish(agent_work_database, independent_one.id, "independent-worker-one")
    _check_claim(
        await _claim(agent_work_database, "independent-worker-two", independent_only=True, agent_work_max_concurrency=1),
        independent_two.id,
        SessionReplyWorkCategory.SUBAGENT,
        "independent-worker-two",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2], ids=["global-one", "global-two"])
async def test_concurrent_independent_claims_use_database_global_limit(agent_work_database, limit: int):
    specs = [
        ("session-1", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.SCHEDULED_TASK_RUN, "source-0", "global-0"),
        ("session-2", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "source-1", "global-1"),
        ("session-3", 2, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.SCHEDULED_TASK_RUN, "source-2", "global-2"),
        ("session-4", 2, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "source-3", "global-3"),
    ]
    works = await _seed(agent_work_database, specs)
    barrier = AsyncBarrier(len(works))

    async def claim_concurrently(index: int):
        async with agent_work_database() as db:
            await barrier.wait()
            return await CRUDSessionReplyWorkItem().claim_next(
                db,
                worker_id=f"global-worker-{index}",
                lease_seconds=300,
                independent_only=True,
                agent_work_max_concurrency=limit,
            )

    claims = await asyncio.gather(*(claim_concurrently(index) for index in range(len(works))))
    claimed = [work for work in claims if work is not None]
    assert len(claimed) == limit
    assert len({work.id for work in claimed}) == limit
    assert all(work.status == SessionReplyWorkStatus.RUNNING and work.locked_by for work in claimed)
    assert {work.work_category for work in claimed} <= {SessionReplyWorkCategory.SCHEDULED_TASK, SessionReplyWorkCategory.SUBAGENT}

    claimed_ids = {work.id for work in claimed}
    for work in works:
        persisted = await _get(agent_work_database, work.id)
        if work.id in claimed_ids:
            assert persisted.status == SessionReplyWorkStatus.RUNNING and persisted.locked_by is not None
        else:
            await _assert_ready(agent_work_database, work.id)

    released = claimed[0]
    assert released.id is not None and released.locked_by is not None
    await _finish(agent_work_database, released.id, released.locked_by)
    next_claim = await _claim(agent_work_database, "global-next-worker", independent_only=True, agent_work_max_concurrency=limit)
    assert next_claim is not None and next_claim.id in {work.id for work in works if work.id not in claimed_ids}
    assert next_claim.status == SessionReplyWorkStatus.RUNNING and next_claim.locked_by == "global-next-worker"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    [SessionReplyWorkCategory.SCHEDULED_TASK, SessionReplyWorkCategory.SUBAGENT],
    ids=["scheduled-profile-limit", "subagent-profile-limit"],
)
async def test_category_profile_limits_cover_confirmations_and_global_capacity(agent_work_database, category):
    source_type = SessionReplySourceType.SCHEDULED_TASK_RUN if category == SessionReplyWorkCategory.SCHEDULED_TASK else SessionReplySourceType.PARENT_WORK
    other_category = SessionReplyWorkCategory.SUBAGENT if category == SessionReplyWorkCategory.SCHEDULED_TASK else SessionReplyWorkCategory.SCHEDULED_TASK
    other_source_type = SessionReplySourceType.PARENT_WORK if category == SessionReplyWorkCategory.SCHEDULED_TASK else SessionReplySourceType.SCHEDULED_TASK_RUN
    specs = [
        ("session-1", 1, SessionReplyWorkType.AGENT_WORK, source_type, "original", f"profile-original-{category.value}"),
        ("session-1", 1, SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION, SessionReplySourceType.AUDIT_RECORD, "audit", f"profile-confirmation-{category.value}", category),
        ("session-2", 1, SessionReplyWorkType.AGENT_WORK, other_source_type, "other", f"profile-other-{category.value}"),
        *[(f"session-{3 + index % 2}", 2, SessionReplyWorkType.AGENT_WORK, source_type, f"profile-two-{index}", f"profile-two-{category.value}-{index}") for index in range(3)],
    ]
    original, confirmation, other, profile_two_one, profile_two_two, profile_two_three = await _seed(agent_work_database, specs)
    args = {"independent_only": True, "agent_work_max_concurrency": 3, "scheduled_profile_limits": {1: 1, 2: 2}, "subagent_profile_limits": {1: 1, 2: 2}}

    _check_claim(await _claim(agent_work_database, "profile-original-worker", **args), original.id, category, "profile-original-worker")
    _check_claim(await _claim(agent_work_database, "profile-other-worker", **args), other.id, other_category, "profile-other-worker")
    first_profile_two_claim = _check_claim(await _claim(agent_work_database, "profile-two-worker-one", **args), profile_two_one.id, category, "profile-two-worker-one")
    assert first_profile_two_claim.profile_id == 2
    assert await _claim(agent_work_database, "profile-global-full", **args) is None
    await _assert_ready(agent_work_database, confirmation.id, category)
    await _assert_ready(agent_work_database, profile_two_two.id, category)
    await _assert_ready(agent_work_database, profile_two_three.id, category)

    await _finish(agent_work_database, other.id, "profile-other-worker")
    _check_claim(await _claim(agent_work_database, "profile-two-worker-two", **args), profile_two_two.id, category, "profile-two-worker-two")
    await _finish(agent_work_database, original.id, "profile-original-worker")
    confirmation_claim = _check_claim(await _claim(agent_work_database, "confirmation-worker", **args), confirmation.id, category, "confirmation-worker")
    assert (confirmation_claim.work_type, confirmation_claim.source_type, confirmation_claim.profile_id) == (
        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        SessionReplySourceType.AUDIT_RECORD,
        1,
    )
    await _finish(agent_work_database, confirmation.id, "confirmation-worker")
    assert await _claim(agent_work_database, "profile-two-limit", **args) is None
    await _assert_ready(agent_work_database, profile_two_three.id, category)


@pytest.mark.asyncio
async def test_waiting_completed_expired_and_excluded_work_do_not_consume_capacity(agent_work_database):
    completed, waiting, expired = await _seed(
        agent_work_database,
        [
            ("session-1", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "completed", "boundary-completed"),
            ("session-2", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.SCHEDULED_TASK_RUN, "waiting", "boundary-waiting"),
            ("session-3", 2, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "expired", "boundary-expired"),
        ],
    )
    _check_claim(await _claim(agent_work_database, "completed-worker", independent_only=True, agent_work_max_concurrency=1), completed.id, SessionReplyWorkCategory.SUBAGENT, "completed-worker")
    await _finish(agent_work_database, completed.id, "completed-worker")
    done = await _get(agent_work_database, completed.id)
    assert (done.status, done.locked_by, done.lock_until) == (SessionReplyWorkStatus.SUCCEEDED, None, None)

    async with agent_work_database() as db:
        waiting_row = await db.get(SessionReplyWorkItem, waiting.id)
        expired_row = await db.get(SessionReplyWorkItem, expired.id)
        assert waiting_row is not None and expired_row is not None
        waiting_row.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        waiting_row.locked_by = waiting_row.lock_until = None
        expired_row.status = SessionReplyWorkStatus.RUNNING
        expired_row.locked_by = "expired-worker"
        expired_row.lock_until = int(time.time()) - 1
        await db.commit()

    blocker = (await _seed(agent_work_database, [("session-4", 2, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.SCHEDULED_TASK_RUN, "blocker", "boundary-blocker")]))[0]
    blocker_claim = _check_claim(await _claim(agent_work_database, "blocker-worker", independent_only=True, agent_work_max_concurrency=1), blocker.id, SessionReplyWorkCategory.SCHEDULED_TASK, "blocker-worker")
    expired_row, waiting_row = await _get(agent_work_database, expired.id), await _get(agent_work_database, waiting.id)
    assert expired_row.status == SessionReplyWorkStatus.RUNNING and expired_row.lock_until is not None and expired_row.lock_until < int(time.time())
    assert waiting_row.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK and waiting_row.locked_by is None

    waiting_category = waiting_row.work_category
    async with agent_work_database() as db:
        waiting_for_wake = await db.get(SessionReplyWorkItem, waiting.id)
        assert waiting_for_wake is not None
        assert await external_work_crud.wake_waiting(db, work=waiting_for_wake, execution_state={"resumed": True}, commit=True)
    woken = await _get(agent_work_database, waiting.id)
    assert (woken.status, woken.work_category, woken.locked_by, woken.lock_until) == (
        SessionReplyWorkStatus.READY_FOR_LLM,
        waiting_category,
        None,
        None,
    )
    assert await _claim(agent_work_database, "waiting-still-blocked", independent_only=True, agent_work_max_concurrency=1) is None

    assert blocker_claim.id is not None and blocker_claim.locked_by is not None
    await _finish(agent_work_database, blocker.id, "blocker-worker")
    _check_claim(await _claim(agent_work_database, "waiting-resume-worker", independent_only=True, agent_work_max_concurrency=1), waiting.id, SessionReplyWorkCategory.SCHEDULED_TASK, "waiting-resume-worker")
    await _finish(agent_work_database, waiting.id, "waiting-resume-worker")

    excluded, fallback = await _seed(
        agent_work_database,
        [
            ("session-1", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "excluded", "boundary-excluded"),
            ("session-2", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "fallback", "boundary-fallback"),
        ],
    )
    _check_claim(
        await _claim(agent_work_database, "exclude-worker", independent_only=True, agent_work_max_concurrency=1, exclude_work_ids={excluded.id}),
        fallback.id,
        SessionReplyWorkCategory.SUBAGENT,
        "exclude-worker",
    )
    await _assert_ready(agent_work_database, excluded.id, SessionReplyWorkCategory.SUBAGENT)


@pytest.mark.asyncio
async def test_consumer_run_dispatches_real_claims_with_global_and_profile_limits(database_factory, monkeypatch):
    profile_one_config = ProfileConfig.model_validate({}).model_dump(mode="json")
    profile_one_config["tool"]["scheduled_task_max_concurrency"] = 1
    profile_one_config["tool"]["subagent_max_concurrency"] = 1
    profile_two_config = ProfileConfig.model_validate({}).model_dump(mode="json")
    profile_two_config["tool"]["subagent_max_concurrency"] = 2

    async with database_factory(foreign_keys=True, wal=True, name="agent-work-consumer.sqlite3") as factory:
        async with factory() as db:
            db.add_all(
                [
                    User(uid=UID, username="agent-work-consumer-user"),
                    Profile(id=1, uid=UID, name="consumer-profile-one", configs=profile_one_config),
                    Profile(id=2, uid=UID, name="consumer-profile-two", configs=profile_two_config),
                    ChatSession(session_id="session-1", uid=UID, profile_id=1),
                    ChatSession(session_id="session-2", uid=UID, profile_id=1),
                    ChatSession(session_id="session-3", uid=UID, profile_id=2),
                    ChatSession(session_id="session-4", uid=UID, profile_id=2),
                    SystemSetting(key="agent_work_max_concurrency", value="3"),
                ]
            )
            await db.commit()

        profile_one_first, profile_one_second, profile_two_first, profile_two_second, foreground_first, foreground_second = await _seed(
            factory,
            [
                ("session-1", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "consumer-p1-first", "consumer-p1-first"),
                ("session-2", 1, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "consumer-p1-second", "consumer-p1-second"),
                ("session-3", 2, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "consumer-p2-first", "consumer-p2-first"),
                ("session-4", 2, SessionReplyWorkType.AGENT_WORK, SessionReplySourceType.PARENT_WORK, "consumer-p2-second", "consumer-p2-second"),
                ("session-1", 1, SessionReplyWorkType.FOREGROUND_REPLY, SessionReplySourceType.USER_MESSAGE, "consumer-foreground-first", "consumer-foreground-first"),
                ("session-2", 1, SessionReplyWorkType.FOREGROUND_REPLY, SessionReplySourceType.USER_MESSAGE, "consumer-foreground-second", "consumer-foreground-second"),
            ],
        )

        started_ids: list[int] = []
        worker_ids: dict[int, str] = {}
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_execute_session_reply_work(work_id: int, worker_id: str) -> None:
            started_ids.append(work_id)
            worker_ids[work_id] = worker_id
            if len(started_ids) == 5:
                all_started.set()
            await release.wait()
            async with factory() as db:
                assert await CRUDSessionReplyWorkItem().mark_terminal(
                    db,
                    work_id=work_id,
                    worker_id=worker_id,
                    status=SessionReplyWorkStatus.SUCCEEDED,
                )

        monkeypatch.setattr(consumer_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(consumer_module, "execute_session_reply_work", fake_execute_session_reply_work)
        consumer = consumer_module.SessionReplyConsumer()
        consumer_task = consumer.start()
        try:
            await asyncio.wait_for(all_started.wait(), timeout=5)

            expected_running_ids = {
                profile_one_first.id,
                profile_two_first.id,
                profile_two_second.id,
                foreground_first.id,
                foreground_second.id,
            }
            assert len(started_ids) == 5
            assert set(started_ids) == expected_running_ids
            assert len(worker_ids) == 5

            async with factory() as db:
                works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
            running = [work for work in works if work.status == SessionReplyWorkStatus.RUNNING]
            running_ids = {work.id for work in running}
            assert running_ids == expected_running_ids
            assert all(work.locked_by in worker_ids.values() and work.lock_until is not None for work in running)
            assert sum(work.work_category == SessionReplyWorkCategory.FOREGROUND for work in running) == 2
            assert sum(work.work_category != SessionReplyWorkCategory.FOREGROUND for work in running) == 3
            assert sum(work.profile_id == 1 and work.work_category == SessionReplyWorkCategory.SUBAGENT for work in running) == 1
            assert sum(work.profile_id == 2 and work.work_category == SessionReplyWorkCategory.SUBAGENT for work in running) == 2

            profile_one_waiting = next(work for work in works if work.id == profile_one_second.id)
            assert profile_one_waiting.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert profile_one_waiting.locked_by is None
            assert profile_one_waiting.lock_until is None
        finally:
            consumer._stop_event.set()
            worker_tasks = [task for task, _worker_id in consumer._running.values() if not task.done()]
            release.set()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            await consumer.stop()
            await asyncio.gather(consumer_task, return_exceptions=True)
