import os
import platform
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import psutil
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.core.terminal.audit_lifecycle as terminal_audit_lifecycle_module
from app.core.constants import ERR_SESSION_DELETE_FAILED, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.exceptions import ServerException
from app.core.session.cleanup import delete_session_data
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.models.audit import AuditConfirmationClaim, AuditRecord, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryMutationOperation, LongTermMemoryMutationStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.scheduled_task import ScheduledTask
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
from app.models.session_todo import SessionTodoPlan
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncGenerator[AsyncSession]:
    async with database_factory(
        tables=[
            ChatSession.__table__,
            SessionTodoPlan.__table__,
            AuditRecord.__table__,
            AuditConfirmationClaim.__table__,
            Message.__table__,
            SessionReplySequence.__table__,
            SessionReplyWorkItem.__table__,
            SessionReplyStreamEvent.__table__,
            SessionEvent.__table__,
            MessagePlatformOutbox.__table__,
            BackgroundTask.__table__,
            LongTermMemoryMutationJob.__table__,
            ScheduledTask.__table__,
            TerminalSession.__table__,
            TerminalControlCommand.__table__,
        ]
    ) as factory:
        async with factory() as session:
            yield session


async def _seed_session_data(db: AsyncSession) -> tuple[int, int, int]:
    db.add(
        ChatSession(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
            source="weixin-openclaw",
            reply_target_source="weixin-openclaw",
        )
    )
    db.add(
        SessionTodoPlan(
            session_id="session-1",
            uid="user-1",
            todos=[{"content": "pending todo", "status": "pending"}],
            revision=1,
        )
    )
    audit_record = AuditRecord(
        uid="user-1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="zh",
        status=AuditRecordStatus.PENDING,
        source_assistant_message_id=1,
        working_directory="/workspace",
        round_arguments_hash="a" * 64,
        tool_count=1,
    )
    db.add(audit_record)
    await db.flush()
    db.add(AuditConfirmationClaim(uid="user-1", session_id="session-1", audit_record_id=audit_record.id))
    message = Message(
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="hello",
        is_processed=False,
    )
    db.add(message)
    await db.flush()

    work, _created = await session_reply_work_item_crud.enqueue(
        db,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=message.id,
        dedupe_key="foreground-message:session-1:1",
        commit=False,
    )
    work.status = SessionReplyWorkStatus.SUCCEEDED
    db.add(
        SessionReplyStreamEvent(
            work_id=work.id,
            sequence_no=1,
            event={"type": "content", "content": "partial"},
        )
    )
    db.add(
        SessionEvent(
            dedupe_key="session-event-1",
            uid="user-1",
            session_id="session-1",
            event={"type": "proactive_reply"},
        )
    )
    db.add(
        MessagePlatformOutbox(
            dedupe_key="outbox-1",
            uid="user-1",
            session_id="session-1",
            source="weixin-openclaw",
            event={"type": "proactive_reply"},
        )
    )
    completed_task = BackgroundTask(
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        tool_call_id="completed-call",
        tool_name="shell",
        status=BackgroundTaskStatus.SUCCEEDED,
        arguments={},
        auto_reply=True,
        reply_status=BackgroundTaskReplyStatus.PENDING,
    )
    pending_task = BackgroundTask(
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        tool_call_id="pending-call",
        tool_name="shell",
        status=BackgroundTaskStatus.PENDING,
        arguments={},
        auto_reply=True,
        reply_status=BackgroundTaskReplyStatus.PENDING,
    )
    running_task = BackgroundTask(
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        tool_call_id="running-call",
        tool_name="shell",
        status=BackgroundTaskStatus.RUNNING,
        arguments={},
        auto_reply=True,
        reply_status=BackgroundTaskReplyStatus.RUNNING,
        locked_by="task-worker",
        lock_until=9999999999,
        reply_locked_by="reply-worker",
        reply_lock_until=9999999999,
    )
    db.add(completed_task)
    db.add(pending_task)
    db.add(running_task)
    db.add(
        ScheduledTask(
            name="scheduled",
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            message="run",
            interval_seconds=60,
        )
    )
    await db.commit()
    return completed_task.id, pending_task.id, running_task.id


@pytest.mark.asyncio
async def test_delete_session_data_removes_empty_session_for_owner(db_session: AsyncSession):
    db_session.add(ChatSession(session_id="empty-session", uid="user-1", profile_id=1))
    await db_session.commit()

    deleted = await delete_session_data(
        db_session,
        session_id="empty-session",
        uid="user-1",
        is_admin=False,
    )
    await db_session.commit()

    assert deleted is True
    assert await db_session.get(ChatSession, "empty-session") is None


@pytest.mark.asyncio
async def test_delete_session_data_rejects_non_owner_without_changes(db_session: AsyncSession):
    await _seed_session_data(db_session)
    terminal_session = TerminalSession(
        terminal_session_id="terminal-session-1",
        uid="user-1",
        session_id="session-1",
        original_tool_call_id="terminal-call-1",
        profile_id=1,
        command="python -i",
        working_directory="/workspace",
        status=TerminalSessionStatus.RUNNING,
        allowed_actions=[TerminalAction.CLOSE.value],
        process_identity={
            "platform": platform.system(),
            "boot_time": psutil.boot_time(),
            "root_pid": os.getpid() + 100_000,
            "root_create_time": 10.0,
            "process_group_id": None,
            "known_processes": {str(os.getpid() + 100_000): 10.0},
        },
    )
    db_session.add(terminal_session)
    await db_session.commit()

    deleted = await delete_session_data(
        db_session,
        session_id="session-1",
        uid="user-2",
        is_admin=False,
    )

    assert deleted is False
    assert await db_session.get(ChatSession, "session-1") is not None
    assert await db_session.get(SessionTodoPlan, "session-1") is not None
    assert await db_session.get(TerminalSession, terminal_session.terminal_session_id) is not None
    assert list((await db_session.execute(select(Message))).scalars().all())


@pytest.mark.asyncio
async def test_delete_session_data_removes_all_associations_and_cancels_running_task(db_session: AsyncSession):
    completed_task_id, pending_task_id, running_task_id = await _seed_session_data(db_session)
    root_pid = os.getpid() + 100_000
    target_terminal = TerminalSession(
        terminal_session_id="terminal-session-1",
        uid="user-1",
        session_id="session-1",
        original_tool_call_id="terminal-call-1",
        profile_id=1,
        command="python -i",
        working_directory="/workspace",
        status=TerminalSessionStatus.RUNNING,
        allowed_actions=[TerminalAction.CLOSE.value],
        process_identity={
            "platform": platform.system(),
            "boot_time": psutil.boot_time(),
            "root_pid": root_pid,
            "root_create_time": 10.0,
            "process_group_id": None,
            "known_processes": {str(root_pid): 10.0},
        },
    )
    target_command = TerminalControlCommand(
        terminal_session_id=target_terminal.terminal_session_id,
        request_id="request-1",
        action=TerminalAction.CLOSE,
        payload={"force": True},
        payload_hash="a" * 64,
        status=TerminalControlCommandStatus.PENDING,
    )
    other_terminal = TerminalSession(
        terminal_session_id="terminal-session-2",
        uid="user-2",
        session_id="session-2",
        original_tool_call_id="terminal-call-2",
        profile_id=1,
        command="python -i",
        working_directory="/workspace",
        status=TerminalSessionStatus.RUNNING,
        allowed_actions=[TerminalAction.CLOSE.value],
        process_identity=None,
    )
    other_command = TerminalControlCommand(
        terminal_session_id=other_terminal.terminal_session_id,
        request_id="request-2",
        action=TerminalAction.CLOSE,
        payload={"force": True},
        payload_hash="b" * 64,
        status=TerminalControlCommandStatus.PENDING,
    )
    db_session.add_all([target_terminal, target_command, other_terminal, other_command])
    memory_job = LongTermMemoryMutationJob(
        uid="user-1",
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="memory-create:session-1:first",
        active_mutation_key="user-1:memory:first",
        payload={"content": "session memory"},
        source_session_id="session-1",
    )
    db_session.add(memory_job)
    await db_session.commit()
    assert target_command.id is not None
    assert other_command.id is not None
    assert memory_job.id is not None

    deleted = await delete_session_data(
        db_session,
        session_id="session-1",
        uid="user-1",
        is_admin=False,
    )
    await db_session.commit()
    db_session.expunge_all()

    assert deleted is True
    assert await db_session.get(ChatSession, "session-1") is None
    assert await db_session.get(SessionTodoPlan, "session-1") is None
    assert await db_session.get(TerminalSession, target_terminal.terminal_session_id) is None
    assert await db_session.get(TerminalControlCommand, target_command.id) is None
    assert await db_session.get(TerminalSession, other_terminal.terminal_session_id) is not None
    assert await db_session.get(TerminalControlCommand, other_command.id) is not None
    for model in (
        Message,
        SessionReplySequence,
        SessionReplyWorkItem,
        SessionReplyStreamEvent,
        SessionEvent,
        MessagePlatformOutbox,
        ScheduledTask,
    ):
        assert list((await db_session.execute(select(model))).scalars().all()) == []

    audit_records = list((await db_session.execute(select(AuditRecord).execution_options(populate_existing=True))).scalars().all())
    assert len(audit_records) == 1
    assert audit_records[0].status == AuditRecordStatus.CANCELLED
    assert list((await db_session.execute(select(AuditConfirmationClaim))).scalars().all()) == []

    assert await db_session.get(BackgroundTask, completed_task_id) is None
    assert await db_session.get(BackgroundTask, pending_task_id) is None
    assert (
        await background_task_crud.try_claim(
            db_session,
            task_id=pending_task_id,
            worker_id="late-worker",
        )
        is None
    )
    running_task = await db_session.get(BackgroundTask, running_task_id)
    assert running_task is not None
    assert running_task.status == BackgroundTaskStatus.CANCELLED
    assert running_task.session_id == f"deleted-session:{running_task_id}"
    assert running_task.auto_reply is False
    assert running_task.reply_status == BackgroundTaskReplyStatus.NONE
    assert running_task.locked_by is None
    assert running_task.lock_until is None
    assert running_task.reply_locked_by is None
    assert running_task.reply_lock_until is None

    retained_memory_job = await db_session.get(LongTermMemoryMutationJob, memory_job.id)
    assert retained_memory_job is not None
    assert retained_memory_job.status == LongTermMemoryMutationStatus.PENDING
    assert retained_memory_job.source_session_id == "session-1"


@pytest.mark.asyncio
async def test_delete_session_data_allows_admin_to_delete_another_users_session(db_session: AsyncSession):
    db_session.add(ChatSession(session_id="admin-session", uid="user-1", profile_id=1))
    db_session.add(
        SessionTodoPlan(
            session_id="admin-session",
            uid="user-1",
            todos=[{"content": "pending todo", "status": "pending"}],
            revision=1,
        )
    )
    await db_session.commit()

    deleted = await delete_session_data(
        db_session,
        session_id="admin-session",
        uid="admin",
        is_admin=True,
    )
    await db_session.commit()

    assert deleted is True
    assert await db_session.get(ChatSession, "admin-session") is None
    assert await db_session.get(SessionTodoPlan, "admin-session") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_count", [1, 2])
async def test_delete_session_data_retries_terminal_process_cleanup_once(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    fail_count: int,
):
    session_id = f"cleanup-retry-session-{fail_count}"
    uid = "cleanup-retry-user"
    terminal_session_id = f"cleanup-retry-terminal-{fail_count}"
    root_pid = os.getpid() + 100_000 + fail_count
    process_identity = {
        "platform": platform.system(),
        "boot_time": psutil.boot_time(),
        "root_pid": root_pid,
        "root_create_time": 10.0,
        "process_group_id": None,
        "known_processes": {str(root_pid): 10.0},
    }
    terminal_session = TerminalSession(
        terminal_session_id=terminal_session_id,
        uid=uid,
        session_id=session_id,
        original_tool_call_id="cleanup-retry-call",
        profile_id=1,
        command="python -i",
        working_directory="/workspace",
        status=TerminalSessionStatus.RUNNING,
        allowed_actions=[TerminalAction.CLOSE.value],
        process_identity=process_identity,
    )
    command = TerminalControlCommand(
        terminal_session_id=terminal_session_id,
        request_id="cleanup-retry-request",
        action=TerminalAction.CLOSE,
        payload={"force": True},
        payload_hash="c" * 64,
        status=TerminalControlCommandStatus.PENDING,
    )
    watch = BackgroundTask(
        uid=uid,
        session_id=session_id,
        profile_id=1,
        tool_call_id="cleanup-retry-watch-call",
        tool_name=TERMINAL_WATCH_TOOL_NAME,
        status=BackgroundTaskStatus.PENDING,
        arguments={
            "terminal_session_id": terminal_session_id,
            "after_offset": 0,
            "condition": "process_exit",
        },
        auto_reply=False,
        reply_status=BackgroundTaskReplyStatus.NONE,
        extra={"terminal_session_id": terminal_session_id},
    )
    message = Message(
        uid=uid,
        session_id=session_id,
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="cleanup retry",
        is_processed=False,
    )
    db_session.add_all(
        [
            ChatSession(session_id=session_id, uid=uid, profile_id=1),
            message,
            terminal_session,
            command,
            watch,
        ]
    )
    await db_session.commit()
    message_id = message.id
    command_id = command.id
    watch_id = watch.id
    assert message_id is not None
    assert command_id is not None
    assert watch_id is not None

    cleanup_calls = 0

    async def cleanup_process_identity(_identity):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls <= fail_count:
            return SimpleNamespace(errors=("process did not terminate",))
        return SimpleNamespace(errors=())

    monkeypatch.setattr(
        terminal_audit_lifecycle_module,
        "cleanup_terminal_process_identity",
        cleanup_process_identity,
    )

    if fail_count == 1:
        deleted = await delete_session_data(
            db_session,
            session_id=session_id,
            uid=uid,
            is_admin=False,
        )
        await db_session.commit()
        db_session.expunge_all()

        assert deleted is True
        assert cleanup_calls == 2
        assert await db_session.get(ChatSession, session_id) is None
        assert await db_session.get(Message, message_id) is None
        assert await db_session.get(TerminalSession, terminal_session_id) is None
        assert await db_session.get(TerminalControlCommand, command_id) is None
        assert await db_session.get(BackgroundTask, watch_id) is None
        return

    with pytest.raises(ServerException) as exc_info:
        await delete_session_data(
            db_session,
            session_id=session_id,
            uid=uid,
            is_admin=False,
        )

    assert exc_info.value.message == ERR_SESSION_DELETE_FAILED
    assert cleanup_calls == 2
    await db_session.rollback()
    db_session.expunge_all()

    retained_session = await db_session.get(ChatSession, session_id)
    retained_message = await db_session.get(Message, message_id)
    retained_terminal = await db_session.get(TerminalSession, terminal_session_id)
    retained_command = await db_session.get(TerminalControlCommand, command_id)
    retained_watch = await db_session.get(BackgroundTask, watch_id)
    assert retained_session is not None
    assert retained_message is not None
    assert retained_terminal is not None
    assert retained_terminal.status is TerminalSessionStatus.RUNNING
    assert retained_terminal.process_identity == process_identity
    assert retained_command is not None
    assert retained_command.status is TerminalControlCommandStatus.PENDING
    assert retained_watch is not None
    assert retained_watch.status is BackgroundTaskStatus.PENDING
    assert retained_watch.session_id == session_id
