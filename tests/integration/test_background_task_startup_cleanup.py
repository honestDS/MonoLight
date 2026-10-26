from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.background_tasks import recovery as recovery_module
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User

UID = "startup-cleanup-user"
SESSION_ID = "startup-cleanup-session"
PROFILE_ID = 1
LOCK_UNTIL = 4_000_000_000

STARTUP_TABLES = [
    PromptLibrary.__table__,
    User.__table__,
    Profile.__table__,
    ChatSession.__table__,
    BackgroundTask.__table__,
    SessionReplySequence.__table__,
    SessionReplyWorkItem.__table__,
]

TASK_PRESERVED_FIELDS = (
    "status",
    "result",
    "error",
    "arguments",
    "extra",
    "attempt_count",
    "created_at",
    "started_at",
    "finished_at",
    "locked_by",
    "lock_until",
)


@pytest_asyncio.fixture
async def startup_session_factory(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(tables=STARTUP_TABLES, foreign_keys=True) as session_factory:
        async with session_factory() as db:
            db.add_all(
                [
                    User(uid=UID, username=UID),
                    Profile(id=PROFILE_ID, uid=UID, name="startup-cleanup-profile", configs={}),
                    ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID),
                ]
            )
            await db.commit()

        monkeypatch.setattr(recovery_module, "AsyncSessionLocal", session_factory)
        yield session_factory


def _task_snapshot(task: BackgroundTask) -> dict[str, Any]:
    return deepcopy(
        {
            "status": task.status,
            "result": task.result,
            "error": task.error,
            "arguments": task.arguments,
            "auto_reply": task.auto_reply,
            "reply_status": task.reply_status,
            "locked_by": task.locked_by,
            "lock_until": task.lock_until,
            "reply_locked_by": task.reply_locked_by,
            "reply_lock_until": task.reply_lock_until,
            "attempt_count": task.attempt_count,
            "extra": task.extra,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
        }
    )


def _work_snapshot(work: SessionReplyWorkItem) -> dict[str, Any]:
    return deepcopy(
        {
            "sequence_no": work.sequence_no,
            "status": work.status,
            "execution_state": work.execution_state,
            "event_sent": work.event_sent,
            "locked_by": work.locked_by,
            "lock_until": work.lock_until,
            "attempt_count": work.attempt_count,
            "max_attempts": work.max_attempts,
            "available_at": work.available_at,
            "error": work.error,
            "created_at": work.created_at,
            "updated_at": work.updated_at,
        }
    )


@pytest.mark.asyncio
async def test_startup_cleanup_cancels_old_background_tasks_and_is_idempotent(startup_session_factory) -> None:
    created_at = datetime(2026, 1, 1, 12, 0, 0)
    tasks = [
        BackgroundTask(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="startup-pending",
            tool_name="shell",
            status=BackgroundTaskStatus.PENDING,
            arguments={"command": "pending"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            locked_by="stale-task-worker",
            lock_until=LOCK_UNTIL,
            reply_locked_by="stale-reply-worker",
            reply_lock_until=LOCK_UNTIL,
            extra={"case": "pending"},
            created_at=created_at,
        ),
        BackgroundTask(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="startup-running",
            tool_name="shell",
            status=BackgroundTaskStatus.RUNNING,
            arguments={"command": "running"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.RUNNING,
            locked_by="running-task-worker",
            lock_until=LOCK_UNTIL,
            reply_locked_by="running-reply-worker",
            reply_lock_until=LOCK_UNTIL,
            attempt_count=1,
            extra={"case": "running"},
            created_at=created_at + timedelta(seconds=1),
            started_at=created_at + timedelta(seconds=2),
        ),
        BackgroundTask(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="startup-succeeded-pending-reply",
            tool_name="shell",
            status=BackgroundTaskStatus.SUCCEEDED,
            arguments={"command": "succeeded"},
            result={"content": "completed result"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            extra={"case": "succeeded-pending-reply", "preserve": True},
            created_at=created_at + timedelta(seconds=3),
            started_at=created_at + timedelta(seconds=4),
            finished_at=created_at + timedelta(seconds=5),
        ),
        BackgroundTask(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="startup-failed-running-reply",
            tool_name="shell",
            status=BackgroundTaskStatus.FAILED,
            arguments={"command": "failed"},
            result={"status": "failed"},
            error="old failure",
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.RUNNING,
            reply_locked_by="failed-reply-worker",
            reply_lock_until=LOCK_UNTIL,
            extra={"case": "failed-running-reply", "preserve": True},
            created_at=created_at + timedelta(seconds=6),
            started_at=created_at + timedelta(seconds=7),
            finished_at=created_at + timedelta(seconds=8),
        ),
        BackgroundTask(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="startup-succeeded-replied",
            tool_name="shell",
            status=BackgroundTaskStatus.SUCCEEDED,
            arguments={"command": "already-replied"},
            result={"content": "already delivered"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
            extra={"case": "already-replied", "preserve": True},
            created_at=created_at + timedelta(seconds=9),
            started_at=created_at + timedelta(seconds=10),
            finished_at=created_at + timedelta(seconds=11),
        ),
    ]

    async with startup_session_factory() as db:
        db.add_all(tasks)
        await db.commit()
        before = {task.tool_call_id: _task_snapshot(task) for task in tasks}
        task_ids = {task.tool_call_id: task.id for task in tasks}

    cleared_count = await recovery_module.clear_background_tasks_on_startup()
    assert cleared_count == 4

    async with startup_session_factory() as db:
        stored_tasks = {task.tool_call_id: task for task in (await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all()}
        active_tasks = await background_task_crud.list_active_user_tasks(db, uid=UID, session_id=SESSION_ID)
        pending_replies = await background_task_crud.list_pending_replies(db)

    assert active_tasks == []
    assert pending_replies == []

    for task_name in ("startup-pending", "startup-running"):
        task = stored_tasks[task_name]
        assert task.status == BackgroundTaskStatus.CANCELLED
        assert task.auto_reply is False
        assert task.reply_status == BackgroundTaskReplyStatus.NONE
        assert task.locked_by is None
        assert task.lock_until is None
        assert task.reply_locked_by is None
        assert task.reply_lock_until is None
        assert task.finished_at is not None

    for task_name in ("startup-succeeded-pending-reply", "startup-failed-running-reply"):
        completed_pending_reply = stored_tasks[task_name]
        completed_pending_reply_snapshot = _task_snapshot(completed_pending_reply)
        assert {field: completed_pending_reply_snapshot[field] for field in TASK_PRESERVED_FIELDS} == {field: before[task_name][field] for field in TASK_PRESERVED_FIELDS}
        assert completed_pending_reply.auto_reply is False
        assert completed_pending_reply.reply_status == BackgroundTaskReplyStatus.NONE
        assert completed_pending_reply.reply_locked_by is None
        assert completed_pending_reply.reply_lock_until is None

    already_replied = stored_tasks["startup-succeeded-replied"]
    assert _task_snapshot(already_replied) == before["startup-succeeded-replied"]

    assert all(task_id is not None for task_id in task_ids.values())
    assert {task.id for task in stored_tasks.values()} == set(task_ids.values())

    second_cleared_count = await recovery_module.clear_background_tasks_on_startup()
    assert second_cleared_count == 0


@pytest.mark.asyncio
async def test_startup_cleanup_cancels_background_summary_and_external_waiting_work(
    startup_session_factory,
) -> None:
    expected_error = t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)
    async with startup_session_factory() as db:
        sequence = SessionReplySequence(session_id=SESSION_ID, next_sequence_no=3)
        summary = SessionReplyWorkItem(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            sequence_no=1,
            work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id="background-summary-source",
            dedupe_key="startup-background-summary",
            status=SessionReplyWorkStatus.RUNNING,
            execution_state={"summary": "in progress"},
            locked_by="summary-worker",
            lock_until=LOCK_UNTIL,
            attempt_count=1,
        )
        waiting = SessionReplyWorkItem(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            sequence_no=2,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="waiting-message-source",
            dedupe_key="startup-external-waiting",
            status=SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            execution_state={"request_ids": ["external-request"]},
            locked_by="external-worker",
            lock_until=LOCK_UNTIL,
        )
        db.add_all([sequence, summary, waiting])
        await db.commit()
        summary_id = summary.id
        waiting_id = waiting.id

    assert summary_id is not None
    assert waiting_id is not None
    first_cleared_count = await recovery_module.clear_background_tasks_on_startup()
    assert first_cleared_count == 2

    async with startup_session_factory() as db:
        stored_summary = await db.get(SessionReplyWorkItem, summary_id)
        stored_waiting = await db.get(SessionReplyWorkItem, waiting_id)

    assert stored_summary is not None
    assert stored_waiting is not None
    assert stored_summary.status == SessionReplyWorkStatus.CANCELLED
    assert stored_summary.locked_by is None
    assert stored_summary.lock_until is None
    assert stored_summary.error == expected_error
    assert stored_waiting.status == SessionReplyWorkStatus.CANCELLED
    assert stored_waiting.status != SessionReplyWorkStatus.READY_FOR_LLM
    assert stored_waiting.locked_by is None
    assert stored_waiting.lock_until is None
    assert stored_waiting.error == expected_error
    first_snapshots = {
        "summary": _work_snapshot(stored_summary),
        "waiting": _work_snapshot(stored_waiting),
    }

    second_cleared_count = await recovery_module.clear_background_tasks_on_startup()
    assert second_cleared_count == 0

    async with startup_session_factory() as db:
        second_snapshots = {
            "summary": _work_snapshot(await db.get(SessionReplyWorkItem, summary_id)),
            "waiting": _work_snapshot(await db.get(SessionReplyWorkItem, waiting_id)),
        }
    assert second_snapshots == first_snapshots
