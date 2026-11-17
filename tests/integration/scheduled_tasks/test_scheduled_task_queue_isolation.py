import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.background_tasks.scheduler as scheduler_module
from app.core.constants import END_SESSION_SCOPE_TASK
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.message import Message
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.scheduled_tasks.scheduled_workflow_support import (
    PROFILE_ID,
    SESSION_ID,
    UID,
    _create_due_run_once_scheduled_task,
    db_session,
    session_factory,
)

__all__ = ("db_session", "session_factory")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws"])
@pytest.mark.parametrize(
    "scheduled_status",
    [
        SessionReplyWorkStatus.READY_FOR_LLM,
        SessionReplyWorkStatus.RUNNING,
        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
    ],
)
async def test_scheduled_work_does_not_block_same_session_user_message(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    source: str,
    scheduled_status: SessionReplyWorkStatus,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one_with_db(db_session, scheduled_task)

    scheduled_work = (
        (
            await db_session.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.session_id == SESSION_ID,
                    SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                )
            )
        )
        .scalars()
        .one()
    )
    assert scheduled_work.id is not None

    scheduled_worker_id = "scheduled-task-isolation-worker"
    if scheduled_status == SessionReplyWorkStatus.RUNNING:
        claimed_scheduled_work = await session_reply_work_item_crud.claim_next(
            db_session,
            worker_id=scheduled_worker_id,
            lease_seconds=300,
        )
        assert claimed_scheduled_work is not None
        assert claimed_scheduled_work.id == scheduled_work.id
    else:
        scheduled_work.status = scheduled_status
        scheduled_work.locked_by = None
        scheduled_work.lock_until = None
        if scheduled_status == SessionReplyWorkStatus.READY_FOR_LLM:
            scheduled_work.available_at = int(get_local_time().timestamp()) + 60
        db_session.add(scheduled_work)
        await db_session.commit()

    await db_session.refresh(scheduled_work)
    scheduled_work_snapshot = (
        scheduled_work.status,
        scheduled_work.locked_by,
        scheduled_work.lock_until,
    )

    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    initial_message, foreground_work, submission_status, _events = await scheduler_module.session_reply_queue_manager.submit_user_message(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        message="同会话前台用户输入",
        attachments=None,
        source=source,
        stream_requested=source == "ws",
        context_summary_events_requested=source == "ws",
    )
    assert submission_status == "accepted"
    assert initial_message.id is not None
    assert foreground_work.id is not None

    async with session_factory() as claim_db:
        claimed_foreground_work = await session_reply_work_item_crud.claim_next(
            claim_db,
            worker_id="foreground-isolation-worker",
            lease_seconds=300,
            scheduled_profile_limits={PROFILE_ID: 1},
        )
        assert claimed_foreground_work is not None
        assert claimed_foreground_work.id == foreground_work.id
        assert claimed_foreground_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
        assert claimed_foreground_work.source_type == SessionReplySourceType.USER_MESSAGE
        assert claimed_foreground_work.source_id == str(initial_message.id)

        persisted_scheduled_work = await claim_db.get(SessionReplyWorkItem, scheduled_work.id)
        assert persisted_scheduled_work is not None
        assert (
            persisted_scheduled_work.status,
            persisted_scheduled_work.locked_by,
            persisted_scheduled_work.lock_until,
        ) == scheduled_work_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("foreground_first", [True, False])
async def test_concurrent_scheduled_and_foreground_claims_respect_profile_limit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    foreground_first: bool,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    scheduler = scheduler_module.ScheduledTaskScheduler()

    if not foreground_first:
        await scheduler._dispatch_one_with_db(db_session, scheduled_task)

    _initial_message_a, foreground_a, submission_status_a, _events = await scheduler_module.session_reply_queue_manager.submit_user_message(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        message="前台工作A",
        attachments=None,
        source="http",
        stream_requested=False,
        context_summary_events_requested=False,
    )
    assert submission_status_a == "accepted"
    if foreground_first:
        await scheduler._dispatch_one_with_db(db_session, scheduled_task)

    _initial_message_b, foreground_b, submission_status_b, _events = await scheduler_module.session_reply_queue_manager.submit_user_message(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        message="前台工作B",
        attachments=None,
        source="http",
        stream_requested=False,
        context_summary_events_requested=False,
    )
    assert submission_status_b == "queued"
    assert foreground_a.id is not None
    assert foreground_b.id is not None

    scheduled_work = (
        (
            await db_session.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.session_id == SESSION_ID,
                    SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                )
            )
        )
        .scalars()
        .one()
    )
    assert scheduled_work.id is not None

    async with session_factory() as scheduled_claim_db, session_factory() as foreground_claim_db:
        scheduled_claim, foreground_claim = await asyncio.gather(
            session_reply_work_item_crud.claim_next(
                scheduled_claim_db,
                worker_id="scheduled-claim-worker",
                lease_seconds=300,
                scheduled_profile_limits={PROFILE_ID: 1},
            ),
            session_reply_work_item_crud.claim_next(
                foreground_claim_db,
                worker_id="foreground-claim-worker",
                lease_seconds=300,
                scheduled_profile_limits={PROFILE_ID: 1},
            ),
        )

    claimed_works = [work for work in (scheduled_claim, foreground_claim) if work is not None]
    assert len(claimed_works) == 2
    assert {work.id for work in claimed_works} == {scheduled_work.id, foreground_a.id}
    assert {work.locked_by for work in claimed_works} == {"scheduled-claim-worker", "foreground-claim-worker"}
    persisted_scheduled_worker_id: str | None = None
    persisted_foreground_worker_id: str | None = None
    async with session_factory() as verify_db:
        persisted_scheduled = await verify_db.get(SessionReplyWorkItem, scheduled_work.id)
        persisted_foreground_a = await verify_db.get(SessionReplyWorkItem, foreground_a.id)
        persisted_foreground_b = await verify_db.get(SessionReplyWorkItem, foreground_b.id)
        assert persisted_scheduled is not None
        assert persisted_foreground_a is not None
        assert persisted_foreground_b is not None
        assert persisted_scheduled.status == SessionReplyWorkStatus.RUNNING
        assert persisted_foreground_a.status == SessionReplyWorkStatus.RUNNING
        assert persisted_foreground_b.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert persisted_foreground_b.locked_by is None
        persisted_scheduled_worker_id = persisted_scheduled.locked_by
        persisted_foreground_worker_id = persisted_foreground_a.locked_by
        assert persisted_scheduled_worker_id is not None
        assert persisted_foreground_worker_id is not None
        assert persisted_scheduled_worker_id != persisted_foreground_worker_id

    async with session_factory() as enqueue_db:
        second_scheduled_work, created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
            enqueue_db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id="scheduled-boundary-second",
            dedupe_key="scheduled-agent-work:scheduled-boundary-second",
            spec=AgentWorkSpec(
                instruction="第二个计划工作",
                execution_scope=END_SESSION_SCOPE_TASK,
                report_target=AgentWorkReportTarget.SESSION,
            ),
        )
    assert created is True
    assert second_scheduled_work.id is not None

    async with session_factory() as blocked_claim_db:
        blocked_claim = await session_reply_work_item_crud.claim_next(
            blocked_claim_db,
            worker_id="blocked-second-scheduled-worker",
            lease_seconds=300,
            scheduled_profile_limits={PROFILE_ID: 1},
        )
    assert blocked_claim is None

    assert persisted_scheduled_worker_id is not None
    async with session_factory() as terminal_db:
        assert await session_reply_work_item_crud.mark_terminal(
            terminal_db,
            work_id=scheduled_work.id,
            worker_id=persisted_scheduled_worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
        )

    async with session_factory() as second_claim_db:
        claimed_second_scheduled = await session_reply_work_item_crud.claim_next(
            second_claim_db,
            worker_id="second-scheduled-claim-worker",
            lease_seconds=300,
            scheduled_profile_limits={PROFILE_ID: 1},
        )
    assert claimed_second_scheduled is not None
    assert claimed_second_scheduled.id == second_scheduled_work.id
    assert claimed_second_scheduled.locked_by == "second-scheduled-claim-worker"

    async with session_factory() as verify_db:
        persisted_scheduled = await verify_db.get(SessionReplyWorkItem, scheduled_work.id)
        persisted_second_scheduled = await verify_db.get(SessionReplyWorkItem, second_scheduled_work.id)
        persisted_foreground_a = await verify_db.get(SessionReplyWorkItem, foreground_a.id)
        persisted_foreground_b = await verify_db.get(SessionReplyWorkItem, foreground_b.id)
        assert persisted_scheduled is not None
        assert persisted_second_scheduled is not None
        assert persisted_foreground_a is not None
        assert persisted_foreground_b is not None
        assert persisted_scheduled.status == SessionReplyWorkStatus.SUCCEEDED
        assert persisted_scheduled.locked_by is None
        assert persisted_second_scheduled.status == SessionReplyWorkStatus.RUNNING
        assert persisted_second_scheduled.locked_by == "second-scheduled-claim-worker"
        assert persisted_foreground_a.status == SessionReplyWorkStatus.RUNNING
        assert persisted_foreground_a.locked_by == persisted_foreground_worker_id
        assert persisted_foreground_b.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert persisted_foreground_b.locked_by is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["freeze", "absorb"])
async def test_scheduled_work_does_not_break_foreground_input_merge(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    phase: str,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    manager = scheduler_module.session_reply_queue_manager
    scheduler = scheduler_module.ScheduledTaskScheduler()
    worker_id = "scheduled-boundary-foreground-worker"

    message_a, foreground_a, status_a, _events = await manager.submit_user_message(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        message="前台工作A",
        attachments=None,
        source="http",
        stream_requested=False,
        context_summary_events_requested=False,
    )
    assert status_a == "accepted"
    assert message_a.id is not None and foreground_a.id is not None

    claimed_a = await session_reply_work_item_crud.claim_next(db_session, worker_id=worker_id, lease_seconds=300)
    assert claimed_a is not None
    assert claimed_a.id == foreground_a.id

    if phase == "absorb":
        frozen = await manager.freeze_foreground_input(db_session, work=claimed_a, worker_id=worker_id)
        assert frozen == ("前台工作A", [], [message_a.id])

    await scheduler._dispatch_one_with_db(db_session, scheduled_task)
    message_b, foreground_b, status_b, _events = await manager.submit_user_message(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        message="前台工作B",
        attachments=None,
        source="http",
        stream_requested=False,
        context_summary_events_requested=False,
    )
    assert status_b == "queued"
    assert message_b.id is not None and foreground_b.id is not None

    if phase == "freeze":
        frozen = await manager.freeze_foreground_input(db_session, work=claimed_a, worker_id=worker_id)
        assert frozen == ("前台工作A\n前台工作B", [], [message_a.id, message_b.id])
    else:
        batch = await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=claimed_a.id,
            worker_id=worker_id,
        )
        assert batch is not None
        assert batch.source_message_ids == (message_b.id,)
        assert len(batch.messages) == 1
        assert batch.messages[0].content == "前台工作B"

    scheduled_work = (
        (
            await db_session.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.session_id == SESSION_ID,
                    SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                )
            )
        )
        .scalars()
        .one()
    )
    assert scheduled_work.id is not None

    async with session_factory() as verify_db:
        persisted_a = await verify_db.get(SessionReplyWorkItem, foreground_a.id)
        persisted_b = await verify_db.get(SessionReplyWorkItem, foreground_b.id)
        persisted_scheduled = await verify_db.get(SessionReplyWorkItem, scheduled_work.id)
        persisted_messages = list((await verify_db.execute(select(Message).where(Message.id.in_([message_a.id, message_b.id])))).scalars().all())

    assert persisted_a is not None and persisted_b is not None and persisted_scheduled is not None
    assert persisted_a.input_message_ids == [message_a.id, message_b.id]
    assert persisted_b.status == SessionReplyWorkStatus.MERGED
    assert persisted_b.merged_into_id == persisted_a.id
    assert all(message.is_processed for message in persisted_messages)
    assert persisted_scheduled.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert persisted_scheduled.merged_into_id is None
    assert not persisted_scheduled.input_message_ids
