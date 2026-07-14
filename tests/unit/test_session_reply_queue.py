import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session_reply_work_item import CRUDSessionReplyWorkItem
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.unit.session_reply_queue_test_support import enqueue

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
