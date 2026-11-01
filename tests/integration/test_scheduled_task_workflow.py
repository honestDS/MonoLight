import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.audit.persistence as audit_persistence_module
import app.core.audit.service as audit_service_module
import app.core.background_tasks.external_work as external_work_module
import app.core.background_tasks.result_delivery as result_delivery_module
import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.message_platforms.notifier as notifier_module
import app.core.session.notifier as session_notifier_module
import app.core.session_reply_queue.executor_agent_work as executor_agent_work_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.tools.send_file_to_user as send_file_to_user_module
import app.models  # noqa: F401
from app.core.background_tasks.external_work import resume_external_work_for_task
from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    END_SESSION_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    MANAGE_TODO_TOOL_NAME,
    SEND_FILE_TO_USER_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_SUMMARY_PROMPT, SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.session_reply_queue.executor_resume import restore_interactive_tool_replies
from app.core.tools.scheduled_tasks import ManageScheduledTasksExecutor
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.audit import AuditRecord, AuditRecordStatus, AuditToolConclusion, AuditToolDetail
from app.models.background_task import BackgroundTask, BackgroundTaskReplyMode, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.llm.client import LLMClient

UID = "scheduled-owner"
SESSION_ID = "scheduled-workflow"
PROFILE_ID = 1
MODEL_ID = "scheduled-model"
FINAL_REPLY = "管理操作已完成"
USAGE = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


@pytest_asyncio.fixture
async def session_factory(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(foreign_keys=True, name="scheduled-task-workflow.sqlite3") as factory:
        yield factory


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession]:
    async with session_factory() as db:
        yield db


async def _create_due_run_once_scheduled_task(db: AsyncSession) -> ScheduledTask:
    profile = Profile(
        id=PROFILE_ID,
        uid=UID,
        name="scheduled-task-run-once-profile",
        configs=ProfileConfig.model_validate({}).model_dump(),
    )
    db.add_all([User(uid=UID, username=UID), profile])
    await db.flush()
    db.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    await db.flush()

    scheduled_task = ScheduledTask(
        name="One-time reminder",
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        message="Run this reminder once",
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db.add(scheduled_task)
    await db.commit()
    await db.refresh(scheduled_task)
    return scheduled_task


async def _enqueue_scheduled_agent_work(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    trigger_message: Message,
    scheduled_task_id: int = 1,
) -> tuple[SessionReplyWorkItem, bool]:
    assert trigger_message.id is not None
    return await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id=trigger_message.id,
        dedupe_key=f"scheduled-agent-work:{scheduled_task_id}:{trigger_message.id}",
        spec=AgentWorkSpec(
            instruction=str(trigger_message.content),
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
            instruction_message_id=trigger_message.id,
        ),
        commit=False,
    )


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


@pytest.mark.asyncio
async def test_agent_work_resume_restores_its_hidden_tool_result_only(
    db_session: AsyncSession,
) -> None:
    profile_id = 204
    session_id = "agent-work-resume-hidden-result"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="agent-work-resume-profile", configs=ProfileConfig.model_validate({}).model_dump()),
            ChatSession(session_id=session_id, uid=UID, profile_id=profile_id),
        ]
    )
    await db_session.commit()

    work, _created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="resume-trigger",
        dedupe_key="scheduled-agent-work:resume-hidden-result",
        spec=AgentWorkSpec(
            instruction="恢复隐藏工具结果",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
        commit=False,
    )
    await db_session.commit()
    await db_session.refresh(work)
    assert work.id is not None

    worker_id = "agent-work-resume-worker"
    claimed = await session_reply_work_item_crud.claim_next(db_session, worker_id=worker_id, lease_seconds=300)
    assert claimed is not None
    assert claimed.id == work.id
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)

    call_id = "resume-hidden-call"
    assistant_call = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id=call_id, name="resume_tool", arguments={})],
    )
    saved_call = await save_assistant_message(db_session, session_id, UID, profile_id, assistant_call)
    assert saved_call.id is not None

    public_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="公开主会话消息",
        is_processed=True,
    )
    await message_crud.persist(db_session, message=public_message, commit=False)

    other_work, _created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="other-agent-work",
        dedupe_key="scheduled-agent-work:other-hidden-result",
        spec=AgentWorkSpec(
            instruction="其他工作",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(other_work)
    assert other_work.id is not None
    other_worker_id = "other-agent-work-worker"
    other_work.status = SessionReplyWorkStatus.RUNNING
    other_work.locked_by = other_worker_id
    other_work.lock_until = int(get_local_time().timestamp()) + 300
    db_session.add(other_work)
    await db_session.commit()

    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (other_work.id, other_worker_id)
    other_result = await save_tool_response(
        db_session,
        session_id,
        UID,
        profile_id,
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=call_id,
            content="其他工作结果",
            tool_execution_status="succeeded",
        ),
        [],
        [],
    )
    assert other_result.id is not None

    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    own_result = await save_tool_response(
        db_session,
        session_id,
        UID,
        profile_id,
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=call_id,
            content="真实成功结果",
            tool_execution_status="succeeded",
        ),
        [],
        [],
    )
    assert own_result.id is not None

    checkpoint_messages = [assistant_call.model_dump(mode="json", exclude_none=True)]
    checkpoint = {
        "messages": checkpoint_messages,
        "turn_messages": list(checkpoint_messages),
        "reload_history": False,
    }
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    restored = await restore_interactive_tool_replies(
        db_session,
        work=work,
        worker_id=worker_id,
        checkpoint=checkpoint,
    )

    restored_messages = [InternalMessage.model_validate(message) for message in restored["messages"]]
    restored_turn_messages = [InternalMessage.model_validate(message) for message in restored["turn_messages"]]
    restored_results = [message for message in restored_messages if message.role == MessageRole.TOOL]
    restored_turn_results = [message for message in restored_turn_messages if message.role == MessageRole.TOOL]
    assert len(restored_results) == 1
    assert restored_results[0].id == own_result.id
    assert restored_results[0].content == "真实成功结果"
    assert restored_results[0].tool_execution_status == "succeeded"
    assert len(restored_turn_results) == 1
    assert restored_turn_results[0].id == own_result.id
    assert not any(message.tool_execution_status == "execution_unknown" for message in restored_results)

    tool_rows = list(
        (
            await db_session.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.type == MessageType.TOOL_RESULT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.id for row in tool_rows} == {other_result.id, own_result.id}
    assert [row.id for row in tool_rows if row.work_id == work.id] == [own_result.id]
    saved_call_row = await db_session.get(Message, saved_call.id)
    assert saved_call_row is not None
    assert saved_call_row.work_id == work.id
    public_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert all(message.work_id is None for message in public_history)
    assert all(message.type not in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} for message in public_history)

    restored_again = await restore_interactive_tool_replies(
        db_session,
        work=work,
        worker_id=worker_id,
        checkpoint=restored,
    )
    assert restored_again == restored
    tool_rows_after_repeat = list(
        (
            await db_session.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.type == MessageType.TOOL_RESULT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.id for row in tool_rows_after_repeat} == {row.id for row in tool_rows}


@pytest.mark.asyncio
async def test_work_owned_messages_stay_out_of_session_history_until_promoted(db_session: AsyncSession) -> None:
    profile_id = 190
    session_id = "work-owned-message-isolation"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="work-owned-message-profile", configs=ProfileConfig.model_validate({}).model_dump()),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id))
    await db_session.flush()

    main_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="主会话消息",
        is_processed=True,
    )
    await message_crud.persist(db_session, message=main_message, commit=False)
    internal_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="AgentWork内部消息",
        is_processed=True,
        work_id=777,
    )
    await message_crud.persist(db_session, message=internal_message, commit=False)
    await db_session.commit()

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    assert session.latest_message_id == main_message.id

    model_history = await message_crud.get_history_backward_by_id(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(model_history)] == ["主会话消息"]

    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息"]

    assert internal_message.id is not None
    promoted = await message_crud.promote_work_message_to_session(
        db_session,
        message_id=internal_message.id,
        work_id=777,
        commit=True,
    )
    assert promoted is True

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    assert session.latest_message_id == internal_message.id
    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息", "AgentWork内部消息"]


@pytest.mark.asyncio
async def test_agent_work_saved_messages_are_hidden_until_promoted(db_session: AsyncSession) -> None:
    profile_id = 191
    session_id = "agent-work-save-isolation"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="agent-work-save-profile", configs=ProfileConfig.model_validate({}).model_dump()),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id))
    await db_session.flush()

    main_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="主会话消息",
        is_processed=True,
    )
    await message_crud.persist(db_session, message=main_message, commit=False)

    work, _created = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="trigger-1",
        dedupe_key="scheduled-agent-work:save-isolation",
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(work)
    assert work.id is not None

    worker_id = "worker-save-isolation"
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = int(get_local_time().timestamp()) + 300
    db_session.add(work)
    await db_session.commit()
    await db_session.refresh(work)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)

    saved = await save_assistant_message(
        db_session,
        session_id,
        UID,
        profile_id,
        InternalMessage(role=MessageRole.ASSISTANT, content="计划任务结果"),
    )
    assert saved.id is not None

    saved_row = await db_session.get(Message, saved.id)
    assert saved_row is not None
    assert saved_row.work_id == work.id

    work_user_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="工作消息不应进入主会话输入队列",
        is_processed=False,
        work_id=work.id,
    )
    await message_crud.persist(db_session, message=work_user_message, commit=False)
    unprocessed = await message_crud.get_unprocessed_messages(db_session, session_id=session_id, uid=UID)
    assert work_user_message not in unprocessed

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    assert session.latest_message_id == main_message.id
    model_history = await message_crud.get_history_backward_by_id(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(model_history)] == ["主会话消息"]
    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息"]

    promoted = await message_crud.promote_work_message_to_session(
        db_session,
        message_id=saved.id,
        work_id=work.id,
        commit=True,
    )
    assert promoted is True
    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息", "计划任务结果"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report_target",
    [AgentWorkReportTarget.SESSION, AgentWorkReportTarget.PARENT_WORK],
    ids=["session-report", "parent-work-report"],
)
async def test_agent_work_failure_message_is_visible_in_session_history(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    report_target: AgentWorkReportTarget,
) -> None:
    profile_id = 192
    session_id = "agent-work-failure-isolation"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="agent-work-failure-profile", configs=ProfileConfig.model_validate({}).model_dump()),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id))
    await db_session.flush()

    work, _created = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="1",
        dedupe_key="scheduled-agent-work:failure-isolation",
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(work)
    assert work.id is not None
    work.execution_state = {
        "frozen_context_messages": [],
        "agent_work": AgentWorkSpec(
            instruction="计划任务失败",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=report_target,
            parent_work_id=1 if report_target == AgentWorkReportTarget.PARENT_WORK else None,
        ).model_dump(mode="json"),
    }
    db_session.add(work)

    worker_id = "worker-failure-isolation"
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = int(get_local_time().timestamp()) + 300
    db_session.add(work)
    await db_session.commit()

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(session_notifier_module, "AsyncSessionLocal", session_factory)

    await executor_lifecycle_module.fail_session_reply_work(work.id, worker_id, "internal failure", user_error="计划任务失败")

    async with session_factory() as verification_db:
        error_message = (await verification_db.execute(select(Message).where(Message.session_id == session_id, Message.role == MessageRole.ERR))).scalars().one()
        session = await verification_db.get(ChatSession, session_id)
        assert session is not None
        finished_work = await verification_db.get(SessionReplyWorkItem, work.id)
        assert finished_work is not None
        assert finished_work.status == SessionReplyWorkStatus.FAILED
        model_history = await message_crud.get_history_backward_by_id(verification_db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(verification_db, session_id=session_id, uid=UID)
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())

    if report_target == AgentWorkReportTarget.SESSION:
        assert error_message.work_id is None
        assert session.latest_message_id == error_message.id
        assert [message.id for message in reversed(model_history)] == [error_message.id]
        assert [message.id for message in reversed(ui_history)] == [error_message.id]
        assert len(session_events) == 1
        error_event = session_events[0].event
        assert error_event["type"] == "proactive_reply_error"
        assert error_event["work_id"] == work.id
        assert error_event["message_id"] == error_message.id
        assert error_event["content"] == error_message.content
        assert finished_work.event_sent is True
    else:
        assert error_message.work_id == work.id
        assert session.latest_message_id is None
        assert model_history == []
        assert ui_history == []
        assert session_events == []
        assert finished_work.event_sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("run_once", [False, True], ids=["recurring", "one-time"])
async def test_llm_workflow_creates_lists_and_deletes_scheduled_task(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    run_once: bool,
) -> None:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-workflow-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    },
                    {
                        "model_id": "scheduled-image-model",
                        "usage": "IMAGE_GENERATION",
                        "protocol": "OPENAI_IMAGE",
                        "size": "1024x1024",
                        "quality": "auto",
                    },
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-workflow-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.flush()

    persisted_user_message = Message(
        session_id=SESSION_ID,
        uid=UID,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="请设置提醒并确认管理完成",
        profile_id=PROFILE_ID,
        is_processed=False,
    )
    db_session.add(persisted_user_message)
    await db_session.commit()
    await db_session.refresh(persisted_user_message)
    persisted_initial_message = InternalMessage(
        id=persisted_user_message.id,
        role=MessageRole.USER,
        content=persisted_user_message.content,
        created_at=persisted_user_message.created_at.timestamp(),
    )

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)

    calls: list[dict[str, Any]] = []
    observed_results: list[dict[str, Any]] = []

    def tool_result(messages: list[InternalMessage], call_id: str) -> dict[str, Any]:
        for message in reversed(messages):
            if message.role == MessageRole.TOOL and message.tool_call_id == call_id:
                assert isinstance(message.content, str)
                result = json.loads(message.content)
                assert isinstance(result, dict)
                return result
        raise AssertionError(f"No tool result found for {call_id}")

    def response_for(kwargs: dict[str, Any]) -> InternalResponse:
        calls.append(kwargs)
        tools = kwargs["tools"]
        scheduled_tool = next(tool for tool in tools if tool["function"]["name"] == MANAGE_SCHEDULED_TASKS_TOOL_NAME)
        operation_schema = scheduled_tool["function"]["parameters"]["properties"]["operation"]
        assert {"create", "list", "delete"} <= set(operation_schema["enum"])

        turn = len(calls)
        if turn == 1:
            create_arguments = {
                "operation": "create",
                "name": "Pipeline reminder",
                "message": "Review deployment status",
                "interval_seconds": 60,
            }
            if run_once:
                create_arguments["run_once"] = True
            tool_call = InternalToolCall(
                id="scheduled-create-call",
                name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                arguments=create_arguments,
            )
        elif turn == 2:
            created = tool_result(kwargs["messages"], "scheduled-create-call")
            assert created["status"] == "success"
            assert created["operation"] == "create"
            observed_results.append(created)
            tool_call = InternalToolCall(
                id="scheduled-list-call",
                name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                arguments={"operation": "list"},
            )
        elif turn == 3:
            listed = tool_result(kwargs["messages"], "scheduled-list-call")
            assert listed["status"] == "success"
            assert listed["operation"] == "list"
            observed_results.append(listed)
            assert len(listed["tasks"]) == 1
            task_id = listed["tasks"][0]["id"]
            tool_call = InternalToolCall(
                id="scheduled-delete-call",
                name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                arguments={"operation": "delete", "task_id": task_id},
            )
        elif turn == 4:
            deleted = tool_result(kwargs["messages"], "scheduled-delete-call")
            assert deleted["status"] == "success"
            assert deleted["operation"] == "delete"
            observed_results.append(deleted)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=FINAL_REPLY),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        else:
            raise AssertionError(f"Unexpected LLM request {turn}")

        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="tool_calls",
        )

    async def generate(**kwargs: Any) -> InternalResponse:
        assert not stream
        return response_for(kwargs)

    async def generate_with_stream_callback(**kwargs: Any) -> InternalResponse:
        assert stream
        response = response_for(kwargs)
        if response.message.content:
            await kwargs["on_content"](response.message.content)
        return response

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "generate_with_stream_callback",
        generate_with_stream_callback,
    )

    if stream:
        result = [
            event
            async for event in ChatDispatcher.dispatch_stream(
                db=db_session,
                message=persisted_user_message.content,
                uid=UID,
                session_id=SESSION_ID,
                request_id="scheduled-task-workflow-request",
                session_source="ws",
                persisted_initial_message=persisted_initial_message,
                frozen_user_message_ids=[persisted_initial_message.id],
                persisted_profile_id=PROFILE_ID,
                show_tool_calls=True,
            )
        ]
        done_events = [event for event in result if event.get("type") == "done"]
        assert len(done_events) == 1
        response = done_events[0]["response"]
    else:
        response = await ChatDispatcher.dispatch(
            db=db_session,
            message=persisted_user_message.content,
            uid=UID,
            session_id=SESSION_ID,
            session_source="http",
            persisted_initial_message=persisted_initial_message,
            frozen_user_message_ids=[persisted_initial_message.id],
            persisted_profile_id=PROFILE_ID,
            show_tool_calls=True,
        )

    assert len(calls) == 4
    assert response["choices"][0]["message"]["content"] == FINAL_REPLY
    assert [result["operation"] for result in observed_results] == ["create", "list", "delete"]

    created_task = observed_results[0]["task"]
    assert (created_task["uid"], created_task["session_id"], created_task["profile_id"]) == (UID, SESSION_ID, PROFILE_ID)
    assert created_task["run_once"] is run_once
    listed_tasks = observed_results[1]["tasks"]
    assert len(listed_tasks) == 1
    listed_task = listed_tasks[0]
    assert listed_task["id"] == created_task["id"]
    assert (listed_task["uid"], listed_task["session_id"], listed_task["profile_id"]) == (UID, SESSION_ID, PROFILE_ID)
    assert listed_task["run_once"] is run_once
    assert observed_results[2]["task_id"] == created_task["id"]

    async with session_factory() as verification_db:
        remaining_tasks = list((await verification_db.execute(select(ScheduledTask))).scalars().all())
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id.asc()))).scalars().all())

    assert remaining_tasks == []
    tool_call_messages = [message for message in persisted_messages if message.type == MessageType.TOOL_CALL]
    tool_result_messages = [message for message in persisted_messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_messages = [message for message in persisted_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_call_messages) == 3
    assert all(message.role == MessageRole.ASSISTANT for message in tool_call_messages)
    assert len(tool_result_messages) == 3
    assert all(message.role == MessageRole.TOOL for message in tool_result_messages)
    assert len(assistant_text_messages) == 1
    assert assistant_text_messages[0].content == FINAL_REPLY
    assert persisted_messages[-1].role == MessageRole.ASSISTANT
    assert persisted_messages[-1].type == MessageType.TEXT


@pytest.mark.asyncio
async def test_scheduled_task_dispatch_enqueues_once_and_delete_preserves_queued_work(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(
        id=PROFILE_ID,
        uid=UID,
        name="scheduled-task-dispatch-profile",
        configs=ProfileConfig.model_validate({}).model_dump(),
    )
    db_session.add_all([User(uid=UID, username=UID), profile])
    await db_session.flush()
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    await db_session.commit()

    executor = ManageScheduledTasksExecutor(project_root=".", uid=UID)
    executor.set_runtime_context(db=db_session, profile=profile, session_id=SESSION_ID)
    created = json.loads(
        await executor.execute(
            "create",
            name="Pipeline reminder",
            message="Review deployment status",
            interval_seconds=60,
        )
    )
    assert created["status"] == "success"
    task_id = created["task"]["id"]

    scheduled_task = await db_session.get(ScheduledTask, task_id)
    assert scheduled_task is not None
    scheduled_task.next_run_at = get_local_time() - timedelta(seconds=1)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    due_time = scheduled_task.next_run_at

    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(task_id)
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                        Message.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.run_count == 1
        assert persisted_task.last_run_at is not None
        assert persisted_task.last_run_at >= due_time
        assert persisted_task.next_run_at > persisted_task.last_run_at
        assert persisted_task.last_message_id is not None
        assert len(trigger_messages) == 1
        trigger_message = trigger_messages[0]
        assert trigger_message.id == persisted_task.last_message_id
        assert trigger_message.uid == UID
        assert trigger_message.session_id == SESSION_ID
        assert trigger_message.profile_id == PROFILE_ID
        assert trigger_message.content == "Review deployment status"
        assert trigger_message.role == MessageRole.USER
        assert trigger_message.is_processed is True

        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_message.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(work_items) == 1
        queued_work = work_items[0]
        assert queued_work.uid == UID
        assert queued_work.session_id == SESSION_ID
        assert queued_work.profile_id == PROFILE_ID
        assert queued_work.work_type == SessionReplyWorkType.AGENT_WORK
        assert queued_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert trigger_message.work_id == queued_work.id
        assert queued_work.execution_state.get("frozen_context_messages") == []
        queued_spec = AgentWorkSpec.model_validate(queued_work.execution_state.get("agent_work"))
        assert queued_spec.instruction == "Review deployment status"
        assert queued_spec.execution_scope == END_SESSION_SCOPE_TASK
        assert queued_spec.report_target == AgentWorkReportTarget.SESSION
        assert queued_spec.instruction_message_id == trigger_message.id

    deleted = json.loads(await executor.execute("delete", task_id=task_id))
    assert deleted["status"] == "success"
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        assert await reader.get(ScheduledTask, task_id) is None
        remaining_triggers = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                        Message.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        remaining_work = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_message.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining_triggers) == 1
    assert len(remaining_work) == 1
    assert remaining_work[0].status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_run_once_scheduled_task_is_not_redispatched_from_stale_snapshot_or_later_poll(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    assert scheduled_task.id is not None
    task_id = scheduled_task.id
    due_time = scheduled_task.next_run_at

    async with session_factory() as snapshot_db:
        stale_snapshot = await snapshot_db.get(ScheduledTask, task_id)
        assert stale_snapshot is not None
        assert stale_snapshot.status == ScheduledTaskStatus.ENABLED
        snapshot_db.expunge(stale_snapshot)

    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(task_id)

    async with session_factory() as stale_db:
        await scheduler._dispatch_one_with_db(stale_db, stale_snapshot)

    monkeypatch.setattr(
        scheduler_module,
        "get_local_time",
        lambda: due_time + timedelta(seconds=scheduled_task.interval_seconds + 1),
    )
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.status == ScheduledTaskStatus.DISABLED
        assert persisted_task.run_count == 1
        assert persisted_task.next_run_at == due_time
        assert persisted_task.last_message_id is not None
        assert len(trigger_messages) == 1

        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_messages[0].id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(work_items) == 1
        assert work_items[0].work_type == SessionReplyWorkType.AGENT_WORK
        assert work_items[0].status == SessionReplyWorkStatus.READY_FOR_LLM
        assert work_items[0].uid == UID
        assert work_items[0].session_id == SESSION_ID
        assert work_items[0].profile_id == PROFILE_ID
        assert all(task.id != task_id for task in await scheduled_task_crud.list_due(reader))


@pytest.mark.asyncio
async def test_run_once_scheduled_task_enqueue_failure_rolls_back_before_retry(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    assert scheduled_task.id is not None
    task_id = scheduled_task.id
    due_time = scheduled_task.next_run_at

    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    scheduler = scheduler_module.ScheduledTaskScheduler()
    queue_manager = scheduler_module.session_reply_queue_manager
    original_enqueue = queue_manager.enqueue_agent_work

    async def enqueue_then_fail(*args: Any, **kwargs: Any) -> None:
        await original_enqueue(*args, **kwargs)
        raise RuntimeError("simulated agent work enqueue failure")

    monkeypatch.setattr(queue_manager, "enqueue_agent_work", enqueue_then_fail)
    with pytest.raises(RuntimeError, match="simulated agent work enqueue failure"):
        await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                    )
                )
            )
            .scalars()
            .all()
        )
        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.status == ScheduledTaskStatus.ENABLED
        assert persisted_task.run_count == 0
        assert persisted_task.last_run_at is None
        assert persisted_task.last_message_id is None
        assert persisted_task.next_run_at == due_time
        assert trigger_messages == []
        assert work_items == []

    monkeypatch.setattr(queue_manager, "enqueue_agent_work", original_enqueue)
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.status == ScheduledTaskStatus.DISABLED
        assert persisted_task.run_count == 1
        assert persisted_task.last_message_id is not None
        assert len(trigger_messages) == 1

        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_messages[0].id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(work_items) == 1
        assert work_items[0].work_type == SessionReplyWorkType.AGENT_WORK
        assert work_items[0].status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "run_once"),
    [("create", False), ("create", True), ("delete", False)],
    ids=["create-recurring", "create-one-time", "delete"],
)
async def test_scheduled_task_mutation_waits_for_audit_confirmation(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    run_once: bool,
) -> None:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["security"].update({"audit_channel_id": 1, "audit_model_id": MODEL_ID, "audit_threshold": 5})
    profile = Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-audit-profile", configs=configs)
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-audit-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            profile,
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.flush()

    scheduled_task_snapshot = None
    if operation == "delete":
        protected_task = ScheduledTask(
            id=1,
            name="Protected task",
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            message="Keep this scheduled request",
            interval_seconds=60,
            next_run_at=get_local_time() + timedelta(seconds=60),
        )
        db_session.add(protected_task)
        await db_session.flush()
        await db_session.refresh(protected_task)
        scheduled_task_snapshot = protected_task.model_dump(mode="json")

    persisted_user_message = Message(
        session_id=SESSION_ID,
        uid=UID,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="管理这个计划任务",
        profile_id=PROFILE_ID,
        is_processed=False,
    )
    db_session.add(persisted_user_message)
    await db_session.commit()
    await db_session.refresh(persisted_user_message)
    persisted_initial_message = InternalMessage(
        id=persisted_user_message.id,
        role=MessageRole.USER,
        content=persisted_user_message.content,
        created_at=persisted_user_message.created_at.timestamp(),
    )

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        audit_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    async def generate(**kwargs: Any) -> InternalResponse:
        prompt = kwargs["messages"][0].content
        if prompt == AUDIT_BATCH_PROMPT:
            content = json.dumps(
                {
                    "results": [
                        {
                            "tool_call_id": "scheduled-audited-call",
                            "score": 9,
                            "deny": False,
                            "reason": "Persistent scheduled action requires confirmation",
                            "file_checks": [],
                        }
                    ]
                }
            )
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        if prompt == AUDIT_SUMMARY_PROMPT.format(audit_report_language="zh"):
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="计划任务操作将在用户确认后执行。",
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )

        if operation == "create":
            arguments = {
                "operation": "create",
                "name": "Pipeline reminder",
                "message": "Review deployment status",
                "interval_seconds": 60,
            }
            if run_once:
                arguments["run_once"] = True
        else:
            arguments = {"operation": "delete", "task_id": 1}
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="scheduled-audited-call",
                        name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                        arguments=arguments,
                    )
                ],
            ),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    response = await ChatDispatcher.dispatch(
        db=db_session,
        message=persisted_user_message.content,
        uid=UID,
        session_id=SESSION_ID,
        session_source="http",
        persisted_initial_message=persisted_initial_message,
        frozen_user_message_ids=[persisted_initial_message.id],
        persisted_profile_id=PROFILE_ID,
        show_tool_calls=True,
    )

    confirmation_card = json.loads(response["choices"][0]["message"]["content"])
    assert confirmation_card["type"] == "audit_confirmation"
    assert confirmation_card["status"] == AuditRecordStatus.PENDING

    async with session_factory() as verification_db:
        audit_records = list((await verification_db.execute(select(AuditRecord).where(AuditRecord.uid == UID, AuditRecord.session_id == SESSION_ID))).scalars().all())
        assert len(audit_records) == 1
        audit_record = audit_records[0]
        assert audit_record.status == AuditRecordStatus.PENDING
        assert audit_record.source == "http"
        assert audit_record.source_assistant_message_id is not None

        details = list((await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record.id))).scalars().all())
        assert len(details) == 1
        detail = details[0]
        assert detail.tool_name == MANAGE_SCHEDULED_TASKS_TOOL_NAME
        assert detail.original_tool_call_id == "scheduled-audited-call"
        assert detail.conclusion == AuditToolConclusion.PENDING
        arguments_summary = json.loads(detail.arguments_summary)
        assert arguments_summary["operation"] == operation
        if operation == "create":
            assert arguments_summary.get("run_once", False) is run_once

        confirmation_messages = list(
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(confirmation_messages) == 1
        tool_results = list(
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.TOOL_RESULT,
                        Message.audit_tool_call_id == "scheduled-audited-call",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(tool_results) == 1
        assert tool_results[0].role == MessageRole.TOOL
        tool_result = InternalMessage.model_validate_json(tool_results[0].content or "")
        assert tool_result.tool_call_id == "scheduled-audited-call"
        assert json.loads(tool_result.content or "")["status"] == AuditRecordStatus.PENDING.value

        scheduled_tasks = list(
            (
                await verification_db.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.uid == UID,
                        ScheduledTask.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        if operation == "create":
            assert scheduled_tasks == []
        else:
            assert len(scheduled_tasks) == 1
            assert scheduled_tasks[0].model_dump(mode="json") == scheduled_task_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saved_execution_state",
    [
        {},
        {
            "stream_requested": True,
            "show_tool_calls": True,
            "expose_tool_call_content": True,
            "context_summary_events_requested": True,
        },
    ],
    ids=["default-state", "saved-output-flags"],
)
@pytest.mark.parametrize(
    "scheduled_message",
    [
        pytest.param("请生成报告并发送给用户", id="original"),
        pytest.param(" \t请生成报告并发送给用户", id="leading-whitespace"),
        pytest.param("请生成报告并发送给用户\n", id="trailing-newline"),
        pytest.param("\t \n请生成报告并发送给用户 \t\r\n", id="surrounding-whitespace"),
    ],
)
async def test_scheduled_task_execution_supports_multiround_and_send_file(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    saved_execution_state: dict[str, Any],
    scheduled_message: str,
) -> None:
    report_file = tmp_path / "scheduled_report.txt"
    report_file.write_text("scheduled task report content", encoding="utf-8")

    session_id = "scheduled-multiround-session"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["tool"]["enabled_tools"] = [SEND_FILE_TO_USER_TOOL_NAME, MANAGE_SCHEDULED_TASKS_TOOL_NAME]
    configs["tool"]["allowed_operation_dirs"] = [str(tmp_path)]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-multiround-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-multiround-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(send_file_to_user_module, "FILE_TOKEN_DIR", tmp_path / "file_download_tokens")
    monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"g" * 32)

    scheduled_task = ScheduledTask(
        name="Scheduled report task",
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        message=scheduled_message,
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db_session.add(scheduled_task)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    assert scheduled_task.id is not None

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(scheduled_task.id)

    work_item = (
        (
            await db_session.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    SessionReplyWorkItem.session_id == session_id,
                )
            )
        )
        .scalars()
        .one()
    )

    worker_id = "worker-multiround"
    work_item.status = SessionReplyWorkStatus.RUNNING
    work_item.locked_by = worker_id
    work_item.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(work_item)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_item.id, worker_id)

    work_item.execution_state = {**(work_item.execution_state or {}), **saved_execution_state}
    db_session.add(work_item)
    await db_session.commit()
    await db_session.refresh(work_item)

    calls: list[dict[str, Any]] = []

    async def mock_create_chat_completion(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        turn = len(calls)
        if turn == 1:
            user_messages = [message for message in kwargs["messages"] if message.role == MessageRole.USER]
            assert len(user_messages) == 1
            payload = json.loads(user_messages[0].content)
            assert payload["user_message"] == scheduled_message.strip()
            assert payload["execution_guidance"] == SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT.strip()
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="内部工具中间正文",
                    tool_calls=[
                        InternalToolCall(
                            id="call-send-file-1",
                            name=SEND_FILE_TO_USER_TOOL_NAME,
                            arguments={"files": [{"path": str(report_file.resolve())}]},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 2:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="call-end-session-2",
                            name=END_SESSION_TOOL_NAME,
                            arguments={"skip_reply": False},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 3:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="报告已生成并发送完成",
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected turn {turn}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "create_chat_completion",
        mock_create_chat_completion,
        raising=False,
    )
    monkeypatch.setattr(LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_create_chat_completion, raising=False)

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(session_notifier_module, "AsyncSessionLocal", session_factory)

    assert work_item.id is not None
    original_send_session_event = executor_lifecycle_module.send_session_event
    send_attempts = 0

    async def fail_first_send(uid: str, session_id: str, event: dict[str, Any]) -> None:
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            raise RuntimeError("simulated notification failure")
        await original_send_session_event(uid, session_id, event)

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", fail_first_send)

    with pytest.raises(RuntimeError, match="simulated notification failure"):
        await executor_lifecycle_module.execute_session_reply_work(work_item.id, worker_id)

    async with session_factory() as failed_delivery_db:
        failed_work = await failed_delivery_db.get(SessionReplyWorkItem, work_item.id)
        assert failed_work is not None
        assert failed_work.status == SessionReplyWorkStatus.RUNNING
        assert failed_work.event_sent is False
        assert failed_work.result_message_id is not None
        failed_result = await failed_delivery_db.get(Message, failed_work.result_message_id)
        assert failed_result is not None
        assert failed_result.work_id is None
        failed_session = await failed_delivery_db.get(ChatSession, session_id)
        assert failed_session is not None
        assert failed_session.latest_message_id == failed_result.id
        failed_model_history = await message_crud.get_history_backward_by_id(failed_delivery_db, session_id=session_id, uid=UID)
        failed_ui_history = await message_crud.get_history_paged(failed_delivery_db, session_id=session_id, uid=UID)
        assert [message.id for message in reversed(failed_model_history)] == [failed_result.id]
        assert [message.id for message in reversed(failed_ui_history)] == [failed_result.id]
        assert list((await failed_delivery_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID))).scalars().all()) == []

    retry_worker_id = "worker-multiround-retry"
    async with session_factory() as retry_db:
        assert await session_reply_work_item_crud.release_for_retry(
            retry_db,
            work_id=work_item.id,
            worker_id=worker_id,
            error="simulated notification failure",
            delay_seconds=0,
        )
        reclaimed = await session_reply_work_item_crud.claim_next(retry_db, worker_id=retry_worker_id, lease_seconds=300)
        assert reclaimed is not None
        assert reclaimed.id == work_item.id

    await executor_lifecycle_module.execute_session_reply_work(work_item.id, retry_worker_id)

    async with session_factory() as verification_db:
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))).scalars().all())
        persisted_work = await verification_db.get(SessionReplyWorkItem, work_item.id)
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())
        session = await verification_db.get(ChatSession, session_id)
        model_history = await message_crud.get_history_backward_by_id(verification_db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(verification_db, session_id=session_id, uid=UID)

    assert persisted_work is not None
    assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert persisted_work.result_message_id is not None
    saved_response = (persisted_work.execution_state or {}).get("response")
    assert isinstance(saved_response, dict)
    assert len(saved_response["files"]) == 1
    sent_file_info = saved_response["files"][0]
    assert sent_file_info["name"] == report_file.name
    assert "id" in sent_file_info

    final_message = next(message for message in persisted_messages if message.id == persisted_work.result_message_id)
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.type == MessageType.TEXT
    assert final_message.work_id is None
    parsed_content = json.loads(final_message.content)
    assert parsed_content["type"] == "assistant_files"
    assert parsed_content["text"] == "报告已生成并发送完成"
    assert len(parsed_content["files"]) == 1
    assert parsed_content["files"][0]["id"] == sent_file_info["id"]

    assert len(calls) == 3
    assert send_attempts == 2
    assert session is not None
    assert session.latest_message_id == final_message.id
    assert [message.content for message in reversed(model_history)] == [final_message.content]
    assert [message.content for message in reversed(ui_history)] == [final_message.content]

    trigger_messages = [message for message in persisted_messages if message.type == MessageType.SCHEDULED_TASK_TRIGGER]
    assert len(trigger_messages) == 1
    assert trigger_messages[0].content == scheduled_message
    assert trigger_messages[0].work_id == work_item.id
    tool_messages = [message for message in persisted_messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT}]
    assert tool_messages
    assert all(message.work_id == work_item.id for message in tool_messages)
    control_messages = [msg for msg in persisted_messages if isinstance(msg.provider_metadata, dict) and msg.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME]
    assert len(control_messages) == 2
    assert all(msg.provider_metadata.get("execution_scope") == END_SESSION_SCOPE_TASK for msg in control_messages)

    assert len(session_events) == 1
    final_event = session_events[0].event
    assert final_event["type"] == "proactive_reply"
    assert final_event["work_id"] == work_item.id
    assert final_event["content"] == "报告已生成并发送完成"
    assert final_event["files"] == saved_response["files"]
    assert final_event["message_id"] == final_message.id
    assert final_event["history"] == []
    assert "内部工具中间正文" not in json.dumps(final_event, ensure_ascii=False)
    assert all(event.event.get("type") != "tool_results_update" for event in session_events)


@pytest.mark.asyncio
async def test_scheduled_task_background_tool_resumes_same_work_with_task_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-background-resume-session"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["channel"]["image_generation_channel"] = {"rules": [{"channel_id": 1, "model_id": "scheduled-image-model", "priority": 1, "weight": 1}]}
    configs["tool"]["enabled_tools"] = [IMAGE_GENERATION_TOOL_NAME, MANAGE_SCHEDULED_TASKS_TOOL_NAME, MANAGE_TODO_TOOL_NAME]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-background-resume-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    },
                    {
                        "model_id": "scheduled-image-model",
                        "usage": "IMAGE_GENERATION",
                        "protocol": "OPENAI_IMAGE",
                        "size": "1024x1024",
                        "quality": "auto",
                    },
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-background-resume-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=False, max_turns=5))
    await db_session.flush()
    scheduled_task = ScheduledTask(
        name="Scheduled background resume",
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        message="run background shell and report",
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db_session.add(scheduled_task)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    assert scheduled_task.id is not None

    for module in (
        channel_cursor_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        external_work_module,
        notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

    calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        if len(calls) == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="scheduled-background-image",
                            name=IMAGE_GENERATION_TOOL_NAME,
                            arguments={"prompt": "scheduled background image"},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if len(calls) == 2:
            resumed_messages: list[InternalMessage] = kwargs["messages"]
            assert any(message.tool_calls and any(tool_call.id == "scheduled-background-image" for tool_call in message.tool_calls) for message in resumed_messages)
            assert any("image-1" in str(message.content) for message in resumed_messages if message.content is not None)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="scheduled background completed"),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected model call {len(calls)}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(scheduled_task.id)

    async with session_factory() as db:
        work = (
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == session_id,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                    )
                )
            )
            .scalars()
            .one()
        )
        assert work.id is not None
        work_id = work.id
        claimed = await session_reply_work_item_crud.claim_next(db, worker_id="scheduled-background-worker-1", lease_seconds=300)
        assert claimed is not None and claimed.id == work_id

    await executor_lifecycle_module.execute_session_reply_work(work_id, "scheduled-background-worker-1")

    async with session_factory() as db:
        waiting = await db.get(SessionReplyWorkItem, work_id)
        assert waiting is not None
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())
        assert [item.id for item in works] == [work_id]
        assert await message_crud.get_history_backward_by_id(db, session_id=session_id, uid=UID) == []
        assert await message_crud.get_history_paged(db, session_id=session_id, uid=UID) == []
        task = (await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == session_id))).scalars().one()
        assert task.id is not None
        assert task.auto_reply is False
        assert task.extra["reply_mode"] == BackgroundTaskReplyMode.RESUME_ORIGIN.value
        task_id = task.id
        claimed_task = await background_task_crud.try_claim(db, task_id=task_id, worker_id="scheduled-background-task-worker", lease_seconds=300)
        assert claimed_task is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=task_id,
            worker_id="scheduled-background-task-worker",
            result={"status": "succeeded", "content": {"files": [{"id": "image-1", "name": "scheduled.png"}]}},
            auto_reply=False,
        )

    async with session_factory() as db:
        foreground_work, _created = await session_reply_work_item_crud.enqueue(
            db,
            uid=UID,
            session_id=session_id,
            profile_id=PROFILE_ID,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="temporary-foreground-input",
            dedupe_key="foreground-message:scheduled-background-isolation",
            commit=False,
        )
        await db.flush()
        await db.refresh(foreground_work)
        assert foreground_work.id is not None
        foreground_worker_id = "scheduled-foreground-isolation-worker"
        claimed_foreground = await session_reply_work_item_crud.claim_next(db, worker_id=foreground_worker_id, lease_seconds=300)
        assert claimed_foreground is not None
        assert claimed_foreground.id == foreground_work.id
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (foreground_work.id, foreground_worker_id)
        assert await result_delivery_module.receive_pending_external_results(db, uid=UID, session_id=session_id, profile_id=PROFILE_ID) == []
        task = await db.get(BackgroundTask, task_id)
        assert task is not None
        reply_owner = task.extra.get("reply_owner") if isinstance(task.extra, dict) else None
        assert not (isinstance(reply_owner, dict) and reply_owner.get("work_id") == foreground_work.id)
        assert await session_reply_work_item_crud.cancel_session(db, session_id=session_id, uid=UID, interactive_only=True) == 1
        cancelled_foreground = await db.get(SessionReplyWorkItem, foreground_work.id)
        assert cancelled_foreground is not None
        assert cancelled_foreground.status == SessionReplyWorkStatus.CANCELLED

    assert await resume_external_work_for_task(task_id) is True

    async with session_factory() as db:
        resumed = await db.get(SessionReplyWorkItem, work_id)
        assert resumed is not None
        assert resumed.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert await message_crud.get_history_backward_by_id(db, session_id=session_id, uid=UID) == []
        assert await message_crud.get_history_paged(db, session_id=session_id, uid=UID) == []
        resumed_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID))).scalars().all())
        assert all(event.event.get("type") != "tool_results_update" for event in resumed_events)
        claimed = await session_reply_work_item_crud.claim_next(db, worker_id="scheduled-background-worker-2", lease_seconds=300)
        assert claimed is not None and claimed.id == work_id

    await executor_lifecycle_module.execute_session_reply_work(work_id, "scheduled-background-worker-2")

    async with session_factory() as db:
        finished = await db.get(SessionReplyWorkItem, work_id)
        assert finished is not None
        assert finished.status == SessionReplyWorkStatus.SUCCEEDED
        agent_works = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == session_id,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [item.id for item in agent_works] == [work_id]
        result_message = await db.get(Message, finished.result_message_id)
        assert result_message is not None
        assert result_message.content == "scheduled background completed"
        assert result_message.work_id is None
        session = await db.get(ChatSession, session_id)
        assert session is not None
        assert session.latest_message_id == result_message.id
        model_history = await message_crud.get_history_backward_by_id(db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(db, session_id=session_id, uid=UID)
        assert [message.content for message in reversed(model_history)] == [result_message.content]
        assert [message.content for message in reversed(ui_history)] == [result_message.content]
        session_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())

    assert [event.event["type"] for event in session_events] == ["work_ready", "proactive_reply"]
    assert all(event.event.get("type") != "tool_results_update" for event in session_events)
    final_event = session_events[-1].event
    assert final_event["work_id"] == work_id
    assert final_event["content"] == "scheduled background completed"
    assert final_event["files"] == []
    assert final_event["history"] == []

    assert len(calls) == 2
    for call in calls:
        tool_names = {tool["function"]["name"] for tool in call["tools"]}
        assert IMAGE_GENERATION_TOOL_NAME in tool_names
        assert MANAGE_SCHEDULED_TASKS_TOOL_NAME not in tool_names
        assert MANAGE_TODO_TOOL_NAME not in tool_names


@pytest.mark.asyncio
async def test_scheduled_agent_work_skip_reply_completes_silently_without_affecting_foreground(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-agent-skip-reply-session"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-agent-skip-reply-profile", configs=ProfileConfig.model_validate({}).model_dump()),
            ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID),
        ]
    )
    await db_session.commit()

    agent_work, _created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="skip-reply-trigger",
        dedupe_key="scheduled-agent-work:skip-reply",
        spec=AgentWorkSpec(
            instruction="无需回复的计划任务",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
        commit=False,
    )
    await db_session.commit()
    await db_session.refresh(agent_work)
    assert agent_work.id is not None

    worker_id = "scheduled-agent-skip-reply-worker"
    claimed_agent_work = await session_reply_work_item_crud.claim_next(db_session, worker_id=worker_id, lease_seconds=300)
    assert claimed_agent_work is not None
    assert claimed_agent_work.id == agent_work.id
    claimed_agent_work.execution_state = {
        **(claimed_agent_work.execution_state or {}),
        "response": {
            "skip_reply": True,
            "choices": [],
            "history": [],
            "files": None,
        },
    }
    db_session.add(claimed_agent_work)

    foreground_work, _created = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="parallel-foreground-input",
        dedupe_key="foreground-message:parallel-skip-reply",
        commit=False,
    )
    await db_session.commit()
    await db_session.refresh(foreground_work)
    assert foreground_work.id is not None

    for module in (executor_lifecycle_module, notifier_module, session_notifier_module):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)

    await executor_lifecycle_module.execute_session_reply_work(agent_work.id, worker_id)

    async with session_factory() as verification_db:
        finished = await verification_db.get(SessionReplyWorkItem, agent_work.id)
        assert finished is not None
        assert finished.status == SessionReplyWorkStatus.SUCCEEDED
        assert finished.result_message_id is None
        session = await verification_db.get(ChatSession, session_id)
        assert session is not None
        assert session.latest_message_id is None
        model_history = await message_crud.get_history_backward_by_id(verification_db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(verification_db, session_id=session_id, uid=UID)
        assert model_history == []
        assert ui_history == []
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID))).scalars().all())
        assert session_events == []
        foreground_after = await verification_db.get(SessionReplyWorkItem, foreground_work.id)
        assert foreground_after is not None
        assert foreground_after.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert foreground_after.result_message_id is None


@pytest.mark.asyncio
async def test_scheduled_task_execution_excludes_scheduled_and_todo_tools(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-tool-filter-session"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["tool"]["enabled_tools"] = [MANAGE_SCHEDULED_TASKS_TOOL_NAME]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-tool-filter-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-tool-filter-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    trigger_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=PROFILE_ID,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="检查定时任务工具过滤",
        is_processed=True,
    )
    db_session.add(trigger_message)
    await db_session.flush()
    await db_session.refresh(trigger_message)

    work_item, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        trigger_message=trigger_message,
    )
    await db_session.flush()
    await db_session.refresh(work_item)

    worker_id = "worker-tool-filter"
    work_item.status = SessionReplyWorkStatus.RUNNING
    work_item.locked_by = worker_id
    work_item.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(work_item)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_item.id, worker_id)

    captured_tools: list[dict[str, Any]] = []

    async def mock_create_chat_completion(**kwargs: Any) -> InternalResponse:
        captured_tools.extend(kwargs.get("tools") or [])
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="定时任务检查已完成"),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "create_chat_completion",
        mock_create_chat_completion,
        raising=False,
    )
    monkeypatch.setattr(LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_create_chat_completion, raising=False)

    response = await executor_agent_work_module.execute_agent_work(db_session, work_item, worker_id)

    assert captured_tools
    captured_tool_names = {tool["function"]["name"] for tool in captured_tools}
    assert MANAGE_SCHEDULED_TASKS_TOOL_NAME not in captured_tool_names
    assert MANAGE_TODO_TOOL_NAME not in captured_tool_names
    assert END_SESSION_TOOL_NAME in captured_tool_names
    assert response["content"] == "定时任务检查已完成"


@pytest.mark.asyncio
async def test_scheduled_agent_work_ignores_all_conversation_history(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-history-boundary-session"
    profile_id = 201
    model_id = "boundary-model"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="boundary-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=profile_id, uid=UID, name="boundary-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    prior_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="历史前置消息",
        is_processed=True,
    )
    db_session.add(prior_message)
    await db_session.flush()
    await db_session.refresh(prior_message)

    trigger_a = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="A任务触发指令",
        is_processed=True,
    )
    db_session.add(trigger_a)
    await db_session.flush()
    await db_session.refresh(trigger_a)

    trigger_b = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="B任务指令特征词",
        is_processed=True,
    )
    db_session.add(trigger_b)
    await db_session.flush()
    await db_session.refresh(trigger_b)

    assert trigger_a.id is not None and trigger_b.id is not None
    assert trigger_a.id < trigger_b.id

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    session.context_summary = "共享总结已覆盖B任务指令特征词"
    session.context_summary_message_id = trigger_b.id
    db_session.add(session)

    work_item_a, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        trigger_message=trigger_a,
    )
    await db_session.flush()
    await db_session.refresh(work_item_a)

    worker_id = "worker-boundary"
    work_item_a.status = SessionReplyWorkStatus.RUNNING
    work_item_a.locked_by = worker_id
    work_item_a.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(work_item_a)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_item_a.id, worker_id)

    captured_calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        captured_calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="A任务处理完毕"),
            model=model_id,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "create_chat_completion", mock_generate, raising=False)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_generate, raising=False)

    response = await executor_agent_work_module.execute_agent_work(db_session, work_item_a, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert any("A任务触发指令" in content for content in message_contents)
    assert not any("历史前置消息" in content for content in message_contents)
    assert not any("B任务指令" in content for content in message_contents)
    assert not any("共享总结已覆盖" in content for content in message_contents)
    assert {m.id for m in llm_messages if m.id is not None} == {trigger_a.id}
    assert response["content"] == "A任务处理完毕"


@pytest.mark.asyncio
async def test_scheduled_agent_work_reload_history_resume_stays_context_free(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-history-boundary-reload-session"
    profile_id = 203
    model_id = "boundary-reload-model"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="boundary-reload-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=profile_id, uid=UID, name="boundary-reload-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    prior_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="历史前置消息",
        is_processed=True,
    )
    db_session.add(prior_message)
    await db_session.flush()
    await db_session.refresh(prior_message)

    trigger_a = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="A任务触发指令",
        is_processed=True,
    )
    db_session.add(trigger_a)
    await db_session.flush()
    await db_session.refresh(trigger_a)

    trigger_b = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="B任务指令特征词",
        is_processed=True,
    )
    db_session.add(trigger_b)
    await db_session.flush()
    await db_session.refresh(trigger_b)

    assert trigger_a.id is not None and trigger_b.id is not None
    assert trigger_a.id < trigger_b.id

    work, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        trigger_message=trigger_a,
    )
    await db_session.flush()
    await db_session.refresh(work)

    worker_id = "worker-boundary-reload"
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = int(get_local_time().timestamp()) + 300
    work.execution_state = dict(work.execution_state or {})
    work.execution_state["dispatcher_checkpoint"] = {
        "messages": [],
        "turn_messages": [],
        "reload_history": True,
        "context_summary_trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE.value,
        "context_summary_fixed_upper_message_id": trigger_a.id,
    }
    await db_session.commit()
    await db_session.refresh(work)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)

    captured_calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        captured_calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="A任务处理完毕"),
            model=model_id,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "create_chat_completion", mock_generate, raising=False)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_generate, raising=False)

    response = await executor_agent_work_module.execute_agent_work(db_session, work, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert any("A任务触发指令" in content for content in message_contents)
    assert not any("历史前置消息" in content for content in message_contents)
    assert not any("B任务指令特征词" in content for content in message_contents)
    assert {m.id for m in llm_messages if m.id is not None} == {trigger_a.id}
    assert response["content"] == "A任务处理完毕"


@pytest.mark.asyncio
async def test_scheduled_task_does_not_hijack_foreground_external_results(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-no-hijack-external-results-session"
    profile_id = 202
    model_id = "no-hijack-model"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="no-hijack-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=profile_id, uid=UID, name="no-hijack-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    fg_user_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="前台用户指令",
        is_processed=True,
    )
    db_session.add(fg_user_message)
    await db_session.flush()
    await db_session.refresh(fg_user_message)

    fg_tool_call_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps({"tool_calls": [{"id": "bg-call-1", "name": "bg_tool", "arguments": {}}]}),
        is_processed=True,
    )
    db_session.add(fg_tool_call_message)
    await db_session.flush()
    await db_session.refresh(fg_tool_call_message)

    fg_tool_res_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=json.dumps({"tool_call_id": "bg-call-1", "content": json.dumps({"status": "queued", "task_id": 999})}),
        is_processed=True,
    )
    db_session.add(fg_tool_res_message)
    await db_session.flush()
    await db_session.refresh(fg_tool_res_message)

    fg_work, _ = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=str(fg_user_message.id),
        dedupe_key=f"foreground-message:{fg_user_message.id}",
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(fg_work)
    fg_work.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    fg_work.execution_state = {
        "pending_tools": [
            {
                "task_id": 999,
                "tool_call_id": "bg-call-1",
                "tool_name": "bg_tool",
                "tool_result_message_id": fg_tool_res_message.id,
            }
        ]
    }
    await db_session.commit()
    await db_session.refresh(fg_work)

    bg_task = BackgroundTask(
        id=999,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        tool_call_id="bg-call-1",
        tool_name="bg_tool",
        status=BackgroundTaskStatus.SUCCEEDED,
        result={"content": "前台专属后台任务结果特征词"},
        auto_reply=False,
        reply_status=BackgroundTaskReplyStatus.NONE,
        extra={
            "origin_work_id": fg_work.id,
            "origin_work_identity": fg_work.dedupe_key,
        },
    )
    db_session.add(bg_task)

    scheduled_trigger = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="定时任务触发检查",
        is_processed=True,
    )
    db_session.add(scheduled_trigger)
    await db_session.flush()
    await db_session.refresh(scheduled_trigger)

    bg_result_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.BACKGROUND_TASK_RESULT,
        content=json.dumps(
            {
                "type": "background_tool_result",
                "task_id": 999,
                "tool_call_id": "bg-call-1",
                "instruction": "前台专用后台结果指令",
                "task": {"status": "succeeded", "result": "前台专属后台任务结果特征词"},
            }
        ),
        is_processed=True,
    )
    db_session.add(bg_result_message)
    await db_session.commit()
    await db_session.refresh(bg_result_message)

    assert scheduled_trigger.id is not None and bg_result_message.id is not None
    assert bg_result_message.id > scheduled_trigger.id

    scheduled_work, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        trigger_message=scheduled_trigger,
    )
    await db_session.flush()
    await db_session.refresh(scheduled_work)

    worker_id = "worker-task-external"
    scheduled_work.status = SessionReplyWorkStatus.RUNNING
    scheduled_work.locked_by = worker_id
    scheduled_work.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(scheduled_work)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (scheduled_work.id, worker_id)

    captured_calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        captured_calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="定时任务正常回复"),
            model=model_id,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "create_chat_completion", mock_generate, raising=False)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_generate, raising=False)

    response = await executor_agent_work_module.execute_agent_work(db_session, scheduled_work, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert not any("前台专属后台任务结果特征词" in content for content in message_contents)
    assert not any("前台专用后台结果指令" in content for content in message_contents)
    assert not any(m.type == MessageType.BACKGROUND_TASK_RESULT for m in llm_messages if hasattr(m, "type"))
    assert not any(m.id == bg_result_message.id for m in llm_messages if m.id is not None)
    assert "前台专属后台任务结果特征词" not in response["content"]

    refreshed_bg_task = await db_session.get(BackgroundTask, 999)
    assert refreshed_bg_task is not None
    reply_owner = refreshed_bg_task.extra.get("reply_owner")
    assert reply_owner is None or reply_owner.get("work_id") != scheduled_work.id

    refreshed_fg_work = await db_session.get(SessionReplyWorkItem, fg_work.id)
    assert refreshed_fg_work is not None
    assert refreshed_fg_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
