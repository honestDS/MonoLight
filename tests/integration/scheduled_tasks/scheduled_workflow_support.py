from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.background_tasks.scheduler as scheduler_module
import app.models  # noqa: F401
from app.core.constants import END_SESSION_SCOPE_TASK
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.message import Message
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem
from app.models.user import User

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
        is_default=True,
    )
    db.add_all([User(uid=UID, username=UID), profile])
    await db.flush()
    db.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    await db.flush()

    scheduled_task = ScheduledTask(
        name="One-time reminder",
        uid=UID,
        session_id=SESSION_ID,
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
