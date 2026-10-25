import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.providers.database as database_provider
from app.api.v1 import chat as chat_api
from app.core.background_tasks import runner as runner_module
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.security import get_current_user
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor_audit as executor_audit_module
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.handler import register_handlers
from app.models.audit import AuditConfirmationClaim, AuditRecord
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession
from app.providers.database import get_db
from tests.database_support import clone_sqlite_schema


@pytest_asyncio.fixture
async def db_session(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "chat-stop-workflow.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    await clone_sqlite_schema(
        database_path,
        tables=[
            PromptLibrary.__table__,
            Profile.__table__,
            ChatSession.__table__,
            Message.__table__,
            BackgroundTask.__table__,
            AuditRecord.__table__,
            AuditConfirmationClaim.__table__,
            SessionReplySequence.__table__,
            SessionReplyWorkItem.__table__,
            SessionReplyStreamEvent.__table__,
            TerminalSession.__table__,
            TerminalControlCommand.__table__,
        ],
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as setup_session:
        setup_session.add_all(
            [
                Profile(id=1, uid="owner", name="owner-profile", configs={}),
                Profile(id=2, uid="other", name="other-profile", configs={}),
            ]
        )
        await setup_session.commit()
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    auth_state: dict[str, object],
) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(chat_api.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as db:
            yield db

    def override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(**auth_state)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


def _patch_queue_runtime_database(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(database_provider, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_audit_module, "AsyncSessionLocal", session_factory)


def _make_work(
    *,
    session_id: str,
    uid: str,
    profile_id: int,
    sequence_no: int,
    work_type: SessionReplyWorkType,
    source_type: SessionReplySourceType,
    source_id: str,
    dedupe_key: str,
    status: SessionReplyWorkStatus,
    locked_by: str | None = None,
    lock_until: int | None = None,
    request_id: str | None = None,
    result_message_id: int | None = None,
    attempt_count: int = 0,
) -> SessionReplyWorkItem:
    return SessionReplyWorkItem(
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        sequence_no=sequence_no,
        work_type=work_type,
        source_type=source_type,
        source_id=source_id,
        dedupe_key=dedupe_key,
        status=status,
        result_message_id=result_message_id,
        execution_state={
            "message_source": "http",
            "request_ids": [request_id or dedupe_key],
        },
        locked_by=locked_by,
        lock_until=lock_until,
        attempt_count=attempt_count,
        max_attempts=3,
    )


async def _snapshot_state(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, list[tuple]]:
    async with session_factory() as db:
        sessions = list((await db.execute(select(ChatSession).order_by(ChatSession.session_id))).scalars().all())
        messages = list((await db.execute(select(Message).order_by(Message.id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
        sequences = list((await db.execute(select(SessionReplySequence).order_by(SessionReplySequence.session_id))).scalars().all())
        stream_events = list((await db.execute(select(SessionReplyStreamEvent).order_by(SessionReplyStreamEvent.id))).scalars().all())
    return {
        "sessions": [(item.session_id, item.uid, item.profile_id, item.source, item.reply_target_source) for item in sessions],
        "messages": [
            (
                item.id,
                item.session_id,
                item.uid,
                item.role.value,
                item.type.value,
                item.content,
                item.is_processed,
                item.profile_id,
            )
            for item in messages
        ],
        "works": [
            (
                item.id,
                item.uid,
                item.session_id,
                item.sequence_no,
                item.work_type.value,
                item.status.value,
                item.locked_by,
                item.lock_until,
                item.result_message_id,
                item.error,
                item.execution_state,
            )
            for item in works
        ],
        "sequences": [(item.session_id, item.next_sequence_no) for item in sequences],
        "stream_events": [(item.id, item.work_id, item.sequence_no, item.event) for item in stream_events],
    }


async def _snapshot_stop_entities(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    async with session_factory() as db:
        sessions = list((await db.execute(select(ChatSession).order_by(ChatSession.session_id))).scalars().all())
        messages = list((await db.execute(select(Message).order_by(Message.id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
        sequences = list((await db.execute(select(SessionReplySequence).order_by(SessionReplySequence.session_id))).scalars().all())
        stream_events = list((await db.execute(select(SessionReplyStreamEvent).order_by(SessionReplyStreamEvent.id))).scalars().all())
        background_tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
        terminal_sessions = list((await db.execute(select(TerminalSession).order_by(TerminalSession.terminal_session_id))).scalars().all())
        terminal_commands = list((await db.execute(select(TerminalControlCommand).order_by(TerminalControlCommand.id))).scalars().all())

    return {
        "sessions": {item.session_id: item.model_dump() for item in sessions},
        "messages": {item.id: dict(item) for item in messages if item.id is not None},
        "works": {item.id: item.model_dump() for item in works if item.id is not None},
        "sequences": {item.session_id: item.model_dump() for item in sequences},
        "stream_events": {item.id: item.model_dump() for item in stream_events if item.id is not None},
        "background_tasks": {item.id: item.model_dump() for item in background_tasks if item.id is not None},
        "terminal_sessions": {item.terminal_session_id: item.model_dump() for item in terminal_sessions},
        "terminal_commands": {item.id: item.model_dump() for item in terminal_commands if item.id is not None},
    }


async def _seed_stop_case(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    owner_session_id = "stop-owner-session"
    other_session_id = "stop-other-session"
    background_session_id = "stop-background-session"
    scheduled_session_id = "stop-scheduled-session"
    async with session_factory() as db:
        db.add_all(
            [
                ChatSession(session_id=owner_session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"),
                ChatSession(session_id=other_session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"),
                ChatSession(session_id=background_session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"),
                ChatSession(session_id=scheduled_session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"),
            ]
        )
        await db.flush()
        history = Message(
            id=100,
            session_id=owner_session_id,
            uid="owner",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="historical reply",
            is_processed=True,
        )
        running = _make_work(
            session_id=owner_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="running-source",
            dedupe_key="stop-owner-running",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="worker-running",
            lock_until=2_000_000_000,
            request_id="running-request",
            attempt_count=1,
        )
        ready = _make_work(
            session_id=owner_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=2,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="ready-source",
            dedupe_key="stop-owner-ready",
            status=SessionReplyWorkStatus.READY_FOR_LLM,
            locked_by="worker-ready",
            lock_until=2_000_000_000,
            request_id="ready-request",
        )
        confirmed = _make_work(
            session_id=owner_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=3,
            work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            source_type=SessionReplySourceType.AUDIT_RECORD,
            source_id="audit-1",
            dedupe_key="stop-owner-confirmed",
            status=SessionReplyWorkStatus.READY_FOR_LLM,
            locked_by="worker-confirmed",
            lock_until=2_000_000_000,
            request_id="confirmed-request",
        )
        succeeded = _make_work(
            session_id=owner_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=4,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="succeeded-source",
            dedupe_key="stop-owner-succeeded",
            status=SessionReplyWorkStatus.SUCCEEDED,
            request_id="succeeded-request",
            result_message_id=history.id,
        )
        other = _make_work(
            session_id=other_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="other-source",
            dedupe_key="stop-other-foreground",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="other-worker",
            lock_until=2_000_000_000,
            request_id="other-request",
            attempt_count=1,
        )
        background = _make_work(
            session_id=background_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id="background-1",
            dedupe_key="stop-background",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="background-worker",
            lock_until=2_000_000_000,
            request_id="background-request",
            attempt_count=1,
        )
        scheduled = _make_work(
            session_id=scheduled_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id="scheduled-1",
            dedupe_key="stop-scheduled",
            status=SessionReplyWorkStatus.READY_FOR_LLM,
            locked_by="scheduled-worker",
            lock_until=2_000_000_000,
            request_id="scheduled-request",
        )
        db.add_all(
            [
                history,
                running,
                ready,
                confirmed,
                succeeded,
                other,
                background,
                scheduled,
                SessionReplySequence(session_id=owner_session_id, next_sequence_no=5),
            ]
        )
        await db.commit()
        return {
            "owner_session_id": owner_session_id,
            "interactive_ids": [running.id, ready.id, confirmed.id],
            "succeeded_id": succeeded.id,
            "other_id": other.id,
            "background_id": background.id,
            "scheduled_id": scheduled.id,
            "history_id": history.id,
        }


async def _seed_full_stop_case(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    target_session_id = "stop-regression-target"
    other_session_id = "stop-regression-other-session"
    other_user_session_id = "stop-regression-other-user"
    terminal_session_id = "stop-regression-terminal-0123456789abcdef"
    lease_until = 2_000_000_000

    async with session_factory() as db:
        target_history_user = Message(
            session_id=target_session_id,
            uid="owner",
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="historical request",
            is_processed=True,
        )
        target_history_assistant = Message(
            session_id=target_session_id,
            uid="owner",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="historical response",
            is_processed=True,
        )
        terminal = TerminalSession(
            terminal_session_id=terminal_session_id,
            uid="owner",
            session_id=target_session_id,
            original_tool_call_id="stop-regression-terminal-call",
            profile_id=1,
            command="printf terminal-output",
            working_directory="/tmp/stop-regression",
            status=TerminalSessionStatus.RUNNING,
            allowed_actions=[action.value for action in TerminalAction],
            process_identity={"pid": 701, "start_time": "stop-regression"},
            output_capacity_bytes=4096,
            oldest_output_offset=11,
            next_output_offset=42,
            oldest_output_sequence=3,
            next_output_sequence=7,
            locked_by="terminal-worker",
            lock_until=lease_until,
        )
        existing_command = TerminalControlCommand(
            terminal_session_id=terminal_session_id,
            request_id="stop-regression-existing-request",
            action=TerminalAction.WRITE,
            payload={"data": "prior command"},
            payload_hash="stop-regression-existing-hash",
            status=TerminalControlCommandStatus.SUCCEEDED,
            result={"bytes_written": 13, "read_offset": 11},
        )
        db.add_all(
            [
                ChatSession(session_id=target_session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"),
                ChatSession(session_id=other_session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"),
                ChatSession(session_id=other_user_session_id, uid="other", profile_id=2, source="http", reply_target_source="http"),
            ]
        )
        await db.flush()
        db.add_all([target_history_user, target_history_assistant, terminal])
        await db.flush()
        db.add(existing_command)
        await db.commit()

        await ensure_terminal_auto_watch(
            db,
            uid="owner",
            session_id=target_session_id,
            profile_id=1,
            terminal_session_id=terminal_session_id,
            after_offset=42,
            session_status=TerminalSessionStatus.RUNNING,
        )
        auto_watch = await background_task_crud.get_active_terminal_watch(
            db,
            "owner",
            target_session_id,
            terminal_session_id,
            automatic_only=True,
        )
        assert auto_watch is not None
        assert auto_watch.id is not None

        manual_watch = BackgroundTask(
            uid="owner",
            session_id=target_session_id,
            profile_id=1,
            tool_call_id="stop-regression-manual-watch",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            status=BackgroundTaskStatus.RUNNING,
            arguments={
                "terminal_session_id": terminal_session_id,
                "after_offset": 17,
                "condition": "output_contains",
                "text": "ready",
            },
            auto_reply=False,
            reply_status=BackgroundTaskReplyStatus.NONE,
            locked_by="manual-watch-worker",
            lock_until=lease_until,
            attempt_count=1,
            extra={"source": "model", "terminal_session_id": terminal_session_id},
        )
        ordinary_task = BackgroundTask(
            uid="owner",
            session_id=target_session_id,
            profile_id=1,
            tool_call_id="stop-regression-ordinary-task",
            tool_name="ordinary_background_tool",
            status=BackgroundTaskStatus.RUNNING,
            arguments={"value": "ordinary"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            locked_by="ordinary-worker",
            lock_until=lease_until,
            attempt_count=1,
            extra={"execution_state": "running"},
        )
        completed_success = BackgroundTask(
            uid="owner",
            session_id=target_session_id,
            profile_id=1,
            tool_call_id="stop-regression-completed-success",
            tool_name="completed_background_tool",
            status=BackgroundTaskStatus.SUCCEEDED,
            arguments={"value": "success"},
            result={"content": "completed success", "value": 1},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            attempt_count=1,
            extra={"execution_state": "succeeded"},
        )
        completed_failed = BackgroundTask(
            uid="owner",
            session_id=target_session_id,
            profile_id=1,
            tool_call_id="stop-regression-completed-failed",
            tool_name="completed_background_tool",
            status=BackgroundTaskStatus.FAILED,
            arguments={"value": "failed"},
            result={"content": "completed failure", "value": 2},
            error="background failure",
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.RUNNING,
            reply_locked_by="completed-reply-worker",
            reply_lock_until=lease_until,
            attempt_count=2,
            extra={"execution_state": "failed"},
        )
        other_session_task = BackgroundTask(
            uid="owner",
            session_id=other_session_id,
            profile_id=1,
            tool_call_id="stop-regression-other-session-task",
            tool_name="other_background_tool",
            status=BackgroundTaskStatus.RUNNING,
            arguments={"value": "other session"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            locked_by="other-session-worker",
            lock_until=lease_until,
        )
        other_user_task = BackgroundTask(
            uid="other",
            session_id=other_user_session_id,
            profile_id=2,
            tool_call_id="stop-regression-other-user-task",
            tool_name="other_user_background_tool",
            status=BackgroundTaskStatus.PENDING,
            arguments={"value": "other user"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
        )
        db.add_all(
            [
                manual_watch,
                ordinary_task,
                completed_success,
                completed_failed,
                other_session_task,
                other_user_task,
            ]
        )
        await db.flush()

        waiting_work = _make_work(
            session_id=target_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=str(target_history_user.id),
            dedupe_key="stop-regression-waiting-work",
            status=SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            request_id="stop-regression-waiting-request",
            attempt_count=1,
        )
        waiting_work.execution_state = {
            "external_resume_pending": True,
            "request_ids": ["stop-regression-waiting-request"],
        }
        running_work = _make_work(
            session_id=target_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=2,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=str(target_history_user.id),
            dedupe_key="stop-regression-running-work",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="foreground-worker",
            lock_until=lease_until,
            request_id="stop-regression-running-request",
            attempt_count=1,
        )
        background_summary_work = _make_work(
            session_id=target_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=3,
            work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id=str(ordinary_task.id),
            dedupe_key="stop-regression-background-summary",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="background-summary-worker",
            lock_until=lease_until,
            request_id="stop-regression-background-summary-request",
            attempt_count=1,
        )
        other_session_work = _make_work(
            session_id=other_session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id=str(other_session_task.id),
            dedupe_key="stop-regression-other-session-work",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="other-session-reply-worker",
            lock_until=lease_until,
            request_id="stop-regression-other-session-request",
            attempt_count=1,
        )
        other_user_work = _make_work(
            session_id=other_user_session_id,
            uid="other",
            profile_id=2,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="stop-regression-other-user-source",
            dedupe_key="stop-regression-other-user-work",
            status=SessionReplyWorkStatus.READY_FOR_LLM,
            request_id="stop-regression-other-user-request",
        )
        db.add_all(
            [
                waiting_work,
                running_work,
                background_summary_work,
                other_session_work,
                other_user_work,
                SessionReplySequence(session_id=target_session_id, next_sequence_no=4),
                SessionReplySequence(session_id=other_session_id, next_sequence_no=2),
                SessionReplySequence(session_id=other_user_session_id, next_sequence_no=2),
            ]
        )
        await db.commit()

        return {
            "target_session_id": target_session_id,
            "other_session_id": other_session_id,
            "other_user_session_id": other_user_session_id,
            "terminal_session_id": terminal_session_id,
            "target_active_work_ids": [waiting_work.id, running_work.id, background_summary_work.id],
            "late_work_id": running_work.id,
            "active_task_ids": [auto_watch.id, manual_watch.id, ordinary_task.id],
            "target_completed_task_ids": [completed_success.id, completed_failed.id],
            "isolated_work_ids": [other_session_work.id, other_user_work.id],
            "isolated_task_ids": [other_session_task.id, other_user_task.id],
            "late_task_id": ordinary_task.id,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "is_superuser"),
    [("owner", False), ("other", True)],
    ids=["owner", "superuser"],
)
async def test_owner_stop_cancels_target_session_work_and_allows_new_claim(
    db_session,
    uid: str,
    is_superuser: bool,
) -> None:
    seeded = await _seed_stop_case(db_session)
    app = _build_app(db_session, {"uid": uid, "is_superuser": is_superuser})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": seeded["owner_session_id"]},
        )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"] == {
            "session_id": seeded["owner_session_id"],
            "cancelled_count": 3,
        }

        repeated = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": seeded["owner_session_id"]},
        )
        assert repeated.status_code == 200
        assert repeated.json()["code"] == 200
        assert repeated.json()["data"]["cancelled_count"] == 0

    async with db_session() as db:
        works = {work.id: work for work in (await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all()}
        owner_session = await db.get(ChatSession, seeded["owner_session_id"])
        history = await db.get(Message, seeded["history_id"])
        messages = list((await db.execute(select(Message).where(Message.session_id == seeded["owner_session_id"]).order_by(Message.id))).scalars().all())
    assert all(works[work_id].status == SessionReplyWorkStatus.CANCELLED for work_id in seeded["interactive_ids"])
    assert all(works[work_id].uid == "owner" for work_id in seeded["interactive_ids"])
    assert all(works[work_id].locked_by is None and works[work_id].lock_until is None for work_id in seeded["interactive_ids"])
    assert works[seeded["succeeded_id"]].status == SessionReplyWorkStatus.SUCCEEDED
    assert works[seeded["succeeded_id"]].result_message_id == seeded["history_id"]
    assert works[seeded["other_id"]].status == SessionReplyWorkStatus.RUNNING
    assert works[seeded["other_id"]].locked_by == "other-worker"
    assert works[seeded["background_id"]].status == SessionReplyWorkStatus.RUNNING
    assert works[seeded["background_id"]].locked_by == "background-worker"
    assert works[seeded["scheduled_id"]].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert works[seeded["scheduled_id"]].locked_by == "scheduled-worker"
    assert owner_session is not None
    assert owner_session.uid == "owner"
    assert history is not None
    assert history.uid == "owner"
    assert history.content == "historical reply"
    assert all(message.uid == "owner" for message in messages)

    async with db_session() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, new_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id=seeded["owner_session_id"],
            profile=profile,
            message="after stop",
            attachments=None,
            source="http",
            request_id="after-stop-request",
        )
        assert submission_status == "accepted"
        scheduled_claim = await session_reply_work_item_crud.claim_next(
            db,
            worker_id="new-worker",
            lease_seconds=60,
        )
        assert scheduled_claim is not None
        assert scheduled_claim.id == seeded["scheduled_id"]
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id="new-worker",
            lease_seconds=60,
        )
    assert claimed is not None
    assert claimed.id == new_work.id
    assert claimed.status == SessionReplyWorkStatus.RUNNING
    assert claimed.locked_by == "new-worker"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "is_superuser"),
    [("owner", False), ("admin", True)],
    ids=["owner", "admin"],
)
async def test_stop_cancels_all_target_async_activity_and_preserves_terminal(
    db_session,
    uid: str,
    is_superuser: bool,
) -> None:
    seeded = await _seed_full_stop_case(db_session)
    before = await _snapshot_stop_entities(db_session)
    app = _build_app(db_session, {"uid": uid, "is_superuser": is_superuser})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": seeded["target_session_id"]},
        )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"] == {
            "session_id": seeded["target_session_id"],
            "cancelled_count": len(seeded["target_active_work_ids"]),
        }

        repeated = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": seeded["target_session_id"]},
        )
        assert repeated.status_code == 200
        assert repeated.json()["code"] == 200
        assert repeated.json()["data"] == {
            "session_id": seeded["target_session_id"],
            "cancelled_count": 0,
        }

    after_stop = await _snapshot_stop_entities(db_session)
    for work_id in seeded["target_active_work_ids"]:
        work = after_stop["works"][work_id]
        assert work["status"] == SessionReplyWorkStatus.CANCELLED
        assert work["locked_by"] is None
        assert work["lock_until"] is None

    for task_id in seeded["active_task_ids"]:
        task = after_stop["background_tasks"][task_id]
        assert task["status"] == BackgroundTaskStatus.CANCELLED
        assert task["auto_reply"] is False
        assert task["reply_status"] == BackgroundTaskReplyStatus.NONE
        assert task["locked_by"] is None
        assert task["lock_until"] is None
        assert task["reply_locked_by"] is None
        assert task["reply_lock_until"] is None

    for task_id in seeded["target_completed_task_ids"]:
        before_task = before["background_tasks"][task_id]
        after_task = after_stop["background_tasks"][task_id]
        assert before_task["status"] in {BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED}
        assert after_task["status"] == before_task["status"]
        assert after_task["result"] == before_task["result"]
        assert after_task["error"] == before_task["error"]
        assert after_task["extra"] == before_task["extra"]
        assert after_task["attempt_count"] == before_task["attempt_count"]
        assert after_task["started_at"] == before_task["started_at"]
        assert after_task["finished_at"] == before_task["finished_at"]
        assert after_task["auto_reply"] is False
        assert after_task["reply_status"] == BackgroundTaskReplyStatus.NONE
        assert after_task["reply_locked_by"] is None
        assert after_task["reply_lock_until"] is None

    for work_id in seeded["isolated_work_ids"]:
        assert after_stop["works"][work_id] == before["works"][work_id]
    for task_id in seeded["isolated_task_ids"]:
        assert after_stop["background_tasks"][task_id] == before["background_tasks"][task_id]

    assert after_stop["sessions"] == before["sessions"]
    assert after_stop["messages"] == before["messages"]
    assert after_stop["sequences"] == before["sequences"]
    assert after_stop["stream_events"] == before["stream_events"]
    assert after_stop["terminal_sessions"][seeded["terminal_session_id"]] == before["terminal_sessions"][seeded["terminal_session_id"]]
    assert after_stop["terminal_commands"] == before["terminal_commands"]
    assert all(command["action"] != TerminalAction.CLOSE for command in after_stop["terminal_commands"].values())

    async with db_session() as db:
        late_marked = await background_task_crud.mark_succeeded(
            db,
            task_id=seeded["late_task_id"],
            worker_id="ordinary-worker",
            result={"content": "late success must be rejected"},
            auto_reply=True,
        )
    assert late_marked is False
    after_late_mark = await _snapshot_stop_entities(db_session)
    assert after_late_mark == after_stop

    async with db_session() as db:
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (seeded["late_work_id"], "foreground-worker")
        with pytest.raises(asyncio.CancelledError):
            await ensure_terminal_auto_watch(
                db,
                uid="owner",
                session_id=seeded["target_session_id"],
                profile_id=1,
                terminal_session_id=seeded["terminal_session_id"],
                after_offset=42,
                session_status=TerminalSessionStatus.RUNNING,
            )

    after_guard = await _snapshot_stop_entities(db_session)
    assert after_guard == after_late_mark


async def _seed_invalid_stop_case(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as db:
        db.add_all(
            [
                ChatSession(session_id="invalid-owner-session", uid="owner", profile_id=1, source="http", reply_target_source="http"),
                ChatSession(session_id="invalid-external-session", uid="owner", profile_id=1, source="external", reply_target_source="external"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                _make_work(
                    session_id="invalid-owner-session",
                    uid="owner",
                    profile_id=1,
                    sequence_no=1,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="invalid-owner-source",
                    dedupe_key="invalid-owner-work",
                    status=SessionReplyWorkStatus.RUNNING,
                    locked_by="invalid-owner-worker",
                    lock_until=2_000_000_000,
                    attempt_count=1,
                ),
                _make_work(
                    session_id="invalid-external-session",
                    uid="owner",
                    profile_id=1,
                    sequence_no=1,
                    work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                    source_type=SessionReplySourceType.AUDIT_RECORD,
                    source_id="invalid-external-audit",
                    dedupe_key="invalid-external-work",
                    status=SessionReplyWorkStatus.READY_FOR_LLM,
                    locked_by="invalid-external-worker",
                    lock_until=2_000_000_000,
                ),
                SessionReplySequence(session_id="invalid-owner-session", next_sequence_no=2),
                SessionReplySequence(session_id="invalid-external-session", next_sequence_no=2),
            ]
        )
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "uid", "is_superuser", "expected_code"),
    [
        ("missing-session", "owner", False, 404),
        ("invalid-owner-session", "other", False, 403),
        ("invalid-external-session", "owner", False, 403),
        ("invalid-external-session", "other", True, 403),
        ("", "owner", False, 422),
    ],
    ids=["not-found", "other-user", "external-session", "superuser-external-session", "empty-id"],
)
async def test_stop_rejects_invalid_target_without_mutating_state(
    db_session,
    session_id: str,
    uid: str,
    is_superuser: bool,
    expected_code: int,
) -> None:
    await _seed_invalid_stop_case(db_session)
    before = await _snapshot_state(db_session)
    app = _build_app(db_session, {"uid": uid, "is_superuser": is_superuser})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )

    assert response.status_code == (422 if expected_code == 422 else 200)
    assert response.json()["code"] == expected_code
    after = await _snapshot_state(db_session)
    assert after == before


async def _seed_reply_work(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    status: SessionReplyWorkStatus,
    worker_id: str | None,
    stream_requested: bool = False,
) -> tuple[int, int, str]:
    async with session_factory() as db:
        db.add(ChatSession(session_id=session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"))
        await db.flush()
        user_message = Message(
            session_id=session_id,
            uid="owner",
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="hold this reply",
            is_processed=False,
        )
        db.add(user_message)
        await db.flush()
        work = _make_work(
            session_id=session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=str(user_message.id),
            dedupe_key=f"{session_id}-work",
            status=status,
            locked_by=worker_id,
            lock_until=2_000_000_000 if worker_id else None,
            request_id=f"{session_id}-request",
            attempt_count=1 if worker_id else 0,
        )
        work.execution_state["stream_requested"] = stream_requested
        db.add_all([work, SessionReplySequence(session_id=session_id, next_sequence_no=2)])
        await db.commit()
        assert user_message.id is not None
        assert work.id is not None
        return work.id, user_message.id, session_id


@pytest.mark.asyncio
async def test_executor_rejects_late_assistant_save_after_http_stop(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id="executor-stop-session",
        status=SessionReplyWorkStatus.RUNNING,
        worker_id="executor-worker",
    )
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    cancellation_seen = asyncio.Event()
    late_save_attempted = asyncio.Event()
    dispatch_calls = 0

    async def controlled_dispatch(**kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        dispatch_started.set()
        try:
            await release_dispatch.wait()
            late_save_attempted.set()
            await save_assistant_message(
                kwargs["db"],
                kwargs["session_id"],
                kwargs["uid"],
                kwargs["persisted_profile_id"],
                InternalMessage(role=MessageRole.ASSISTANT, content="late assistant"),
                dedupe_key=kwargs["final_message_dedupe_key"],
            )
        except asyncio.CancelledError:
            cancellation_seen.set()
            raise
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "late assistant"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    monkeypatch.setattr(ChatDispatcher, "dispatch", controlled_dispatch)
    execution_task = asyncio.create_task(executor_lifecycle_module.execute_session_reply_work(work_id, "executor-worker"))
    try:
        await asyncio.wait_for(dispatch_started.wait(), timeout=2)
        app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stopped = await client.post(
                "/api/v1/chat/sessions/stop",
                params={"session_id": session_id},
            )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"]["cancelled_count"] == 1
        release_dispatch.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution_task, timeout=2)
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)

    assert late_save_attempted.is_set()
    assert cancellation_seen.is_set()
    assert dispatch_calls == 1
    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
    assert work is not None
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None
    assert all(message.role != MessageRole.ASSISTANT for message in messages)


@pytest.mark.asyncio
async def test_cancelled_stream_is_terminal_and_late_content_is_not_persisted(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id="stream-stop-session",
        status=SessionReplyWorkStatus.CANCELLED,
        worker_id=None,
        stream_requested=True,
    )

    events = [event async for event in session_reply_queue_manager.wait_for_stream(work_id)]
    assert [event["type"] for event in events] == ["cancelled"]
    assert events[0]["session_id"] == session_id
    assert events[0]["work_id"] == work_id
    assert not {event["type"] for event in events} & {"error", "done"}

    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
    assert work is not None
    stream_state = executor_interactive_module._InteractiveWorkStreamEventState(
        work=work,
        worker_id="stream-worker",
        next_sequence=1,
        dequeued_request_ids=set(),
        turn_end_content_by_response_id={},
        tool_names_by_response_id={},
    )
    with pytest.raises(asyncio.CancelledError):
        await executor_interactive_module._persist_interactive_work_stream_event(
            stream_state,
            {"type": "content", "content": "late content", "response_id": "late-response"},
        )

    async with db_session() as db:
        persisted_events = list((await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == work_id))).scalars().all())
    assert persisted_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_requested", [False, True])
async def test_consumer_cancels_http_stopped_claim_without_retry(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    stream_requested: bool,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id="consumer-stop-session",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        worker_id=None,
        stream_requested=stream_requested,
    )
    dispatch_started = asyncio.Event()
    dispatch_cancelled = asyncio.Event()
    dispatch_finished = asyncio.Event()
    release_dispatch = asyncio.Event()
    dispatch_calls = 0

    async def runtime_settings(_db):
        return SimpleNamespace(session_reply_max_concurrency=1)

    async def controlled_dispatch(**_kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        dispatch_started.set()
        try:
            await release_dispatch.wait()
        except asyncio.CancelledError:
            dispatch_cancelled.set()
            raise
        finally:
            dispatch_finished.set()
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "should not persist"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    monkeypatch.setattr(consumer_module.system_setting_crud, "get_runtime_settings", runtime_settings)
    if stream_requested:
        import app.core.dispatchers.stream as stream_module

        monkeypatch.setattr(stream_module, "dispatch_interactive", controlled_dispatch)
    else:
        monkeypatch.setattr(ChatDispatcher, "dispatch", controlled_dispatch)
    consumer = consumer_module.SessionReplyConsumer()
    consumer_task = consumer.start()
    try:
        await asyncio.wait_for(dispatch_started.wait(), timeout=2)
        app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stopped = await client.post(
                "/api/v1/chat/sessions/stop",
                params={"session_id": session_id},
            )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"]["cancelled_count"] == 1
        await asyncio.wait_for(dispatch_cancelled.wait(), timeout=2)
    finally:
        await consumer.stop()
        if not consumer_task.done():
            consumer_task.cancel()
        await asyncio.gather(consumer_task, return_exceptions=True)

    assert dispatch_finished.is_set()
    assert dispatch_calls == 1
    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        all_works = list((await db.execute(select(SessionReplyWorkItem))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        stream_events = list((await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == work_id))).scalars().all())
    assert work is not None
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None
    assert work.attempt_count == 1
    assert len(all_works) == 1
    assert all(message.role != MessageRole.ASSISTANT for message in messages)
    event_types = [stream_event.event["type"] for stream_event in stream_events]
    if stream_requested:
        assert set(event_types) <= {"task_start"}
    else:
        assert stream_events == []
    assert not set(event_types) & {"done", "error", "content"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [TERMINAL_WATCH_TOOL_NAME, "ordinary_background_tool"],
    ids=["terminal-watch", "ordinary-background"],
)
async def test_http_stop_cancels_running_background_runner(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    _work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id=f"background-runner-stop-{tool_name}",
        status=SessionReplyWorkStatus.RUNNING,
        worker_id="reply-worker",
    )
    arguments = (
        {
            "terminal_session_id": "background-runner-stop-terminal",
            "after_offset": 0,
            "condition": "process_exit",
        }
        if tool_name == TERMINAL_WATCH_TOOL_NAME
        else {"value": "blocking"}
    )
    async with db_session() as db:
        task = await background_task_crud.create_task(
            db,
            uid="owner",
            session_id=session_id,
            profile_id=1,
            tool_call_id=f"{session_id}-tool-call",
            tool_name=tool_name,
            arguments=arguments,
            auto_reply=True,
            extra={"source": "test"},
        )
    assert task.id is not None
    assert task.status == BackgroundTaskStatus.PENDING
    assert task.reply_status == BackgroundTaskReplyStatus.PENDING
    task_id = task.id

    execution_started = asyncio.Event()
    execution_cancelled = asyncio.Event()
    release_execution = asyncio.Event()

    class BlockingExecutor:
        requires_audit = False

        def __init__(self, project_root: str, uid: str = "default") -> None:
            self.project_root = project_root
            self.uid = uid

        def set_config(self, _cfg) -> None:
            pass

        def set_runtime_context(self, **_kwargs) -> None:
            pass

        async def execute(self, **_kwargs):
            execution_started.set()
            try:
                await release_execution.wait()
            except asyncio.CancelledError:
                execution_cancelled.set()
                raise
            return {"content": "unexpected completion"}

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", db_session)
    monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, tool_name, BlockingExecutor)
    runner_task = asyncio.create_task(runner_module.run_background_task(task_id, worker_id="background-worker"))
    try:
        await asyncio.wait_for(execution_started.wait(), timeout=5)
        app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stopped = await client.post(
                "/api/v1/chat/sessions/stop",
                params={"session_id": session_id},
            )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"] == {
            "session_id": session_id,
            "cancelled_count": 1,
        }
        await asyncio.wait_for(execution_cancelled.wait(), timeout=5)
        await asyncio.wait_for(runner_task, timeout=5)
    finally:
        if not runner_task.done():
            runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)

    async with db_session() as db:
        cancelled_task = await db.get(BackgroundTask, task_id)
        late_claim = await background_task_crud.try_claim(
            db,
            task_id=task_id,
            worker_id="late-worker",
        )
    assert late_claim is None
    assert cancelled_task is not None
    assert cancelled_task.status == BackgroundTaskStatus.CANCELLED
    assert cancelled_task.auto_reply is False
    assert cancelled_task.reply_status == BackgroundTaskReplyStatus.NONE
    assert cancelled_task.locked_by is None
    assert cancelled_task.lock_until is None
    assert cancelled_task.reply_locked_by is None
    assert cancelled_task.reply_lock_until is None
    assert cancelled_task.result is None

    async with db_session() as db:
        tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
    assert len(tasks) == 1
    assert tasks[0].id == task_id
    assert tasks[0].result is None
