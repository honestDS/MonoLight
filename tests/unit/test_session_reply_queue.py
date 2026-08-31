import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.constants import SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY
from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.unit.session_reply_queue_test_support import AsyncBarrier, enqueue

pytest_plugins = ("tests.unit.session_reply_queue_fixture",)


@pytest.mark.asyncio
async def test_sequence_numbers_are_shared_by_all_work_types(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    second = await enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=8, dedupe_key="background-task-summary:8")
    third = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await db_session.commit()

    assert [first.sequence_no, second.sequence_no, third.sequence_no] == [1, 2, 3]
    assert [first.max_attempts, second.max_attempts, third.max_attempts] == [2, 2, 2]


def test_session_reply_work_model_defaults_to_two_attempts():
    work = SessionReplyWorkItem(
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
    )

    assert work.max_attempts == 2


@pytest.mark.asyncio
async def test_duplicate_enqueue_returns_existing_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await db_session.commit()

    duplicate, created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=1,
        dedupe_key="foreground-message:1",
    )

    saved = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    assert created is False
    assert duplicate.id == first.id
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_concurrent_enqueue_assigns_contiguous_session_sequence_numbers(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
):
    crud = CRUDSessionReplyWorkItem()
    barrier = AsyncBarrier(3)
    source_ids = [30, 10, 20]

    async def enqueue_in_session(source_id: int) -> tuple[int | None, int | None, bool]:
        async with concurrent_session_factory() as db:
            await barrier.wait()
            work, created = await crud.enqueue(
                db,
                uid="user-1",
                session_id="session-1",
                profile_id=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id=source_id,
                dedupe_key=f"foreground-message:{source_id}",
            )
            return work.id, work.sequence_no, created

    results = await asyncio.gather(*(enqueue_in_session(source_id) for source_id in source_ids))

    async with concurrent_session_factory() as db:
        work = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == "session-1").order_by(SessionReplyWorkItem.sequence_no))).scalars().all())

    assert all(created for _work_id, _sequence_no, created in results)
    assert [item.sequence_no for item in work] == [1, 2, 3]
    assert len({item.sequence_no for item in work}) == len(work)
    assert {int(item.source_id) for item in work} == set(source_ids)
    assert [item.sequence_no for item in work] != [int(item.source_id) for item in work]


@pytest.mark.asyncio
async def test_concurrent_duplicate_enqueue_does_not_consume_a_sequence_number(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
):
    crud = CRUDSessionReplyWorkItem()
    allocation_barrier = AsyncBarrier(2)
    original_allocate_sequence_no = crud.allocate_sequence_no

    async def synchronize_sequence_allocation(db: AsyncSession, session_id: str) -> int:
        await allocation_barrier.wait()
        return await original_allocate_sequence_no(db, session_id)

    monkeypatch.setattr(crud, "allocate_sequence_no", synchronize_sequence_allocation)

    async def enqueue_duplicate(source_id: int) -> tuple[int | None, int | None, bool]:
        async with concurrent_session_factory() as db:
            work, created = await crud.enqueue(
                db,
                uid="user-1",
                session_id="session-1",
                profile_id=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id=source_id,
                dedupe_key="foreground-message:duplicate",
            )
            return work.id, work.sequence_no, created

    duplicate_results = await asyncio.gather(enqueue_duplicate(30), enqueue_duplicate(10))
    async with concurrent_session_factory() as db:
        next_work, created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=20,
            dedupe_key="foreground-message:next",
        )

    assert sum(created for _work_id, _sequence_no, created in duplicate_results) == 1
    assert len({work_id for work_id, _sequence_no, _created in duplicate_results}) == 1
    assert {sequence_no for _work_id, sequence_no, _created in duplicate_results} == {1}
    assert created is True
    assert next_work.sequence_no == 2


@pytest.mark.asyncio
async def test_concurrent_claims_keep_same_session_serial_and_claim_other_session(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
):
    crud = CRUDSessionReplyWorkItem()
    async with concurrent_session_factory() as db:
        first_session_first, _created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=30,
            dedupe_key="foreground-message:30",
            commit=False,
        )
        first_session_second, _created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=10,
            dedupe_key="foreground-message:10",
            commit=False,
        )
        second_session_first, _created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-2",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=20,
            dedupe_key="foreground-message:20",
            commit=False,
        )
        await db.commit()

    claim_barrier = AsyncBarrier(2)

    async def claim_in_session(worker_id: str) -> SessionReplyWorkItem | None:
        async with concurrent_session_factory() as db:
            await claim_barrier.wait()
            return await crud.claim_next(db, worker_id=worker_id, lease_seconds=300)

    claimed = await asyncio.gather(claim_in_session("worker-1"), claim_in_session("worker-2"))

    assert {work.id for work in claimed if work is not None} == {first_session_first.id, second_session_first.id}
    assert first_session_second.id not in {work.id for work in claimed if work is not None}


@pytest.mark.asyncio
async def test_claim_does_not_skip_an_earlier_waiting_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    second = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    first.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    db_session.add(first)
    await db_session.commit()

    claimed = await crud.claim_next(db_session, worker_id="worker-1", lease_seconds=300)

    assert claimed is None
    await db_session.refresh(second)
    assert second.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_claim_allows_different_sessions_to_run_concurrently(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    other, _created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-2",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=2,
        dedupe_key="foreground-message:2",
        commit=False,
    )
    await db_session.commit()

    claimed_first = await crud.claim_next(db_session, worker_id="worker-1", lease_seconds=300)
    claimed_second = await crud.claim_next(db_session, worker_id="worker-2", lease_seconds=300)

    assert {claimed_first.id, claimed_second.id} == {first.id, other.id}


@pytest.mark.asyncio
async def test_cancel_session_only_cancels_owned_non_terminal_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    ready = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    succeeded = await enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=2, dedupe_key="background-task-summary:2")
    succeeded.status = SessionReplyWorkStatus.SUCCEEDED
    other, _created = await crud.enqueue(
        db_session,
        uid="user-2",
        session_id="session-1",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=3,
        dedupe_key="foreground-message:3",
        commit=False,
    )
    await db_session.commit()

    cancelled_count = await crud.cancel_session(
        db_session,
        session_id="session-1",
        uid="user-1",
    )

    await db_session.refresh(ready)
    await db_session.refresh(succeeded)
    await db_session.refresh(other)
    assert cancelled_count == 1
    assert ready.status == SessionReplyWorkStatus.CANCELLED
    assert succeeded.status == SessionReplyWorkStatus.SUCCEEDED
    assert other.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_recover_expired_does_not_retry_work_with_emitted_stream_content(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    streamed = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:1",
    )
    retryable = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:2",
    )
    streamed.status = SessionReplyWorkStatus.RUNNING
    streamed.locked_by = "lost-worker-1"
    streamed.lock_until = 0
    retryable.status = SessionReplyWorkStatus.RUNNING
    retryable.locked_by = "lost-worker-2"
    retryable.lock_until = 0
    db_session.add(
        SessionReplyStreamEvent(
            work_id=streamed.id,
            sequence_no=1,
            event={"type": "content", "content": "partial"},
        )
    )
    await db_session.commit()

    recovered_count, exhausted_claims = await crud.recover_expired(db_session)

    await db_session.refresh(streamed)
    await db_session.refresh(retryable)
    assert recovered_count == 2
    assert exhausted_claims == [
        (
            streamed.id,
            "lost-worker-1",
            "Stream interrupted after partial response",
        )
    ]
    assert streamed.status == SessionReplyWorkStatus.RUNNING
    assert streamed.locked_by == "lost-worker-1"
    assert retryable.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_recover_expired_fails_work_at_max_attempts(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    exhausted = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:1",
    )
    exhausted.status = SessionReplyWorkStatus.RUNNING
    exhausted.locked_by = "lost-worker"
    exhausted.lock_until = 0
    exhausted.attempt_count = exhausted.max_attempts
    await db_session.commit()

    recovered_count, exhausted_claims = await crud.recover_expired(db_session)

    await db_session.refresh(exhausted)
    assert recovered_count == 1
    assert exhausted_claims == [
        (
            exhausted.id,
            "lost-worker",
            "Maximum retry attempts reached after worker interruption",
        )
    ]
    assert exhausted.status == SessionReplyWorkStatus.RUNNING
    assert exhausted.locked_by == "lost-worker"


@pytest.mark.asyncio
async def test_recover_expired_never_retries_confirmed_tool_execution(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    confirmed = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id=10,
        dedupe_key="confirmed-audit:10",
    )
    confirmed.status = SessionReplyWorkStatus.RUNNING
    confirmed.locked_by = "lost-worker"
    confirmed.lock_until = 0
    confirmed.attempt_count = 1
    confirmed.max_attempts = 2
    await db_session.commit()

    recovered_count, terminal_claims = await crud.recover_expired(db_session)

    await db_session.refresh(confirmed)
    assert recovered_count == 1
    assert terminal_claims == [
        (
            confirmed.id,
            "lost-worker",
            "Confirmed tool execution was interrupted; result unknown and automatic retry is forbidden",
        )
    ]
    assert confirmed.status == SessionReplyWorkStatus.RUNNING
    assert confirmed.locked_by == "lost-worker"


@pytest.mark.asyncio
async def test_recover_expired_does_not_retry_foreground_work_with_active_audit_execution(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    active = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:active-audit",
    )
    retryable = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:retryable",
    )
    active.status = SessionReplyWorkStatus.RUNNING
    active.locked_by = "lost-worker"
    active.lock_until = 0
    active.execution_state = {
        "active_audit_execution": {
            "audit_record_id": 42,
            "claim_token": "claim-token",
        }
    }
    retryable.status = SessionReplyWorkStatus.RUNNING
    retryable.locked_by = "lost-worker-2"
    retryable.lock_until = 0
    await db_session.commit()

    recovered_count, terminal_claims = await crud.recover_expired(db_session)

    await db_session.refresh(active)
    await db_session.refresh(retryable)
    assert recovered_count == 2
    assert terminal_claims == [
        (
            active.id,
            "lost-worker",
            "审计工具执行领取后被中断，结果未知，禁止自动重试",
        )
    ]
    assert active.status == SessionReplyWorkStatus.RUNNING
    assert active.locked_by == "lost-worker"
    assert retryable.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_type",
    [SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.SCHEDULED_TASK_SUMMARY],
)
async def test_recover_expired_does_not_retry_background_or_scheduled_audit_work(db_session: AsyncSession, work_type):
    """租约恢复发现后台或定时审计绑定时不得重新入队。"""
    crud = CRUDSessionReplyWorkItem()
    work = await enqueue(
        crud,
        db_session,
        work_type=work_type,
        source_id=1,
        dedupe_key=f"{work_type.value}:active-audit",
    )
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = "lost-worker"
    work.lock_until = 0
    work.execution_state = {
        SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
            "audit_record_id": 42,
            "claim_token": "claim-token",
        }
    }
    await db_session.commit()

    recovered_count, terminal_claims = await crud.recover_expired(db_session)

    await db_session.refresh(work)
    assert recovered_count == 1
    assert terminal_claims == [
        (
            work.id,
            "lost-worker",
            "审计工具执行领取后被中断，结果未知，禁止自动重试",
        )
    ]
    assert work.status == SessionReplyWorkStatus.RUNNING


@pytest.mark.asyncio
async def test_scheduled_claim_respects_profile_limit_and_still_claims_other_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    running = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
        source_id=1,
        dedupe_key="scheduled-task-summary:1",
    )
    running.status = SessionReplyWorkStatus.RUNNING
    running.locked_by = "worker-1"
    running.lock_until = 9999999999

    scheduled, _created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-2",
        profile_id=1,
        work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id=2,
        dedupe_key="scheduled-task-summary:2",
        commit=False,
    )
    foreground, _created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-3",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=3,
        dedupe_key="foreground-message:3",
        commit=False,
    )
    await db_session.commit()

    claimed = await crud.claim_next(
        db_session,
        worker_id="worker-2",
        lease_seconds=300,
        scheduled_profile_limits={1: 1},
    )

    assert claimed.id == foreground.id
    await db_session.refresh(scheduled)
    assert scheduled.status == SessionReplyWorkStatus.READY_FOR_LLM
