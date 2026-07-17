from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.crud.background_task import background_task_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.session_cleanup import delete_session_data
from app.models.audit import AuditConfirmationClaim, AuditRecord, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
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


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    ChatSession.__table__,
                    AuditRecord.__table__,
                    AuditConfirmationClaim.__table__,
                    Message.__table__,
                    SessionReplySequence.__table__,
                    SessionReplyWorkItem.__table__,
                    SessionReplyStreamEvent.__table__,
                    SessionEvent.__table__,
                    MessagePlatformOutbox.__table__,
                    BackgroundTask.__table__,
                    ScheduledTask.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


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
async def test_delete_session_data_rejects_non_owner_without_changes(db_session: AsyncSession):
    await _seed_session_data(db_session)

    deleted = await delete_session_data(
        db_session,
        session_id="session-1",
        uid="user-2",
        is_admin=False,
    )

    assert deleted is False
    assert await db_session.get(ChatSession, "session-1") is not None
    assert list((await db_session.execute(select(Message))).scalars().all())


@pytest.mark.asyncio
async def test_delete_session_data_removes_all_associations_and_cancels_running_task(db_session: AsyncSession):
    completed_task_id, pending_task_id, running_task_id = await _seed_session_data(db_session)

    deleted = await delete_session_data(
        db_session,
        session_id="session-1",
        uid="user-1",
        is_admin=False,
    )
    await db_session.commit()

    assert deleted is True
    assert await db_session.get(ChatSession, "session-1") is None
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
