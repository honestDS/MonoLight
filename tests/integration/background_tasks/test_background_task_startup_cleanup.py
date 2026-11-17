import json
from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.providers.database as database_provider
from app.core.background_tasks import recovery as recovery_module
from app.core.constants import (
    ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION,
    ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY,
)
from app.core.context import ContextManager
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.session.notifier import session_notifier
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor_audit as executor_audit_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.utils.context_summary.boundary import ContextSummaryTriggerMode, resolve_context_summary_boundary
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot, iter_persistent_summary_rounds
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from tests.integration.chat.test_chat_stop_workflow import _seed_tool_call_stop_case

UID = "startup-cleanup-user"
SESSION_ID = "startup-cleanup-session"
PROFILE_ID = 1
LOCK_UNTIL = 4_000_000_000

STARTUP_TABLES = [
    PromptLibrary.__table__,
    User.__table__,
    Profile.__table__,
    ChatSession.__table__,
    Message.__table__,
    SessionEvent.__table__,
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


@pytest.mark.asyncio
async def test_startup_cleanup_cancels_background_resume_and_fills_missing_tool_result(
    startup_session_factory,
) -> None:
    worker_id = "startup-background-resume-worker"
    created_at = datetime(2026, 1, 1, 12, 0, 0)
    successful_call = InternalToolCall(id="startup-success-call", name="completed_tool", arguments={"value": 1})
    missing_call = InternalToolCall(id="startup-missing-call", name="interrupted_tool", arguments={"value": 2})

    async with startup_session_factory() as db:
        previous_user = Message(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="processed user message",
            is_processed=True,
            created_at=created_at,
        )
        db.add(previous_user)
        await db.flush()

        work = SessionReplyWorkItem(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            sequence_no=1,
            work_type=SessionReplyWorkType.BACKGROUND_TASK_RESUME,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id="startup-background-task",
            dedupe_key="startup-background-resume",
            status=SessionReplyWorkStatus.RUNNING,
            execution_state={"show_tool_calls": True},
            locked_by=worker_id,
            lock_until=LOCK_UNTIL,
            attempt_count=1,
            created_at=created_at,
        )
        db.add(work)
        await db.commit()
        assert previous_user.id is not None
        assert work.id is not None
        work_id = work.id
        previous_user_id = previous_user.id

    async with startup_session_factory() as db:
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)
        assistant = await save_assistant_message(
            db,
            SESSION_ID,
            UID,
            PROFILE_ID,
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[successful_call, missing_call]),
            dedupe_key="startup-background-resume-assistant",
        )
        successful_result = await save_tool_response(
            db,
            SESSION_ID,
            UID,
            PROFILE_ID,
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=successful_call.id,
                tool_execution_status="succeeded",
                content=json.dumps({"status": "succeeded", "value": "stable result"}),
            ),
            [],
            [],
        )
        assert assistant.id is not None
        assert successful_result.id is not None
        successful_row = await db.get(Message, successful_result.id)
        assert successful_row is not None
        successful_content = successful_row.content

        current_user = Message(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="current unprocessed user message",
            is_processed=False,
        )
        db.add(current_user)
        await db.commit()
        assert current_user.id is not None
        assistant_id = assistant.id
        successful_result_id = successful_result.id
        current_user_id = current_user.id

    async with startup_session_factory() as db:
        blocked = await resolve_context_summary_boundary(
            db,
            session_id=SESSION_ID,
            uid=UID,
            expected_summary_message_id=None,
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=current_user_id,
        )
    assert blocked.target_message_id is None

    cleared_count = await recovery_module.clear_background_tasks_on_startup()
    assert cleared_count == 1

    async with startup_session_factory() as db:
        stored_work = await db.get(SessionReplyWorkItem, work_id)
        messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
        events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID).order_by(SessionEvent.id))).scalars().all())
        after_boundary = await resolve_context_summary_boundary(
            db,
            session_id=SESSION_ID,
            uid=UID,
            expected_summary_message_id=None,
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=current_user_id,
        )

    assert stored_work is not None
    assert stored_work.status == SessionReplyWorkStatus.CANCELLED
    assert stored_work.locked_by is None
    assert stored_work.lock_until is None
    assert stored_work.error == t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)

    tool_results = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assert len(tool_results) == 2
    result_by_call_id = {InternalMessage.model_validate_json(message.content or "{}").tool_call_id: message for message in tool_results}
    assert set(result_by_call_id) == {successful_call.id, missing_call.id}

    stored_success = result_by_call_id[successful_call.id]
    assert stored_success.content == successful_content
    parsed_success = InternalMessage.model_validate_json(stored_success.content or "{}")
    assert parsed_success.tool_execution_status == "succeeded"
    assert parsed_success.content == json.dumps({"status": "succeeded", "value": "stable result"})

    stored_missing = result_by_call_id[missing_call.id]
    assert stored_missing.provider_metadata == {TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY: assistant_id}
    missing_payload = json.loads(stored_missing.content or "{}")
    assert missing_payload["tool_cancelled"] is True
    assert missing_payload["tool_execution_status"] == "execution_unknown"
    assert json.loads(missing_payload["content"]) == {
        "type": "tool_cancellation",
        "status": "cancelled",
        "error": PROMPT_TOOL_INTERRUPTED,
    }

    assert not [message for message in messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
    assert len(events) == 1
    event = events[0].event
    assert event["type"] == "tool_results_update"
    assert event["work_id"] == work_id
    assert len(event["messages"]) == 1
    assert event["messages"][0]["id"] == stored_missing.id
    assert event["messages"][0]["content"] == stored_missing.content

    assert after_boundary.target_message_id == successful_result_id
    assert after_boundary.target_message_id < current_user_id
    assert after_boundary.covered_user_message_id == previous_user_id
    assert len(after_boundary.late_cancelled_tool_results) == 1
    late_source_id, late_result = after_boundary.late_cancelled_tool_results[0]
    assert late_source_id == assistant_id
    assert late_result.id == stored_missing.id
    assert late_result.tool_call_id == missing_call.id
    assert late_result.tool_execution_status == "execution_unknown"
    assert json.loads(late_result.content or "{}") == {
        "type": "tool_cancellation",
        "status": "cancelled",
        "error": PROMPT_TOOL_INTERRUPTED,
    }

    message_snapshot = [(message.id, message.type, message.content) for message in messages]
    event_snapshot = [(item.id, deepcopy(item.event)) for item in events]
    second_cleared_count = await recovery_module.clear_background_tasks_on_startup()
    assert second_cleared_count == 0

    async with startup_session_factory() as db:
        second_messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
        second_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID).order_by(SessionEvent.id))).scalars().all())

    assert [(message.id, message.type, message.content) for message in second_messages] == message_snapshot
    assert [(item.id, item.event) for item in second_events] == event_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_started",
    [False, True],
    ids=["pending-background-task", "started-background-task"],
)
@pytest.mark.parametrize(
    "startup_cleanup",
    [False, True],
    ids=["without-startup-cleanup", "with-startup-cleanup"],
)
async def test_expired_recovery_preserves_or_cancels_background_task_by_startup_cleanup(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    task_started: bool,
    startup_cleanup: bool,
) -> None:
    session_id = f"startup-recovery-background-{task_started}"

    if startup_cleanup:
        expected_task_status = BackgroundTaskStatus.CANCELLED
    elif task_started:
        expected_task_status = BackgroundTaskStatus.RUNNING
    else:
        expected_task_status = BackgroundTaskStatus.PENDING

    async def capture_session_event(_uid: str, _session_id: str, _event: dict[str, Any]) -> None:
        return None

    async def capture_notification(_uid: str, _session_id: str, _event: dict[str, Any], **_kwargs: Any) -> bool:
        return True

    async with database_factory(foreign_keys=True) as session_factory:
        async with session_factory() as db:
            db.add_all([User(uid="owner", username="owner"), Profile(id=1, uid="owner", name="startup-recovery-profile", configs={})])
            await db.commit()

        monkeypatch.setattr(recovery_module, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(database_provider, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(consumer_module, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(executor_audit_module, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)
        monkeypatch.setattr(session_notifier, "notify", capture_notification)

        case = await _seed_tool_call_stop_case(session_factory, session_id=session_id, stream_requested=False, show_tool_calls=True, save_queued_result=False)
        work_id = case["work_id"]
        successful_call, queued_call, missing_call = case["tool_calls"]

        async with session_factory() as db:
            work = await db.get(SessionReplyWorkItem, work_id)
            assert work is not None
            messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
            previous_user = next(message for message in messages if message.content == "hold this reply")
            source = next(message for message in messages if message.type == MessageType.TOOL_CALL)
            successful_result = next(message for message in messages if message.type == MessageType.TOOL_RESULT and InternalMessage.model_validate_json(message.content or "{}").tool_call_id == successful_call.id)
            previous_user.is_processed = True
            current_user = Message(session_id=session_id, uid="owner", profile_id=1, role=MessageRole.USER, type=MessageType.TEXT, content="current unprocessed user message", is_processed=False)
            db.add(current_user)
            await db.flush()
            assert source.id is not None and successful_result.id is not None and current_user.id is not None
            assert work.execution_state.get("tool_call_message_ids") == [source.id]
            previous_user_id = previous_user.id
            source_id = source.id
            source_content = source.content
            successful_result_id = successful_result.id
            successful_result_content = successful_result.content
            current_user_id = current_user.id
            origin_work_identity = work.dedupe_key
            await db.commit()

        async with session_factory() as db:
            task = await background_task_crud.create_task(
                db,
                uid="owner",
                session_id=session_id,
                profile_id=1,
                tool_call_id=queued_call.id,
                tool_name=queued_call.name,
                arguments=queued_call.arguments,
                auto_reply=False,
                extra={
                    "origin_work_id": work_id,
                    "origin_work_identity": origin_work_identity,
                },
            )
            assert task.id is not None
            task_id = task.id
            if task_started:
                assert await background_task_crud.try_claim(db, task_id=task_id, worker_id="background-task-worker") is not None

        if startup_cleanup:
            assert await recovery_module.clear_background_tasks_on_startup() == 1

        async with session_factory() as db:
            stored_task = await db.get(BackgroundTask, task_id)
            stored_work = await db.get(SessionReplyWorkItem, work_id)
        assert stored_task is not None and stored_work is not None
        assert stored_task.status == expected_task_status
        task_snapshot_before_recovery = _task_snapshot(stored_task)
        assert stored_work.status == SessionReplyWorkStatus.RUNNING

        async with session_factory() as db:
            work = await db.get(SessionReplyWorkItem, work_id)
            assert work is not None
            work.lock_until = 0
            db.add(SessionReplyStreamEvent(work_id=work_id, sequence_no=1, event={"type": "tool_start"}))
            await db.commit()

        await consumer_module.SessionReplyConsumer()._recover_expired()

        async with session_factory() as db:
            stored_work = await db.get(SessionReplyWorkItem, work_id)
            stored_task = await db.get(BackgroundTask, task_id)
            session = await db.get(ChatSession, session_id)
            messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
            events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all())
            late_work = await session_reply_work_item_crud.claim_next(db, worker_id="late-reply-worker", lease_seconds=300)
            message_snapshot = [(message.id, message.type, message.content) for message in messages]
            event_snapshot = [(item.id, deepcopy(item.event)) for item in events]

        assert stored_work.status == SessionReplyWorkStatus.FAILED
        assert late_work is None
        assert _task_snapshot(stored_task) == task_snapshot_before_recovery
        assert (
            session.context_summary,
            session.context_summary_revision,
            session.context_content_revision,
        ) == ("summary before stop", 7, 11)

        tool_results = {InternalMessage.model_validate_json(message.content or "{}").tool_call_id: message for message in messages if message.type == MessageType.TOOL_RESULT}
        assert set(tool_results) == {successful_call.id, queued_call.id, missing_call.id}

        assert next(message for message in messages if message.id == source_id).content == source_content
        assert tool_results[successful_call.id].content == successful_result_content
        expected_receipt_call_ids = (queued_call.id, missing_call.id)
        receipt_statuses: dict[str, str] = {}
        receipt_contents: dict[str, dict[str, Any]] = {}
        for call_id in expected_receipt_call_ids:
            result = tool_results[call_id]
            payload = json.loads(result.content or "{}")
            assert result.provider_metadata == {TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY: source_id}
            assert payload["tool_call_id"] == call_id
            receipt_statuses[call_id] = payload["tool_execution_status"]
            inner_payload = json.loads(payload["content"] or "{}")
            assert isinstance(inner_payload, dict)
            receipt_contents[call_id] = inner_payload

            if startup_cleanup and call_id == queued_call.id:
                expected_status = "execution_unknown"
                expected_error = PROMPT_TOOL_INTERRUPTED
                if not task_started:
                    expected_status = "failed"
                    expected_error = t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION)
                assert payload["tool_cancelled"] is True
                assert payload["tool_execution_status"] == expected_status
                assert inner_payload == {
                    "type": "tool_cancellation",
                    "status": "cancelled",
                    "error": expected_error,
                }
            else:
                assert payload["tool_execution_status"] == "execution_unknown"
                assert payload.get("tool_cancelled") is not True
                assert inner_payload["status"] == "execution_unknown"
                assert inner_payload.get("type") != "tool_cancellation"
                assert isinstance(inner_payload.get("error"), str) and inner_payload["error"]

        event = events[0].event
        assert event["type"] == "tool_results_update"
        assert event["work_id"] == work_id
        event_messages = {json.loads(item["content"])["tool_call_id"]: item for item in event["messages"]}
        assert set(event_messages) == set(expected_receipt_call_ids)
        for call_id in expected_receipt_call_ids:
            assert event_messages[call_id]["id"] == tool_results[call_id].id
            assert event_messages[call_id]["content"] == tool_results[call_id].content

        async with session_factory() as db:
            profile = await db.get(Profile, 1)
            assert profile is not None
            model_history = await ContextManager.get_messages(
                db,
                session_id=session_id,
                uid="owner",
                profile=profile,
                current_message="current unprocessed user message",
                before_id=current_user_id,
                processed_only=True,
            )
            boundary = await resolve_context_summary_boundary(
                db,
                session_id=session_id,
                uid="owner",
                expected_summary_message_id=None,
                trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
                fixed_upper_message_id=current_user_id,
            )
            assert boundary.target_message_id == successful_result_id
            assert boundary.covered_user_message_id == previous_user_id

            snapshot = await build_context_summary_snapshot(
                db,
                session_id=session_id,
                uid="owner",
                expected_summary_message_id=None,
                before_id=None,
                target_message_id=boundary.target_message_id,
                late_cancelled_tool_results=boundary.late_cancelled_tool_results,
            )
            summary_rounds = [
                round_messages
                async for round_messages in iter_persistent_summary_rounds(
                    db,
                    session_id=session_id,
                    uid="owner",
                    snapshot=snapshot,
                )
            ]

        summary_messages = [message for round_messages in summary_rounds for message in round_messages]
        for history, is_summary in ((model_history, False), (summary_messages, True)):
            history_by_call = {message.tool_call_id: message for message in history if message.tool_call_id in expected_receipt_call_ids}
            assert set(history_by_call) == set(expected_receipt_call_ids)
            assert all(message.id != current_user_id for message in history)
            for call_id in expected_receipt_call_ids:
                stored_result = tool_results[call_id]
                history_result = history_by_call[call_id]
                if is_summary:
                    assert history_result.id == source_id
                else:
                    assert history_result.id == stored_result.id
                assert history_result.tool_execution_status == receipt_statuses[call_id]
                assert json.loads(history_result.content or "{}") == receipt_contents[call_id]

        await consumer_module.SessionReplyConsumer()._recover_expired()
        async with session_factory() as db:
            second_messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
            second_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all())
        assert [(message.id, message.type, message.content) for message in second_messages] == message_snapshot
        assert [(item.id, item.event) for item in second_events] == event_snapshot
