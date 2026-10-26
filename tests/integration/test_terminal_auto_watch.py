import asyncio
import copy
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.audit.confirmation_lifecycle import stop_interactive_reply
from app.core.background_tasks import reply_trigger as reply_trigger_module
from app.core.background_tasks.terminal_watch_reply import prepare_terminal_auto_watch_resume
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.task.background import background_task_crud
from app.core.dispatchers.context import DispatchContext
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus, TerminalWriteRequest
from app.core.tools.terminal import TerminalReadExecutor
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplySequence, SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession
from app.models.user import User

UID = "terminal-auto-watch-owner"
SESSION_ID = "terminal-auto-watch-chat"
PROFILE_ID = 1
TERMINAL_SESSION_ID = "terminal-auto-watch-session-0123456789abcdef"
SECOND_TERMINAL_SESSION_ID = "terminal-auto-watch-session-fedcba9876543210"
AFTER_OFFSET = 37


@pytest_asyncio.fixture
async def terminal_auto_watch_database(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="terminal-auto-watch-concurrency.sqlite3",
    ) as session_factory:
        async with session_factory() as db:
            db.add_all(
                [
                    User(uid=UID, username="terminal-auto-watch-owner"),
                    Profile(id=PROFILE_ID, uid=UID, name="terminal-auto-watch-profile", configs={}),
                    ChatSession(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                    ),
                    TerminalSession(
                        terminal_session_id=TERMINAL_SESSION_ID,
                        uid=UID,
                        session_id=SESSION_ID,
                        original_tool_call_id="initial-terminal-watch-call",
                        profile_id=PROFILE_ID,
                        command="test-terminal-command",
                        working_directory="/tmp/terminal-auto-watch",
                        status=TerminalSessionStatus.RUNNING,
                        allowed_actions=[action.value for action in TerminalAction],
                    ),
                    TerminalSession(
                        terminal_session_id=SECOND_TERMINAL_SESSION_ID,
                        uid=UID,
                        session_id=SESSION_ID,
                        original_tool_call_id="second-terminal-watch-call",
                        profile_id=PROFILE_ID,
                        command="test-terminal-command-second",
                        working_directory="/tmp/terminal-auto-watch-second",
                        status=TerminalSessionStatus.RUNNING,
                        allowed_actions=[action.value for action in TerminalAction],
                    ),
                ]
            )
            await db.commit()
        yield session_factory


async def _terminal_watch_tasks(db: AsyncSession) -> list[BackgroundTask]:
    result = await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.uid == UID,
            BackgroundTask.session_id == SESSION_ID,
            BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
        )
    )
    return list(result.scalars().all())


async def _create_claimed_reply_work(db: AsyncSession, *, worker_id: str) -> SessionReplyWorkItem:
    work = SessionReplyWorkItem(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="terminal-auto-watch-origin-message",
        dedupe_key=f"terminal-auto-watch-origin:{worker_id}",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=worker_id,
        lock_until=2_000_000_000,
        attempt_count=1,
    )
    db.add(work)
    await db.flush()
    assert work.id is not None
    db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    return work


@pytest.mark.asyncio
async def test_terminal_auto_watch_registration_is_idempotent_and_preserves_manual_watch(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    async with terminal_auto_watch_database() as db:
        manual_task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="model-terminal-watch-call",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 11,
                "condition": "output_contains",
                "text": "ready",
            },
            auto_reply=False,
            extra={
                "source": "model",
                "request_context": {"turn": 4, "reason": "wait for readiness"},
            },
        )
        manual_task_id = manual_task.id
        assert manual_task_id is not None
        manual_snapshot = copy.deepcopy(manual_task.model_dump())

        active_manual = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
        )
        assert active_manual is not None
        assert active_manual.id == manual_task_id

    async def register_auto_watch() -> None:
        async with terminal_auto_watch_database() as db:
            await ensure_terminal_auto_watch(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=AFTER_OFFSET,
                session_status=TerminalSessionStatus.RUNNING,
            )

    await asyncio.gather(register_auto_watch(), register_auto_watch())

    async with terminal_auto_watch_database() as db:
        await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=AFTER_OFFSET,
            session_status=TerminalSessionStatus.RUNNING,
        )

        tasks = await _terminal_watch_tasks(db)
        auto_tasks = [task for task in tasks if isinstance(task.extra, dict) and task.extra.get("auto_terminal_watch") is True]
        assert len(tasks) == 2
        assert len(auto_tasks) == 1

        auto_task = auto_tasks[0]
        assert auto_task.uid == UID
        assert auto_task.session_id == SESSION_ID
        assert auto_task.profile_id == PROFILE_ID
        assert auto_task.status == BackgroundTaskStatus.PENDING
        assert auto_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert auto_task.auto_reply is True
        assert auto_task.arguments == {
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": AFTER_OFFSET,
            "condition": "process_exit",
        }
        assert auto_task.extra.get("terminal_session_id") == TERMINAL_SESSION_ID

        manual_persisted = await db.get(BackgroundTask, manual_task_id)
        assert manual_persisted is not None
        assert manual_persisted.model_dump() == manual_snapshot

        active_task = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
        )
        assert active_task is not None
        assert active_task.id in {manual_task_id, auto_task.id}


@pytest.mark.asyncio
async def test_terminal_auto_watch_registration_preserves_session_reply_work_origin(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    worker_id = "terminal-auto-watch-reply-worker"
    async with terminal_auto_watch_database() as db:
        work = await _create_claimed_reply_work(db, worker_id=worker_id)

        await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=AFTER_OFFSET,
            session_status=TerminalSessionStatus.RUNNING,
        )

        tasks = await _terminal_watch_tasks(db)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.extra["origin_work_id"] == work.id
        assert task.extra["origin_work_identity"] == work.dedupe_key
        assert task.status == BackgroundTaskStatus.PENDING
        assert task.reply_status == BackgroundTaskReplyStatus.PENDING

        persisted_work = await db.get(SessionReplyWorkItem, work.id)
        assert persisted_work is not None
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING
        assert persisted_work.locked_by == worker_id


@pytest.mark.asyncio
async def test_terminal_auto_watch_reply_is_suppressed_after_explicit_terminal_read(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reply_trigger_module, "AsyncSessionLocal", terminal_auto_watch_database)

    async with terminal_auto_watch_database() as db:
        assistant_message = Message(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="terminal output already read",
            is_processed=True,
        )
        db.add(assistant_message)
        await db.flush()
        assert assistant_message.id is not None
        assistant_message_id = assistant_message.id

        work = SessionReplyWorkItem(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="explicit-terminal-read",
            dedupe_key="terminal-auto-watch-explicit-read",
            status=SessionReplyWorkStatus.SUCCEEDED,
            result_message_id=assistant_message.id,
            execution_state={
                "terminal_reads": {
                    TERMINAL_SESSION_ID: {
                        "start_offset": 250,
                        "next_offset": 300,
                    }
                }
            },
            event_sent=True,
        )
        db.add(work)
        await db.flush()
        assert work.id is not None
        work_id = work.id

        auto_task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="terminal-auto-watch-late-reply",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 200,
                "condition": "process_exit",
            },
            auto_reply=True,
            extra={
                "source": "terminal_auto_watch",
                "auto_terminal_watch": True,
                "terminal_session_id": TERMINAL_SESSION_ID,
                "origin_work_id": work.id,
                "origin_work_identity": work.dedupe_key,
            },
            commit=False,
        )
        assert auto_task.id is not None
        auto_task_id = auto_task.id

        worker_id = "terminal-auto-watch-late-reply-worker"
        claimed = await background_task_crud.try_claim(
            db,
            task_id=auto_task.id,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert claimed is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=auto_task.id,
            worker_id=worker_id,
            result={
                "status": "succeeded",
                "tool_name": TERMINAL_WATCH_TOOL_NAME,
                "content": {
                    "event": "process_exit",
                    "eof": True,
                    "next_offset": 300,
                },
            },
            auto_reply=True,
        )
        await db.refresh(auto_task)
        assert auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert auto_task.reply_status == BackgroundTaskReplyStatus.PENDING

    await reply_trigger_module.trigger_background_task_reply(auto_task_id)

    async with terminal_auto_watch_database() as db:
        persisted_auto = await db.get(BackgroundTask, auto_task_id)
        persisted_work = await db.get(SessionReplyWorkItem, work_id)
        result_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.BACKGROUND_TASK_RESULT,
                    )
                )
            )
            .scalars()
            .all()
        )
        summary_work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == SESSION_ID,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert persisted_auto is not None
    assert persisted_auto.status == BackgroundTaskStatus.SUCCEEDED
    assert persisted_auto.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
    assert persisted_work is not None
    assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert persisted_work.result_message_id == assistant_message_id
    assert result_messages == []
    assert summary_work_items == []


@pytest.mark.parametrize("manual_status", [BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING])
@pytest.mark.asyncio
async def test_terminal_auto_watch_reply_is_suppressed_by_active_manual_watch(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    manual_status: BackgroundTaskStatus,
) -> None:
    monkeypatch.setattr(reply_trigger_module, "AsyncSessionLocal", terminal_auto_watch_database)

    async with terminal_auto_watch_database() as db:
        manual_task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="manual-terminal-watch-call",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 0,
                "condition": "process_exit",
            },
            auto_reply=False,
            extra={"source": "model"},
        )
        manual_task.status = manual_status
        if manual_status == BackgroundTaskStatus.RUNNING:
            manual_task.locked_by = "manual-terminal-watch-worker"
            manual_task.lock_until = 2_000_000_000
        db.add(manual_task)
        await db.commit()

        await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=AFTER_OFFSET,
            session_status=TerminalSessionStatus.RUNNING,
        )
        auto_task = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
            automatic_only=True,
        )
        assert auto_task is not None
        assert auto_task.id is not None
        auto_task_id = auto_task.id
        manual_task_id = manual_task.id
        assert manual_task_id is not None

        claimed = await background_task_crud.try_claim(
            db,
            task_id=auto_task_id,
            worker_id="terminal-auto-watch-task-worker",
            lease_seconds=300,
        )
        assert claimed is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=auto_task_id,
            worker_id="terminal-auto-watch-task-worker",
            result={
                "status": "succeeded",
                "tool_name": TERMINAL_WATCH_TOOL_NAME,
                "content": {"event": "process_exit", "eof": True, "next_offset": AFTER_OFFSET},
            },
            auto_reply=True,
        )

    await reply_trigger_module.trigger_background_task_reply(auto_task_id)

    async with terminal_auto_watch_database() as db:
        persisted_manual = await db.get(BackgroundTask, manual_task_id)
        persisted_auto = await db.get(BackgroundTask, auto_task_id)
        result_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.BACKGROUND_TASK_RESULT,
                    )
                )
            )
            .scalars()
            .all()
        )
        summary_work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == SESSION_ID,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert persisted_manual is not None
    assert persisted_manual.status == manual_status
    assert persisted_auto is not None
    assert persisted_auto.status == BackgroundTaskStatus.SUCCEEDED
    assert persisted_auto.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
    assert result_messages == []
    assert summary_work_items == []


@pytest.mark.asyncio
async def test_terminal_auto_watch_reply_is_not_suppressed_by_manual_watch_on_other_terminal(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reply_trigger_module, "AsyncSessionLocal", terminal_auto_watch_database)

    async with terminal_auto_watch_database() as db:
        manual_task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="manual-terminal-watch-call",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 0,
                "condition": "process_exit",
            },
            auto_reply=False,
            extra={"source": "model"},
        )
        manual_task_id = manual_task.id
        assert manual_task_id is not None
        assert manual_task.status == BackgroundTaskStatus.PENDING

        await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=SECOND_TERMINAL_SESSION_ID,
            after_offset=AFTER_OFFSET,
            session_status=TerminalSessionStatus.RUNNING,
        )
        auto_task = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            SECOND_TERMINAL_SESSION_ID,
            automatic_only=True,
        )
        assert auto_task is not None
        assert auto_task.id is not None
        auto_task_id = auto_task.id
        assert auto_task.status == BackgroundTaskStatus.PENDING
        assert auto_task.extra.get("auto_terminal_watch") is True
        assert auto_task.extra.get("terminal_session_id") == SECOND_TERMINAL_SESSION_ID

        claimed = await background_task_crud.try_claim(
            db,
            task_id=auto_task_id,
            worker_id="terminal-auto-watch-other-terminal-worker",
            lease_seconds=300,
        )
        assert claimed is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=auto_task_id,
            worker_id="terminal-auto-watch-other-terminal-worker",
            result={
                "status": "succeeded",
                "tool_name": TERMINAL_WATCH_TOOL_NAME,
                "content": {
                    "event": "process_exit",
                    "eof": True,
                    "next_offset": AFTER_OFFSET,
                    "terminal_session_id": SECOND_TERMINAL_SESSION_ID,
                },
            },
            auto_reply=True,
        )
        await db.refresh(auto_task)
        assert auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert auto_task.reply_status == BackgroundTaskReplyStatus.PENDING

    await reply_trigger_module.trigger_background_task_reply(auto_task_id)

    async with terminal_auto_watch_database() as db:
        persisted_manual = await db.get(BackgroundTask, manual_task_id)
        persisted_auto = await db.get(BackgroundTask, auto_task_id)
        result_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.BACKGROUND_TASK_RESULT,
                    )
                )
            )
            .scalars()
            .all()
        )
        summary_work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == SESSION_ID,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert persisted_manual is not None
    assert persisted_manual.status == BackgroundTaskStatus.PENDING
    assert persisted_auto is not None
    assert persisted_auto.status == BackgroundTaskStatus.SUCCEEDED
    assert persisted_auto.reply_status == BackgroundTaskReplyStatus.RUNNING
    assert len(result_messages) == 1
    result_message = result_messages[0]
    assert result_message.type == MessageType.BACKGROUND_TASK_RESULT
    assert len(summary_work_items) == 1
    summary_work = summary_work_items[0]
    assert summary_work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY
    assert summary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert summary_work.source_id == str(auto_task_id)


@pytest.mark.asyncio
async def test_terminal_auto_watch_replies_group_by_origin_work_across_terminals(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reply_trigger_module, "AsyncSessionLocal", terminal_auto_watch_database)
    worker_id = "terminal-auto-watch-group-worker"

    async with terminal_auto_watch_database() as db:
        db.add(SessionReplySequence(session_id=SESSION_ID, next_sequence_no=3))
        await db.commit()
        work = await _create_claimed_reply_work(db, worker_id=worker_id)

        for terminal_session_id in (TERMINAL_SESSION_ID, SECOND_TERMINAL_SESSION_ID):
            await ensure_terminal_auto_watch(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                terminal_session_id=terminal_session_id,
                after_offset=AFTER_OFFSET,
                session_status=TerminalSessionStatus.RUNNING,
            )

        auto_tasks = sorted(await _terminal_watch_tasks(db), key=lambda task: task.id or 0)
        assert len(auto_tasks) == 2
        assert {task.extra["terminal_session_id"] for task in auto_tasks} == {
            TERMINAL_SESSION_ID,
            SECOND_TERMINAL_SESSION_ID,
        }
        assert all(task.extra["origin_work_id"] == work.id for task in auto_tasks)
        assert all(task.extra["origin_work_identity"] == work.dedupe_key for task in auto_tasks)

        task_ids = [task.id for task in auto_tasks]
        assert all(task_id is not None for task_id in task_ids)
        for task in auto_tasks:
            assert task.id is not None
            claimed = await background_task_crud.try_claim(
                db,
                task_id=task.id,
                worker_id="terminal-auto-watch-task-worker",
                lease_seconds=300,
            )
            assert claimed is not None
            assert await background_task_crud.mark_succeeded(
                db,
                task_id=task.id,
                worker_id="terminal-auto-watch-task-worker",
                result={
                    "status": "succeeded",
                    "tool_name": TERMINAL_WATCH_TOOL_NAME,
                    "content": {
                        "event": "process_exit",
                        "eof": True,
                        "next_offset": AFTER_OFFSET,
                        "terminal_session_id": task.extra["terminal_session_id"],
                    },
                },
                auto_reply=True,
            )

        first_task_id, second_task_id = task_ids
        assert first_task_id is not None
        assert second_task_id is not None
        await reply_trigger_module.trigger_background_task_reply(first_task_id)
        await reply_trigger_module.trigger_background_task_reply(second_task_id)
        assert await prepare_terminal_auto_watch_resume(db, work, worker_id)

    async with terminal_auto_watch_database() as db:
        persisted_tasks = list((await db.execute(select(BackgroundTask).where(BackgroundTask.id.in_(task_ids)).order_by(BackgroundTask.id))).scalars().all())
        summary_work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == SESSION_ID,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                    )
                )
            )
            .scalars()
            .all()
        )
        result_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.BACKGROUND_TASK_RESULT,
                    )
                )
            )
            .scalars()
            .all()
        )
        persisted_work = await db.get(SessionReplyWorkItem, work.id)

    assert len(persisted_tasks) == 2
    assert all(task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED for task in persisted_tasks)
    assert all(task.extra["terminal_auto_watch_routed"] is True for task in persisted_tasks)
    assert summary_work_items == []
    assert len(result_messages) == 2
    result_payloads = [json.loads(message.content) for message in result_messages]
    assert all(payload["type"] == "background_tool_result" for payload in result_payloads)
    assert all(payload["ui_only"] is True for payload in result_payloads)
    assert {payload["task_id"] for payload in result_payloads} == set(task_ids)

    assert persisted_work is not None
    assert persisted_work.status == SessionReplyWorkStatus.RUNNING
    checkpoint = (persisted_work.execution_state or {}).get("dispatcher_checkpoint") or {}
    assert checkpoint["reload_history"] is True
    assert "external_resume_messages" not in checkpoint
    assert checkpoint["pending_resume_message_ids"] == [message.id for message in result_messages]
    resume_messages = parse_db_messages_to_internal(result_messages)
    assert len(resume_messages) == 2
    assert all(message.role == MessageRole.USER for message in resume_messages)
    assert [message.id for message in resume_messages] == [message.id for message in result_messages]
    resume_payloads = [json.loads(message.content) for message in resume_messages]
    assert all(payload["type"] == "external_work_resume" for payload in resume_payloads)
    assert {task["task_id"] for payload in resume_payloads for task in payload["tasks"]} == set(task_ids)
    assert [payload["tasks"] for payload in resume_payloads] == [[json.loads(message.content)] for message in result_messages]


@pytest.mark.parametrize(
    ("observed_content", "expected_next_offset"),
    [
        pytest.param(
            {
                "event": "timeout",
                "eof": False,
                "next_offset": 19,
                "observed_at": "2026-10-24T21:40:00",
            },
            19,
            id="timeout-renews",
        ),
        pytest.param(
            {
                "event": "output_gap",
                "eof": False,
                "next_offset": 19,
                "observed_at": "2026-10-24T21:40:00",
            },
            None,
            id="removed-output-gap-does-not-renew",
        ),
        pytest.param(
            {
                "event": "observation_error",
                "eof": False,
                "next_offset": 19,
                "observed_at": "2026-10-24T21:40:00",
            },
            19,
            id="observation-error-renews",
        ),
        pytest.param(
            {"event": "process_exit", "eof": True, "next_offset": 19},
            None,
            id="process-exit-stops",
        ),
        pytest.param(
            {"event": "output_gap", "eof": True, "next_offset": 19},
            None,
            id="output-gap-eof-stops",
        ),
        pytest.param(
            {"event": "observation_error", "eof": True, "next_offset": 19},
            None,
            id="observation-error-eof-stops",
        ),
        pytest.param(
            {"event": "timeout", "eof": True, "next_offset": 19},
            None,
            id="eof-stops",
        ),
        pytest.param(
            {
                "event": "unknown",
                "eof": False,
                "next_offset": 19,
                "observed_at": "2026-10-24T21:40:00",
            },
            None,
            id="unknown-event-stops",
        ),
    ],
)
@pytest.mark.asyncio
async def test_terminal_auto_watch_renews_after_timeout_and_stops_at_eof(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    observed_content: dict,
    expected_next_offset: int | None,
) -> None:
    async with terminal_auto_watch_database() as db:
        task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="terminal-auto-watch:cycle-test",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 0,
                "condition": "process_exit",
            },
            auto_reply=True,
            extra={
                "source": "terminal_auto_watch",
                "auto_terminal_watch": True,
                "terminal_session_id": TERMINAL_SESSION_ID,
            },
        )
        assert task.id is not None

        worker_id = "terminal-auto-watch-cycle-worker"
        claimed_task = await background_task_crud.try_claim(
            db,
            task_id=task.id,
            worker_id=worker_id,
        )
        assert claimed_task is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=task.id,
            worker_id=worker_id,
            result={
                "status": "succeeded",
                "tool_name": TERMINAL_WATCH_TOOL_NAME,
                "content": observed_content,
            },
            auto_reply=True,
        )

        await db.refresh(task)
        task.reply_status = BackgroundTaskReplyStatus.SUCCEEDED
        db.add(task)
        await db.commit()

        next_task = await background_task_crud.create_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            previous_task_id=task.id,
        )

        if expected_next_offset is None:
            assert next_task is None
            tasks = await _terminal_watch_tasks(db)
            assert len(tasks) == 1
            assert tasks[0].extra.get("next_auto_watch_task_id") is None
            return

        assert next_task is not None
        assert next_task.arguments == {
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": expected_next_offset,
            "condition": "process_exit",
        }
        assert next_task.auto_reply is True
        assert next_task.extra == {
            "source": "terminal_auto_watch",
            "auto_terminal_watch": True,
            "terminal_session_id": TERMINAL_SESSION_ID,
            "observation_started_at": observed_content["observed_at"],
        }

        await db.refresh(task)
        assert task.extra.get("next_auto_watch_task_id") == next_task.id
        tasks = await _terminal_watch_tasks(db)
        assert len(tasks) == 2


@pytest.mark.asyncio
async def test_terminal_auto_watch_stop_between_summary_delivery_and_renewal_preserves_terminal_session(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_id = "terminal-auto-watch-summary-worker"
    async with terminal_auto_watch_database() as db:
        task = BackgroundTask(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="terminal-auto-watch-stop-race-call",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            status=BackgroundTaskStatus.SUCCEEDED,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 0,
                "condition": "process_exit",
            },
            result={
                "status": "succeeded",
                "tool_name": TERMINAL_WATCH_TOOL_NAME,
                "content": {
                    "event": "timeout",
                    "eof": False,
                    "next_offset": AFTER_OFFSET,
                },
            },
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.RUNNING,
            extra={
                "source": "terminal_auto_watch",
                "auto_terminal_watch": True,
                "terminal_session_id": TERMINAL_SESSION_ID,
            },
        )
        db.add(task)
        await db.flush()
        assert task.id is not None

        work = SessionReplyWorkItem(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            sequence_no=1,
            work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id=str(task.id),
            dedupe_key="terminal-auto-watch-stop-race-work",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by=worker_id,
            lock_until=2_000_000_000,
            attempt_count=1,
        )
        db.add(work)
        await db.flush()
        assert work.id is not None
        result_message = Message(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="terminal watch summary",
            is_processed=True,
            dedupe_key=_result_message_dedupe_key(work),
        )
        db.add(result_message)
        await db.commit()
        work_id = work.id
        task_id = task.id

    event_reached = asyncio.Event()
    release_event = asyncio.Event()

    async def controlled_send_session_event(_uid: str, _session_id: str, event: dict) -> None:
        assert event["type"] == "proactive_reply"
        event_reached.set()
        await release_event.wait()

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", terminal_auto_watch_database)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", controlled_send_session_event)

    execution_task = asyncio.create_task(executor_lifecycle_module.execute_session_reply_work(work_id, worker_id))
    try:
        await asyncio.wait_for(event_reached.wait(), timeout=2)
        async with terminal_auto_watch_database() as stop_db:
            assert await stop_interactive_reply(stop_db, uid=UID, session_id=SESSION_ID) == 1
        release_event.set()
        await asyncio.wait_for(execution_task, timeout=2)
    finally:
        release_event.set()
        if not execution_task.done():
            execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)

    async with terminal_auto_watch_database() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        task = await db.get(BackgroundTask, task_id)
        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        close_commands = list(
            (
                await db.execute(
                    select(TerminalControlCommand).where(
                        TerminalControlCommand.terminal_session_id == TERMINAL_SESSION_ID,
                        TerminalControlCommand.action == TerminalAction.CLOSE,
                    )
                )
            )
            .scalars()
            .all()
        )
        tasks = await _terminal_watch_tasks(db)

    assert work is not None
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None
    assert task is not None
    assert task.status == BackgroundTaskStatus.SUCCEEDED
    assert task.auto_reply is False
    assert task.reply_status == BackgroundTaskReplyStatus.NONE
    assert task.extra.get("next_auto_watch_task_id") is None
    assert [item.id for item in tasks] == [task_id]
    assert terminal_session is not None
    assert terminal_session.status == TerminalSessionStatus.RUNNING
    assert close_commands == []

    async with terminal_auto_watch_database() as stop_db:
        assert await stop_interactive_reply(stop_db, uid=UID, session_id=SESSION_ID) == 0


@pytest.mark.asyncio
async def test_terminal_read_status_query_does_not_restart_cancelled_auto_watch_without_worker(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    write_request_id = "terminal-write-status-pending"

    async with terminal_auto_watch_database() as db:
        await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        auto_task = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
            automatic_only=True,
        )
        assert auto_task is not None
        assert auto_task.id is not None
        auto_task_id = auto_task.id

        cancelled_task = await background_task_crud.cancel_user_task(db, task_id=auto_task_id, uid=UID)
        assert cancelled_task is not None
        assert cancelled_task.status == BackgroundTaskStatus.CANCELLED

        write_request = TerminalWriteRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            request_id=write_request_id,
            data="pending terminal input",
        )
        write_command, created = await terminal_session_manager.enqueue_control(
            db,
            UID,
            SESSION_ID,
            write_request,
        )
        assert created
        assert write_command.id is not None
        assert write_command.status == TerminalControlCommandStatus.PENDING

        profile = await db.get(Profile, PROFILE_ID)
        assert profile is not None
        executor = TerminalReadExecutor(project_root=".", uid=UID)
        executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 0.05}}))
        executor.set_runtime_context(
            dispatch_context=DispatchContext(
                mode="interactive",
                source="test",
                uid=UID,
                session_id=SESSION_ID,
                profile=profile,
                db=db,
                tool_call_id="terminal-read-status-query",
            )
        )

        response = json.loads(
            await asyncio.wait_for(
                executor.execute(
                    terminal_session_id=TERMINAL_SESSION_ID,
                    request_id=write_request_id,
                    read_output=False,
                ),
                timeout=1,
            )
        )

        assert response["status"] == TerminalSessionStatus.RUNNING.value
        assert response["output_buffer"] == {
            "capacity_bytes": 1_048_576,
            "oldest_offset": 0,
            "next_offset": 0,
            "oldest_sequence": 1,
            "next_sequence": 1,
        }
        assert response["write_request"] == {
            "request_id": write_request_id,
            "command_status": TerminalControlCommandStatus.PENDING.value,
            "result": None,
            "error": None,
        }
        assert "output" not in response

        tasks = await _terminal_watch_tasks(db)
        commands = list(
            (
                await db.execute(
                    select(TerminalControlCommand).where(
                        TerminalControlCommand.terminal_session_id == TERMINAL_SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        active_task = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
        )

    assert len(tasks) == 1
    assert tasks[0].id == auto_task_id
    assert tasks[0].status == BackgroundTaskStatus.CANCELLED
    assert len(commands) == 1
    assert commands[0].id == write_command.id
    assert commands[0].action == TerminalAction.WRITE
    assert commands[0].request_id == write_request_id
    assert commands[0].status == TerminalControlCommandStatus.PENDING
    assert active_task is None
