import asyncio
import copy
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.terminal.audit_lifecycle as terminal_audit_lifecycle_module
import app.core.terminal.watcher as watcher_module
import app.core.tools.terminal as terminal_tool_module
from app.core.audit.confirmation_lifecycle import stop_interactive_reply
from app.core.background_tasks import reply_trigger as reply_trigger_module
from app.core.background_tasks import runner as runner_module
from app.core.background_tasks.manager import background_task_manager
from app.core.background_tasks.terminal_watch_coordinator import (
    complete_terminal_watch_round,
    create_terminal_auto_watch,
    create_terminal_watch_subscription,
)
from app.core.background_tasks.terminal_watch_reply import prepare_terminal_auto_watch_resume, route_terminal_auto_watch_reply
from app.core.constants import (
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SHELL_TOOL_NAME,
    TERMINAL_READ_TOOL_NAME,
    TERMINAL_WATCH_TOOL_NAME,
    TERMINAL_WRITE_TOOL_NAME,
)
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.dispatchers.context import DispatchContext
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus, TerminalWriteRequest
from app.core.tools.terminal import TerminalCloseExecutor, TerminalReadExecutor
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.dispatcher.external_work import build_terminal_wait_response
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.terminal_watch_snapshot import append_terminal_watch_snapshot, load_current_terminal_watch_snapshot
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
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
                        process_identity={"pid": 1},
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
                        process_identity={"pid": 2},
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


async def _create_claimed_reply_work(
    db: AsyncSession,
    *,
    worker_id: str,
) -> SessionReplyWorkItem:
    sequence_no = await session_reply_work_item_crud.allocate_sequence_no(db, SESSION_ID)
    work = SessionReplyWorkItem(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        sequence_no=sequence_no,
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


async def _create_claimed_auto_watch(
    db: AsyncSession,
    *,
    worker_id: str,
    after_offset: int = 0,
) -> tuple[SessionReplyWorkItem, BackgroundTask]:
    work = await _create_claimed_reply_work(db, worker_id=worker_id)
    task = await ensure_terminal_auto_watch(
        db,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        terminal_session_id=TERMINAL_SESSION_ID,
        after_offset=after_offset,
        session_status=TerminalSessionStatus.RUNNING,
    )
    assert task is not None
    assert task.id is not None
    return work, task


async def _complete_auto_watch(
    db: AsyncSession,
    *,
    task: BackgroundTask,
    worker_id: str,
    content: dict,
) -> None:
    assert task.id is not None
    claimed = await background_task_crud.try_claim(
        db,
        task_id=task.id,
        worker_id=worker_id,
        lease_seconds=300,
    )
    assert claimed is not None
    assert await complete_terminal_watch_round(
        db,
        task=task,
        worker_id=worker_id,
        content=content,
    )
    await db.refresh(task)


async def _submit_terminal_watch_subscription(
    db: AsyncSession,
    *,
    tool_call_id: str,
) -> tuple[BackgroundTask, InternalToolCall, InternalMessage, str]:
    profile = await db.get(Profile, PROFILE_ID)
    assert profile is not None
    tool_call = InternalToolCall(
        id=tool_call_id,
        name=TERMINAL_WATCH_TOOL_NAME,
        arguments={
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": 0,
            "condition": "output_contains",
            "text": "never-seen",
        },
    )
    assistant = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call])
    task = await background_task_manager.submit(
        db,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        arguments=tool_call.arguments,
        messages=[assistant],
        source="model",
    )
    assert task.id is not None
    queued_payload = {
        "status": "queued",
        "task_id": task.id,
        "terminal_session_id": TERMINAL_SESSION_ID,
        "session_status": TerminalSessionStatus.RUNNING.value,
    }
    queued_body = json.dumps(queued_payload, ensure_ascii=False)
    tool_result = await save_tool_response(
        db,
        SESSION_ID,
        UID,
        PROFILE_ID,
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=queued_body,
        ),
        [],
        [],
    )
    assert tool_result.id is not None
    return task, tool_call, tool_result, queued_body


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
async def test_terminal_auto_watch_background_survives_snapshot_and_renewal(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    request_content = "Run the terminal command and keep watching it."
    later_request_content = "A later unrelated request must not replace the terminal context."
    purpose = "Keep watching this terminal until the process exits."
    expected_snapshot_open = "<current_terminal_watch_snapshot>"
    expected_snapshot_close = "</current_terminal_watch_snapshot>"

    async with terminal_auto_watch_database() as db:
        request_message = await save_message(
            db,
            SESSION_ID,
            UID,
            MessageRole.USER,
            MessageType.TEXT,
            InternalMessage.from_user_input(content=request_content),
            PROFILE_ID,
        )
        assert request_message.id is not None

        assistant_message = await save_message(
            db,
            SESSION_ID,
            UID,
            MessageRole.ASSISTANT,
            MessageType.TOOL_CALL,
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content=purpose,
                tool_calls=[
                    InternalToolCall(
                        id="initial-terminal-watch-call",
                        name=SHELL_TOOL_NAME,
                        arguments={"command": "test-terminal-command", "execution_mode": "interactive"},
                    )
                ],
            ),
            PROFILE_ID,
        )
        assert assistant_message.id is not None

        worker_id = "terminal-auto-watch-background-worker"
        work = await _create_claimed_reply_work(db, worker_id=worker_id)
        work.input_message_ids = [request_message.id]
        db.add(work)
        await db.flush()
        assert work.input_message_ids == [request_message.id]

        task = await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        assert task is not None
        assert task.id is not None
        expected_request_context = [{"id": request_message.id, "role": MessageRole.USER.value, "content": request_content}]
        assert task.extra["request_context"] == expected_request_context
        assert task.extra["purpose"] == purpose

        snapshot = await load_current_terminal_watch_snapshot(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
        )
        assert snapshot is not None
        snapshot_payload = json.loads(snapshot[len(expected_snapshot_open) : -len(expected_snapshot_close)])
        terminal = next(item for item in snapshot_payload["terminals"] if item["terminal_session_id"] == TERMINAL_SESSION_ID)
        assert terminal["terminal_session_id"] == TERMINAL_SESSION_ID
        assert terminal["status"] == TerminalSessionStatus.RUNNING.value
        assert terminal["command"] == "test-terminal-command"
        assert terminal["working_directory"] == "/tmp/terminal-auto-watch"
        assert terminal["next_output_offset"] == 0
        assert terminal["request_context"] == expected_request_context
        assert terminal["purpose"] == purpose

        claimed_task = await background_task_crud.try_claim(
            db,
            task_id=task.id,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert claimed_task is not None

        later_request = await save_message(
            db,
            SESSION_ID,
            UID,
            MessageRole.USER,
            MessageType.TEXT,
            InternalMessage.from_user_input(content=later_request_content),
            PROFILE_ID,
        )
        assert later_request.id is not None

        assert await complete_terminal_watch_round(
            db,
            task=claimed_task,
            worker_id=worker_id,
            content={"event": "timeout", "output": "", "next_offset": 37, "eof": False},
        )

        tasks = await _terminal_watch_tasks(db)
        assert len(tasks) == 2
        next_task = next(candidate for candidate in tasks if candidate.id != task.id)
        assert next_task.arguments["after_offset"] == 37
        assert next_task.extra["request_context"] == expected_request_context
        assert next_task.extra["purpose"] == purpose
        assert later_request.id not in [item["id"] for item in next_task.extra["request_context"]]
        assert later_request_content not in json.dumps(next_task.extra, ensure_ascii=False)

        renewed_snapshot = await load_current_terminal_watch_snapshot(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
        )
        assert renewed_snapshot is not None
        renewed_payload = json.loads(renewed_snapshot[len(expected_snapshot_open) : -len(expected_snapshot_close)])
        renewed_terminal = next(item for item in renewed_payload["terminals"] if item["terminal_session_id"] == TERMINAL_SESSION_ID)
        assert renewed_terminal["request_context"] == expected_request_context
        assert renewed_terminal["purpose"] == purpose
        assert later_request_content not in json.dumps(renewed_terminal, ensure_ascii=False)

        summary_base = '<conversation_summary id="terminal-background-summary">compressed history</conversation_summary>'
        summary_message = InternalMessage(role=MessageRole.ASSISTANT, content=summary_base)
        original_request_snapshot = request_message.model_dump(mode="json")
        compressed_messages = append_terminal_watch_snapshot([request_message, summary_message], renewed_snapshot)
        assert is_context_summary_message(summary_message)
        assert is_context_summary_message(compressed_messages[1])
        assert compressed_messages[0].content == request_content
        assert request_message.model_dump(mode="json") == original_request_snapshot
        assert summary_message.content == summary_base
        assert compressed_messages[1].content == f"{summary_base}\n\n{renewed_snapshot}"

        compressed_snapshot = compressed_messages[1].content[len(summary_base) + 2 :]
        compressed_payload = json.loads(compressed_snapshot[len(expected_snapshot_open) : -len(expected_snapshot_close)])
        compressed_terminal = next(item for item in compressed_payload["terminals"] if item["terminal_session_id"] == TERMINAL_SESSION_ID)
        assert compressed_terminal["request_context"] == expected_request_context
        assert compressed_terminal["purpose"] == purpose


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
async def test_terminal_auto_watch_reply_is_not_suppressed_by_active_manual_watch(
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
    assert persisted_auto.reply_status == BackgroundTaskReplyStatus.RUNNING
    assert len(result_messages) == 0
    assert len(summary_work_items) == 1
    summary_work = summary_work_items[0]
    assert summary_work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY
    assert summary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert summary_work.source_id == str(auto_task_id)
    summary_state = dict(summary_work.execution_state or {})
    external_resume_context = summary_state.get("external_resume_context")
    assert isinstance(external_resume_context, list)
    assert len(external_resume_context) == 1
    resume_payload = external_resume_context[0]
    assert isinstance(resume_payload, dict)
    assert resume_payload["task_id"] == auto_task_id
    assert resume_payload["task"]["content"] == {
        "event": "process_exit",
        "eof": True,
        "next_offset": AFTER_OFFSET,
    }
    assert json.loads(resume_payload["tool_result"]["content"]) == persisted_auto.result
    checkpoint = dict(summary_state.get("dispatcher_checkpoint") or {})
    assert checkpoint["reload_history"] is True
    assert checkpoint["deferred_external_results"] is True


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
    assert len(result_messages) == 0
    assert len(summary_work_items) == 1
    summary_work = summary_work_items[0]
    assert summary_work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY
    assert summary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert summary_work.source_id == str(auto_task_id)
    summary_state = dict(summary_work.execution_state or {})
    external_resume_context = summary_state.get("external_resume_context")
    assert isinstance(external_resume_context, list)
    assert len(external_resume_context) == 1
    resume_payload = external_resume_context[0]
    assert isinstance(resume_payload, dict)
    assert resume_payload["task_id"] == auto_task_id
    assert resume_payload["task"]["content"] == {
        "event": "process_exit",
        "eof": True,
        "next_offset": AFTER_OFFSET,
        "terminal_session_id": SECOND_TERMINAL_SESSION_ID,
    }
    assert json.loads(resume_payload["tool_result"]["content"]) == persisted_auto.result
    checkpoint = dict(summary_state.get("dispatcher_checkpoint") or {})
    assert checkpoint["reload_history"] is True
    assert checkpoint["deferred_external_results"] is True


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        pytest.param(
            SHELL_TOOL_NAME,
            {"command": "echo terminal-auto-watch-sync", "execution_mode": "interactive"},
            id="shell",
        ),
        pytest.param(
            TERMINAL_WRITE_TOOL_NAME,
            {"terminal_session_id": TERMINAL_SESSION_ID, "data": "input\n"},
            id="write",
        ),
        pytest.param(
            TERMINAL_READ_TOOL_NAME,
            {"terminal_session_id": TERMINAL_SESSION_ID, "offset": 0, "read_output": True},
            id="read",
        ),
    ],
)
@pytest.mark.asyncio
async def test_terminal_sync_receipts_suspend_claimed_work_without_rewriting_tool_result(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    tool_name: str,
    arguments: dict,
) -> None:
    worker_id = f"terminal-auto-watch-sync-{tool_name}"
    receipt_payload = {
        "status": "succeeded",
        "terminal_session_id": TERMINAL_SESSION_ID,
        "session_status": TerminalSessionStatus.RUNNING.value,
    }

    async with terminal_auto_watch_database() as db:
        work, auto_task = await _create_claimed_auto_watch(db, worker_id=worker_id)
        tool_call = InternalToolCall(
            id=f"terminal-auto-watch-sync-{tool_name}-call",
            name=tool_name,
            arguments=arguments,
        )
        receipt_body = json.dumps(receipt_payload, ensure_ascii=False)
        tool_result = await save_tool_response(
            db,
            SESSION_ID,
            UID,
            PROFILE_ID,
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call.id,
                content=receipt_body,
            ),
            [],
            [],
        )
        assert tool_result.id is not None

        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[tool_call],
            tool_results=[tool_result],
            response_id=f"terminal-auto-watch-sync-{tool_name}-response",
        )
        assert wait_response is not None
        assert wait_response["status"] == "waiting_external_work"
        assert len(wait_response["pending_tools"]) == 1
        pending_tool = wait_response["pending_tools"][0]
        assert pending_tool["task_id"] == auto_task.id
        assert pending_tool["tool_call_id"] == tool_call.id
        assert pending_tool["tool_name"] == tool_name
        assert pending_tool["tool_result_message_id"] == tool_result.id
        assert pending_tool["terminal_auto_watch"] is True

        assert await external_work_crud.suspend_claimed(
            db,
            work=work,
            worker_id=worker_id,
            pending_tools=wait_response["pending_tools"],
        )

        persisted_work = await db.get(SessionReplyWorkItem, work.id)
        assert persisted_work is not None
        assert persisted_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert persisted_work.result_message_id is None
        execution_state = persisted_work.execution_state or {}
        assert execution_state["pending_tools"] == []
        assert len(execution_state["terminal_watch_waits"]) == 1
        assert execution_state["terminal_watch_waits"][0]["task_id"] == auto_task.id

        persisted_tool_result = await db.get(Message, tool_result.id)
        assert persisted_tool_result is not None
        persisted_internal_result = InternalMessage.model_validate_json(persisted_tool_result.content or "{}")
        assert persisted_internal_result.tool_call_id == tool_call.id
        assert json.loads(persisted_internal_result.content or "{}") == receipt_payload
        assert (
            list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == SESSION_ID,
                            Message.role == MessageRole.ASSISTANT,
                            Message.type == MessageType.TEXT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            == []
        )
        assert (
            list(
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
            == []
        )

        if tool_name != TERMINAL_READ_TOOL_NAME:
            return

        cancelled_auto = await background_task_crud.cancel_user_task(db, task_id=auto_task.id, uid=UID)
        assert cancelled_auto is not None
        assert cancelled_auto.status == BackgroundTaskStatus.CANCELLED

        await db.refresh(persisted_work)
        waiting_state = dict(persisted_work.execution_state or {})
        assert await external_work_crud.wake_waiting(
            db,
            work=persisted_work,
            execution_state=waiting_state,
        )
        resumed_work = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=f"{worker_id}-resume",
            lease_seconds=300,
        )
        assert resumed_work is not None
        assert resumed_work.id == work.id
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, f"{worker_id}-resume")

        read_wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[tool_call],
            tool_results=[tool_result],
            response_id="terminal-auto-watch-read-after-cancel",
        )
        assert read_wait_response is None
        assert (
            await background_task_crud.get_active_terminal_watch(
                db,
                UID,
                SESSION_ID,
                TERMINAL_SESSION_ID,
                automatic_only=True,
            )
            is None
        )
        assert len(await _terminal_watch_tasks(db)) == 1


@pytest.mark.asyncio
async def test_terminal_watch_subscription_timeout_routes_to_existing_work_without_summary(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    worker_id = "terminal-auto-watch-subscription-worker"
    timeout_content = {
        "event": "timeout",
        "eof": False,
        "next_offset": 0,
    }

    async with terminal_auto_watch_database() as db:
        work, auto_task = await _create_claimed_auto_watch(db, worker_id=worker_id)
        subscription, tool_call, queued_result, queued_body = await _submit_terminal_watch_subscription(
            db,
            tool_call_id="terminal-auto-watch-subscription-call",
        )
        assert subscription.auto_reply is False
        assert subscription.status == BackgroundTaskStatus.PENDING
        assert subscription.extra["origin_work_id"] == work.id
        assert subscription.extra["origin_work_identity"] == work.dedupe_key

        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[tool_call],
            tool_results=[queued_result],
            response_id="terminal-auto-watch-subscription-response",
        )
        assert wait_response is not None
        assert wait_response["status"] == "waiting_external_work"
        assert len(wait_response["pending_tools"]) == 1
        pending_tool = wait_response["pending_tools"][0]
        assert pending_tool["task_id"] == subscription.id
        assert pending_tool["tool_call_id"] == tool_call.id
        assert pending_tool["terminal_watch"] is True
        assert "terminal_auto_watch" not in pending_tool
        assert await external_work_crud.suspend_claimed(
            db,
            work=work,
            worker_id=worker_id,
            pending_tools=wait_response["pending_tools"],
        )

        await _complete_auto_watch(
            db,
            task=auto_task,
            worker_id=worker_id,
            content=timeout_content,
        )
        assert await route_terminal_auto_watch_reply(db, auto_task)

        persisted_work = await db.get(SessionReplyWorkItem, work.id)
        persisted_subscription = await db.get(BackgroundTask, subscription.id)
        assert persisted_work is not None
        await db.refresh(persisted_work)
        assert persisted_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert persisted_subscription is not None
        assert persisted_subscription.status == BackgroundTaskStatus.PENDING
        next_task_id = auto_task.extra.get("next_auto_watch_task_id")
        assert isinstance(next_task_id, int)
        next_task = await db.get(BackgroundTask, next_task_id)
        assert next_task is not None
        assert next_task.status == BackgroundTaskStatus.PENDING
        assert next_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert (
            len(
                list(
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
            )
            == 0
        )
        assert len(list((await db.execute(select(SessionReplyWorkItem))).scalars().all())) == 1

        resume_worker_id = "terminal-auto-watch-subscription-resume-worker"
        claimed_work = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=resume_worker_id,
            lease_seconds=300,
        )
        assert claimed_work is not None
        assert claimed_work.id == work.id
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, resume_worker_id)
        assert await prepare_terminal_auto_watch_resume(db, claimed_work, resume_worker_id)

        result_markers = list(
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
        assert len(result_markers) == 1
        marker = result_markers[0]
        assert marker.role == MessageRole.USER
        marker_payload = json.loads(marker.content or "{}")
        assert marker_payload["task_id"] == auto_task.id
        assert marker_payload["task"] == timeout_content

        await db.refresh(claimed_work)
        await db.refresh(persisted_subscription)
        assert claimed_work.status == SessionReplyWorkStatus.RUNNING
        assert claimed_work.result_message_id is None
        assert persisted_subscription.status == BackgroundTaskStatus.PENDING
        queued_row = await db.get(Message, queued_result.id)
        assert queued_row is not None
        assert InternalMessage.model_validate_json(queued_row.content or "{}").content == queued_body
        assert (
            list(
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
            == []
        )


@pytest.mark.asyncio
async def test_terminal_exited_auto_watch_rebinds_to_new_work_before_subscription_wait(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    old_worker_id = "terminal-auto-watch-exited-old-worker"
    new_worker_id = "terminal-auto-watch-exited-new-worker"
    completion = {
        "event": "process_exit",
        "eof": True,
        "next_offset": 0,
        "terminal_session_id": TERMINAL_SESSION_ID,
        "exit_code": 0,
        "session_status": TerminalSessionStatus.EXITED.value,
    }

    async with terminal_auto_watch_database() as db:
        old_work, auto_task = await _create_claimed_auto_watch(db, worker_id=old_worker_id)
        assert auto_task.id is not None
        old_auto_task_id = auto_task.id
        assert await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=old_work.id,
            worker_id=old_worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
        )

        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        terminal_session.status = TerminalSessionStatus.EXITED
        terminal_session.exit_code = 0
        db.add(terminal_session)
        await db.commit()

        new_work = await _create_claimed_reply_work(db, worker_id=new_worker_id)
        assert new_work.dedupe_key != old_work.dedupe_key
        subscription, tool_call, queued_result, queued_body = await _submit_terminal_watch_subscription(
            db,
            tool_call_id="terminal-auto-watch-exited-rebind-subscription",
        )
        auto_tasks = [task for task in await _terminal_watch_tasks(db) if task.extra.get("auto_terminal_watch") is True]
        assert len(auto_tasks) == 1
        rebound_auto = auto_tasks[0]
        await db.refresh(rebound_auto)
        assert rebound_auto.id == old_auto_task_id
        assert rebound_auto.extra["origin_work_id"] == new_work.id
        assert rebound_auto.extra["origin_work_identity"] == new_work.dedupe_key

        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[tool_call],
            tool_results=[queued_result],
            response_id="terminal-auto-watch-exited-rebind-response",
        )
        assert wait_response is not None
        assert wait_response["pending_tools"][0]["task_id"] == subscription.id
        assert await external_work_crud.suspend_claimed(
            db,
            work=new_work,
            worker_id=new_worker_id,
            pending_tools=wait_response["pending_tools"],
        )

        await _complete_auto_watch(
            db,
            task=rebound_auto,
            worker_id=new_worker_id,
            content=completion,
        )
        assert await route_terminal_auto_watch_reply(db, rebound_auto)

        await db.refresh(old_work)
        await db.refresh(new_work)
        await db.refresh(subscription)
        await db.refresh(rebound_auto)
        assert old_work.status == SessionReplyWorkStatus.SUCCEEDED
        assert old_work.result_message_id is None
        assert new_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert subscription.status == BackgroundTaskStatus.SUCCEEDED
        assert rebound_auto.id == old_auto_task_id
        assert rebound_auto.result is not None
        assert rebound_auto.result["content"] == {**completion, "completed_watch_task_ids": [subscription.id]}
        assert rebound_auto.extra.get("next_auto_watch_task_id") is None
        assert len([task for task in await _terminal_watch_tasks(db) if task.extra.get("auto_terminal_watch") is True]) == 1
        queued_row = await db.get(Message, queued_result.id)
        assert queued_row is not None
        assert InternalMessage.model_validate_json(queued_row.content or "{}").content == queued_body


@pytest.mark.parametrize("old_result_consumed", [False, True])
@pytest.mark.asyncio
async def test_terminal_exited_completed_auto_watch_reuses_cached_result_for_new_subscription(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    old_result_consumed: bool,
) -> None:
    old_worker_id = "terminal-auto-watch-cached-old-worker"
    new_worker_id = "terminal-auto-watch-cached-new-worker"
    completion = {
        "event": "process_exit",
        "eof": True,
        "next_offset": AFTER_OFFSET,
        "terminal_session_id": TERMINAL_SESSION_ID,
        "exit_code": 0,
        "session_status": TerminalSessionStatus.EXITED.value,
    }

    async with terminal_auto_watch_database() as db:

        async def result_markers() -> list[Message]:
            result = await db.execute(select(Message).where(Message.session_id == SESSION_ID, Message.type == MessageType.BACKGROUND_TASK_RESULT).order_by(Message.id))
            return list(result.scalars().all())

        async def work_ready_ids() -> list[int | None]:
            result = await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID).order_by(SessionEvent.id))
            return [event.id for event in result.scalars().all() if event.event.get("type") == "work_ready"]

        async def auto_watch_tasks() -> list[BackgroundTask]:
            return [task for task in await _terminal_watch_tasks(db) if isinstance(task.extra, dict) and task.extra.get("auto_terminal_watch") is True]

        async def active_auto_watch() -> BackgroundTask | None:
            return await background_task_crud.get_active_terminal_watch(
                db,
                UID,
                SESSION_ID,
                TERMINAL_SESSION_ID,
                automatic_only=True,
            )

        old_work, auto_task = await _create_claimed_auto_watch(db, worker_id=old_worker_id)
        assert auto_task.id is not None
        auto_task_id = auto_task.id

        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        terminal_session.status = TerminalSessionStatus.EXITED
        terminal_session.exit_code = 0
        db.add(terminal_session)
        await db.commit()

        await _complete_auto_watch(
            db,
            task=auto_task,
            worker_id=old_worker_id,
            content=completion,
        )
        await db.refresh(auto_task)
        auto_result_snapshot = copy.deepcopy(auto_task.result)

        old_marker_id: int | None = None
        old_marker_content: str | None = None
        if old_result_consumed:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (old_work.id, old_worker_id)
            assert await prepare_terminal_auto_watch_resume(db, old_work, old_worker_id)
            checkpoint = (old_work.execution_state or {}).get("dispatcher_checkpoint") or {}
            pending_resume_ids = checkpoint.get("pending_resume_message_ids")
            assert isinstance(pending_resume_ids, list) and len(pending_resume_ids) == 1
            old_marker_id = pending_resume_ids[0]
            old_marker = await db.get(Message, old_marker_id)
            assert old_marker is not None
            old_marker_content = copy.deepcopy(old_marker.content)

        assert await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=old_work.id,
            worker_id=old_worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
        )
        await db.refresh(old_work)

        new_work = await _create_claimed_reply_work(db, worker_id=new_worker_id)
        subscription, tool_call, queued_result, queued_body = await _submit_terminal_watch_subscription(
            db,
            tool_call_id="terminal-auto-watch-cached-subscription",
        )
        assert subscription.id is not None
        assert subscription.status == BackgroundTaskStatus.SUCCEEDED
        child_content = (subscription.result or {}).get("content") or {}
        assert child_content["event"] == "process_exit"
        assert child_content["condition"] == "output_contains"
        assert child_content["matched"] is False
        assert child_content["output"] == ""

        auto_tasks = await auto_watch_tasks()
        assert len(auto_tasks) == 1
        assert auto_tasks[0].id == auto_task_id
        assert await active_auto_watch() is None
        await db.refresh(auto_task)
        assert auto_task.result == auto_result_snapshot

        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[tool_call],
            tool_results=[queued_result],
            response_id="terminal-auto-watch-cached-subscription-response",
        )
        assert wait_response is not None
        subscription_pending = next(pending for pending in wait_response["pending_tools"] if pending["task_id"] == subscription.id)
        assert subscription_pending["tool_result_message_id"] == queued_result.id
        assert await external_work_crud.suspend_claimed(
            db,
            work=new_work,
            worker_id=new_worker_id,
            pending_tools=wait_response["pending_tools"],
        )

        await db.refresh(auto_task)
        assert await route_terminal_auto_watch_reply(db, auto_task)
        await db.refresh(new_work)
        await db.refresh(old_work)
        assert new_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        if old_result_consumed:
            assert old_work.status == SessionReplyWorkStatus.SUCCEEDED
        else:
            assert old_work.status == SessionReplyWorkStatus.READY_FOR_LLM

        claimed_ids: set[int] = set()
        new_resume_worker_id: str | None = None
        expected_claims = 1 if old_result_consumed else 2
        for claim_index in range(expected_claims):
            resume_worker_id = f"terminal-auto-watch-cached-resume-{claim_index}"
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=resume_worker_id,
                lease_seconds=300,
            )
            assert claimed_work is not None
            assert claimed_work.id in {old_work.id, new_work.id}
            assert claimed_work.id not in claimed_ids
            assert claimed_work.id is not None
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (claimed_work.id, resume_worker_id)
            assert await prepare_terminal_auto_watch_resume(db, claimed_work, resume_worker_id)
            checkpoint = (claimed_work.execution_state or {}).get("dispatcher_checkpoint") or {}
            pending_resume_ids = checkpoint.get("pending_resume_message_ids")
            assert isinstance(pending_resume_ids, list) and len(pending_resume_ids) == 1
            marker = await db.get(Message, pending_resume_ids[0])
            assert marker is not None
            marker_payload = json.loads(marker.content or "{}")
            assert marker_payload["task"]["event"] == "process_exit"
            if claimed_work.id == new_work.id:
                new_resume_worker_id = resume_worker_id
                assert marker_payload["task_id"] == subscription.id
                assert marker_payload["original_tool_result_message_id"] == queued_result.id
                assert marker_payload["task"]["condition"] == "output_contains"
                assert marker_payload["task"]["matched"] is False
            else:
                assert old_result_consumed is False
                old_marker_id = marker.id
                old_marker_content = copy.deepcopy(marker.content)
                assert marker_payload["task_id"] == auto_task_id
                assert await session_reply_work_item_crud.mark_terminal(
                    db,
                    work_id=claimed_work.id,
                    worker_id=resume_worker_id,
                    status=SessionReplyWorkStatus.SUCCEEDED,
                )
            claimed_ids.add(claimed_work.id)

        assert claimed_ids == ({new_work.id} if old_result_consumed else {old_work.id, new_work.id})
        assert new_resume_worker_id is not None
        assert old_marker_id is not None
        assert old_marker_content is not None

        markers_before_repeat = await result_markers()
        assert len(markers_before_repeat) == 2
        assert {json.loads(marker.content or "{}")["task_id"] for marker in markers_before_repeat} == {
            auto_task_id,
            subscription.id,
        }
        ready_ids_before_repeat = await work_ready_ids()
        await db.refresh(auto_task)
        assert await route_terminal_auto_watch_reply(db, auto_task)
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (new_work.id, new_resume_worker_id)
        assert await prepare_terminal_auto_watch_resume(db, new_work, new_resume_worker_id)
        markers_after_repeat = await result_markers()
        assert [marker.id for marker in markers_after_repeat] == [marker.id for marker in markers_before_repeat]
        assert await work_ready_ids() == ready_ids_before_repeat

        await db.refresh(auto_task)
        assert auto_task.result == auto_result_snapshot
        deliveries = auto_task.extra.get("terminal_watch_deliveries")
        assert isinstance(deliveries, dict)
        assert set(deliveries) == {str(old_work.id), str(new_work.id)}
        assert all(item["consumed"] and item["routed"] for item in deliveries.values())
        assert auto_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        old_marker = await db.get(Message, old_marker_id)
        assert old_marker is not None
        assert old_marker.content == old_marker_content
        await db.refresh(subscription)
        assert subscription.status == BackgroundTaskStatus.SUCCEEDED
        queued_row = await db.get(Message, queued_result.id)
        assert queued_row is not None
        assert InternalMessage.model_validate_json(queued_row.content or "{}").content == queued_body
        assert await active_auto_watch() is None
        auto_tasks = await auto_watch_tasks()
        assert len(auto_tasks) == 1
        assert auto_tasks[0].id == auto_task_id


@pytest.mark.asyncio
async def test_terminal_auto_watch_two_timeout_routes_keep_one_work_and_only_latest_marker(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    worker_id = "terminal-auto-watch-two-timeout-worker"
    first_timeout = {"event": "timeout", "eof": False, "next_offset": 0}
    second_timeout = {"event": "timeout", "eof": False, "next_offset": 0}

    async with terminal_auto_watch_database() as db:
        work, first_task = await _create_claimed_auto_watch(db, worker_id=worker_id)
        await _complete_auto_watch(
            db,
            task=first_task,
            worker_id=worker_id,
            content=first_timeout,
        )
        first_result = copy.deepcopy(first_task.result)
        assert await route_terminal_auto_watch_reply(db, first_task)
        persisted_work = await db.get(SessionReplyWorkItem, work.id)
        assert persisted_work is not None
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING

        second_task_id = first_task.extra.get("next_auto_watch_task_id")
        assert isinstance(second_task_id, int)
        second_task = await db.get(BackgroundTask, second_task_id)
        assert second_task is not None
        await _complete_auto_watch(
            db,
            task=second_task,
            worker_id=worker_id,
            content=second_timeout,
        )
        assert await route_terminal_auto_watch_reply(db, second_task)

        await db.refresh(first_task)
        await db.refresh(second_task)
        await db.refresh(persisted_work)
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING
        assert first_task.result == first_result
        assert first_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        assert second_task.reply_status == BackgroundTaskReplyStatus.PENDING
        pending_timeout_replies = [
            candidate
            for candidate in await _terminal_watch_tasks(db)
            if candidate.status == BackgroundTaskStatus.SUCCEEDED and candidate.reply_status == BackgroundTaskReplyStatus.PENDING and isinstance(candidate.result, dict) and isinstance(candidate.result.get("content"), dict) and candidate.result["content"].get("event") == "timeout"
        ]
        assert [candidate.id for candidate in pending_timeout_replies] == [second_task_id]
        assert len(list((await db.execute(select(SessionReplyWorkItem))).scalars().all())) == 1
        assert (
            list(
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
            == []
        )

        sync_tool_call = InternalToolCall(
            id="terminal-auto-watch-two-timeout-sync-call",
            name=SHELL_TOOL_NAME,
            arguments={"command": "echo terminal-auto-watch", "execution_mode": "interactive"},
        )
        sync_tool_result = await save_tool_response(
            db,
            SESSION_ID,
            UID,
            PROFILE_ID,
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=sync_tool_call.id,
                content=json.dumps(
                    {
                        "status": "succeeded",
                        "terminal_session_id": TERMINAL_SESSION_ID,
                        "session_status": TerminalSessionStatus.RUNNING.value,
                    },
                    ensure_ascii=False,
                ),
            ),
            [],
            [],
        )
        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[sync_tool_call],
            tool_results=[sync_tool_result],
            response_id="terminal-auto-watch-two-timeout-sync-response",
        )
        assert wait_response is not None
        assert len(wait_response["pending_tools"]) == 1
        assert wait_response["pending_tools"][0]["task_id"] != second_task_id
        assert await external_work_crud.suspend_claimed(
            db,
            work=persisted_work,
            worker_id=worker_id,
            pending_tools=wait_response["pending_tools"],
        )
        assert await route_terminal_auto_watch_reply(db, second_task)

        persisted_work = await db.get(SessionReplyWorkItem, work.id)
        assert persisted_work is not None
        await db.refresh(persisted_work)
        assert persisted_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        resume_worker_id = "terminal-auto-watch-two-timeout-resume-worker"
        claimed_work = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=resume_worker_id,
            lease_seconds=300,
        )
        assert claimed_work is not None
        assert claimed_work.id == work.id
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, resume_worker_id)
        assert await prepare_terminal_auto_watch_resume(db, claimed_work, resume_worker_id)

        assert await route_terminal_auto_watch_reply(db, second_task)
        assert await prepare_terminal_auto_watch_resume(db, claimed_work, resume_worker_id)

        result_markers = list(
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
        assert len(result_markers) == 1
        marker = result_markers[0]
        assert marker.role == MessageRole.USER
        marker_payload = json.loads(marker.content or "{}")
        assert marker_payload["task_id"] == second_task_id
        assert marker_payload["task"] == second_timeout
        assert first_task.result == first_result
        assert (
            list(
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
            == []
        )
        assert len(list((await db.execute(select(SessionReplyWorkItem))).scalars().all())) == 1


@pytest.mark.parametrize("old_wait_kind", ["automatic", "subscription"])
@pytest.mark.asyncio
async def test_terminal_auto_watch_timeout_delivery_stays_independent_per_waiting_work(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    old_wait_kind: str,
) -> None:
    old_worker_id = f"terminal-auto-watch-shared-old-{old_wait_kind}"
    new_worker_id = f"terminal-auto-watch-shared-new-{old_wait_kind}"
    observer_worker_id = f"terminal-auto-watch-shared-observer-{old_wait_kind}"
    first_timeout = {"event": "timeout", "eof": False, "next_offset": 0, "observed_at": "2026-10-27T00:00:01"}
    second_timeout = {"event": "timeout", "eof": False, "next_offset": 0, "observed_at": "2026-10-27T00:00:02"}

    async with terminal_auto_watch_database() as db:
        subscriptions = []
        if old_wait_kind == "automatic":
            old_work, old_wait_task = await _create_claimed_auto_watch(db, worker_id=old_worker_id)
            old_tool_call = InternalToolCall(
                id="terminal-auto-watch-shared-old-sync-call",
                name=SHELL_TOOL_NAME,
                arguments={"command": "echo terminal-auto-watch-shared-old", "execution_mode": "interactive"},
            )
            old_body = json.dumps(
                {
                    "status": "succeeded",
                    "terminal_session_id": TERMINAL_SESSION_ID,
                    "session_status": TerminalSessionStatus.RUNNING.value,
                },
                ensure_ascii=False,
            )
            old_tool_result = await save_tool_response(
                db,
                SESSION_ID,
                UID,
                PROFILE_ID,
                InternalMessage(role=MessageRole.TOOL, tool_call_id=old_tool_call.id, content=old_body),
                [],
                [],
            )
            assert old_tool_result.id is not None
        else:
            old_work = await _create_claimed_reply_work(db, worker_id=old_worker_id)
            old_wait_task, old_tool_call, old_tool_result, old_body = await _submit_terminal_watch_subscription(
                db,
                tool_call_id="terminal-auto-watch-shared-old-subscription",
            )
            subscriptions.append(old_wait_task)

        old_wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[old_tool_call],
            tool_results=[old_tool_result],
            response_id=f"terminal-auto-watch-shared-old-{old_wait_kind}-response",
        )
        assert old_wait_response is not None
        assert old_wait_response["status"] == "waiting_external_work"
        assert len(old_wait_response["pending_tools"]) == 1
        old_pending_tool = old_wait_response["pending_tools"][0]
        assert old_pending_tool["task_id"] == old_wait_task.id
        assert old_pending_tool["terminal_watch"] is True
        assert await external_work_crud.suspend_claimed(
            db,
            work=old_work,
            worker_id=old_worker_id,
            pending_tools=old_wait_response["pending_tools"],
        )

        new_work = await _create_claimed_reply_work(db, worker_id=new_worker_id)
        new_subscription, new_tool_call, new_tool_result, new_body = await _submit_terminal_watch_subscription(
            db,
            tool_call_id="terminal-auto-watch-shared-new-subscription",
        )
        subscriptions.append(new_subscription)
        new_wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[new_tool_call],
            tool_results=[new_tool_result],
            response_id="terminal-auto-watch-shared-new-subscription-response",
        )
        assert new_wait_response is not None
        assert len(new_wait_response["pending_tools"]) == 1
        assert new_wait_response["pending_tools"][0]["task_id"] == new_subscription.id
        assert await external_work_crud.suspend_claimed(
            db,
            work=new_work,
            worker_id=new_worker_id,
            pending_tools=new_wait_response["pending_tools"],
        )

        assert old_work.id is not None and new_work.id is not None
        works = {old_work.id: old_work, new_work.id: new_work}
        await db.refresh(old_work)
        await db.refresh(new_work)
        assert all(work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK for work in works.values())
        assert all(work.merged_into_id is None for work in works.values())

        active_auto = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
            automatic_only=True,
        )
        assert active_auto is not None and active_auto.id is not None

        await _complete_auto_watch(db, task=active_auto, worker_id=observer_worker_id, content=first_timeout)
        first_task = active_auto
        first_result = copy.deepcopy(first_task.result)
        second_task_id = first_task.extra.get("next_auto_watch_task_id")
        assert isinstance(second_task_id, int)
        second_task = await db.get(BackgroundTask, second_task_id)
        assert second_task is not None and second_task.status == BackgroundTaskStatus.PENDING
        await _complete_auto_watch(db, task=second_task, worker_id=observer_worker_id, content=second_timeout)
        await db.refresh(first_task)
        await db.refresh(second_task)
        assert first_task.status == BackgroundTaskStatus.SUCCEEDED
        assert first_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        assert first_task.result == first_result
        assert second_task.status == BackgroundTaskStatus.SUCCEEDED
        assert second_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert second_task.result["content"] == second_timeout
        deliveries = second_task.extra.get("terminal_watch_deliveries")
        assert isinstance(deliveries, dict)
        assert set(deliveries) == {str(work_id) for work_id in works}
        assert all(not item["consumed"] and not item["routed"] for item in deliveries.values())

        async def result_markers() -> list[Message]:
            result = await db.execute(select(Message).where(Message.session_id == SESSION_ID, Message.type == MessageType.BACKGROUND_TASK_RESULT))
            return list(result.scalars().all())

        async def ready_work_ids() -> list[int]:
            result = await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID))
            events = result.scalars().all()
            return [event.event["work_id"] for event in events if event.event.get("type") == "work_ready"]

        assert await route_terminal_auto_watch_reply(db, second_task)
        for work in works.values():
            await db.refresh(work)
        assert all(work.status == SessionReplyWorkStatus.READY_FOR_LLM for work in works.values())
        assert second_task.reply_status == BackgroundTaskReplyStatus.PENDING
        ready_ids = await ready_work_ids()
        assert sorted(ready_ids) == sorted(works)
        assert len(ready_ids) == 2
        summary_result = await db.execute(
            select(SessionReplyWorkItem.id).where(
                SessionReplyWorkItem.session_id == SESSION_ID,
                SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            )
        )
        summary_ids = list(summary_result.scalars().all())
        assert summary_ids == []
        for work in works.values():
            pending = await background_task_crud.list_pending_terminal_watch_results(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                origin_work_id=work.id,
                origin_work_identity=work.dedupe_key,
            )
            assert [task.id for task in pending] == [second_task_id]
        assert await route_terminal_auto_watch_reply(db, second_task)

        claim_order: list[int] = []
        shared_marker_id: int | None = None
        last_claimed_work = None
        last_claim_worker_id = ""
        for claim_index in range(2):
            claim_worker_id = f"terminal-auto-watch-shared-resume-{old_wait_kind}-{claim_index}"
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=claim_worker_id,
                lease_seconds=300,
            )
            assert claimed_work is not None and claimed_work.id in works
            assert claimed_work.id not in claim_order
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (claimed_work.id, claim_worker_id)
            assert await prepare_terminal_auto_watch_resume(db, claimed_work, claim_worker_id)
            claim_order.append(claimed_work.id)
            last_claimed_work = claimed_work
            last_claim_worker_id = claim_worker_id
            markers = await result_markers()
            assert len(markers) == 1
            marker_id = markers[0].id
            assert marker_id is not None
            marker_payload = json.loads(markers[0].content or "{}")
            assert marker_payload["task_id"] == second_task_id
            assert marker_payload["task"] == second_timeout
            if shared_marker_id is None:
                shared_marker_id = marker_id
            else:
                assert marker_id == shared_marker_id
            await db.refresh(claimed_work)
            checkpoint = (claimed_work.execution_state or {}).get("dispatcher_checkpoint") or {}
            assert checkpoint["pending_resume_message_ids"] == [shared_marker_id]
            await db.refresh(second_task)
            deliveries = second_task.extra["terminal_watch_deliveries"]
            assert deliveries[str(claimed_work.id)]["consumed"] is True
            if claim_index == 0:
                other_work_id = next(work_id for work_id in works if work_id != claimed_work.id)
                assert deliveries[str(other_work_id)]["consumed"] is False
                assert second_task.reply_status == BackgroundTaskReplyStatus.PENDING
                pending_other = await background_task_crud.list_pending_terminal_watch_results(
                    db,
                    uid=UID,
                    session_id=SESSION_ID,
                    profile_id=PROFILE_ID,
                    origin_work_id=other_work_id,
                    origin_work_identity=works[other_work_id].dedupe_key,
                )
                assert [task.id for task in pending_other] == [second_task_id]
                assert await session_reply_work_item_crud.mark_terminal(
                    db,
                    work_id=claimed_work.id,
                    worker_id=claim_worker_id,
                    status=SessionReplyWorkStatus.SUCCEEDED,
                )
            else:
                assert second_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
                assert all(item["consumed"] for item in deliveries.values())

        assert len(claim_order) == 2
        assert set(claim_order) == set(works)
        assert last_claimed_work is not None
        assert await route_terminal_auto_watch_reply(db, second_task)
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (last_claimed_work.id, last_claim_worker_id)
        assert await prepare_terminal_auto_watch_resume(db, last_claimed_work, last_claim_worker_id)
        markers = await result_markers()
        assert [marker.id for marker in markers] == [shared_marker_id]
        final_ready_ids = await ready_work_ids()
        assert sorted(final_ready_ids) == sorted(works)
        assert len(final_ready_ids) == 2
        first_work = works[claim_order[0]]
        second_work = works[claim_order[1]]
        await db.refresh(first_work)
        await db.refresh(second_work)
        assert first_work.status == SessionReplyWorkStatus.SUCCEEDED
        assert second_work.status == SessionReplyWorkStatus.RUNNING
        await db.refresh(second_task)
        assert second_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED

        for subscription in subscriptions:
            await db.refresh(subscription)
            assert subscription.status == BackgroundTaskStatus.PENDING
        for tool_result, body in ((old_tool_result, old_body), (new_tool_result, new_body)):
            stored_result = await db.get(Message, tool_result.id)
            assert stored_result is not None
            assert InternalMessage.model_validate_json(stored_result.content or "{}").content == body

        active_auto = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
            automatic_only=True,
        )
        assert active_auto is not None and active_auto.status == BackgroundTaskStatus.PENDING
        active_auto_tasks = [task for task in await _terminal_watch_tasks(db) if isinstance(task.extra, dict) and task.extra.get("auto_terminal_watch") is True and task.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}]
        assert [task.id for task in active_auto_tasks] == [active_auto.id]


@pytest.mark.parametrize("event", ["process_exit", "observation_error"])
@pytest.mark.asyncio
async def test_terminal_auto_watch_shared_subscriptions_deliver_independent_results(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    event: str,
) -> None:
    observer_worker_id = f"terminal-auto-watch-shared-{event}-observer"
    content = {
        "event": event,
        "eof": event == "process_exit",
        "next_offset": 0,
        "terminal_session_id": TERMINAL_SESSION_ID,
    }
    if event == "process_exit":
        content.update(
            {
                "exit_code": 0,
                "session_status": TerminalSessionStatus.EXITED.value,
            }
        )
    else:
        content.update(
            {
                "error": "terminal output read failed",
                "matched_watch_task_ids": [],
            }
        )

    waits: list[tuple[SessionReplyWorkItem, BackgroundTask, Message, str]] = []

    async with terminal_auto_watch_database() as db:

        async def result_markers() -> list[Message]:
            return list((await db.execute(select(Message).where(Message.session_id == SESSION_ID, Message.type == MessageType.BACKGROUND_TASK_RESULT))).scalars().all())

        async def session_event_ids() -> list[int | None]:
            return [event_row.id for event_row in (await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID))).scalars().all()]

        async def summary_items() -> list[SessionReplyWorkItem]:
            return list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == SESSION_ID, SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY))).scalars().all())

        async def active_auto_watch() -> BackgroundTask | None:
            return await background_task_crud.get_active_terminal_watch(db, UID, SESSION_ID, TERMINAL_SESSION_ID, automatic_only=True)

        for index in range(2):
            worker_id = f"terminal-auto-watch-shared-{event}-work-{index}"
            work = await _create_claimed_reply_work(db, worker_id=worker_id)
            assert work.id is not None
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
            subscription, tool_call, queued_result, queued_body = await _submit_terminal_watch_subscription(
                db,
                tool_call_id=f"terminal-auto-watch-shared-{event}-subscription-{index}",
            )
            assert subscription.id is not None

            wait_response = await build_terminal_wait_response(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                tool_calls=[tool_call],
                tool_results=[queued_result],
                response_id=f"terminal-auto-watch-shared-{event}-response-{index}",
            )
            assert wait_response is not None
            pending_tool = wait_response["pending_tools"][0]
            assert pending_tool["task_id"] == subscription.id
            assert pending_tool["terminal_watch"] is True
            assert await external_work_crud.suspend_claimed(
                db,
                work=work,
                worker_id=worker_id,
                pending_tools=wait_response["pending_tools"],
            )

            await db.refresh(work)
            assert work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            waits.append((work, subscription, queued_result, queued_body))

        active_auto_tasks = [task for task in await _terminal_watch_tasks(db) if isinstance(task.extra, dict) and task.extra.get("auto_terminal_watch") is True and task.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}]
        assert len(active_auto_tasks) == 1
        auto_task = active_auto_tasks[0]
        assert auto_task.id is not None

        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        if event == "process_exit":
            terminal_session.status = TerminalSessionStatus.EXITED
            terminal_session.exit_code = 0
            db.add(terminal_session)
            await db.commit()
            assert terminal_session.status == TerminalSessionStatus.EXITED and terminal_session.exit_code == 0
        else:
            assert terminal_session.status == TerminalSessionStatus.RUNNING

        await _complete_auto_watch(
            db,
            task=auto_task,
            worker_id=observer_worker_id,
            content=content,
        )
        await db.refresh(auto_task)
        assert auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert auto_task.result is not None
        assert set(auto_task.result["content"]["completed_watch_task_ids"]) == {subscription.id for _work, subscription, _queued, _body in waits}
        assert "next_auto_watch_task_id" not in auto_task.extra

        assert await route_terminal_auto_watch_reply(db, auto_task)
        for work, _subscription, _queued_result, _queued_body in waits:
            await db.refresh(work)
            assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert auto_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert await summary_items() == []

        wait_by_work_id = {work.id: (work, subscription, queued_result, queued_body) for work, subscription, queued_result, queued_body in waits}

        claimed_ids: list[int] = []
        last_work: SessionReplyWorkItem | None = None
        last_work_id: int | None = None
        last_worker_id = ""
        for index in range(2):
            worker_id = f"terminal-auto-watch-shared-{event}-resume-{index}"
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
            )
            assert claimed_work is not None
            assert claimed_work.id in wait_by_work_id
            assert claimed_work.id not in claimed_ids
            assert claimed_work.id is not None
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (claimed_work.id, worker_id)
            assert await prepare_terminal_auto_watch_resume(db, claimed_work, worker_id)

            _work, claimed_subscription, claimed_queued_result, _claimed_queued_body = wait_by_work_id[claimed_work.id]
            state = claimed_work.execution_state or {}
            checkpoint = state.get("dispatcher_checkpoint") or {}
            pending_resume_ids = checkpoint.get("pending_resume_message_ids")
            assert isinstance(pending_resume_ids, list)
            assert len(pending_resume_ids) == 1
            marker = await db.get(Message, pending_resume_ids[0])
            assert marker is not None
            assert marker.type == MessageType.BACKGROUND_TASK_RESULT
            marker_payload = json.loads(marker.content or "{}")
            assert marker_payload["task_id"] == claimed_subscription.id
            assert marker_payload["original_tool_result_message_id"] == claimed_queued_result.id
            assert marker_payload["task"]["event"] == event
            assert marker_payload["task"]["condition"] == "output_contains"
            assert marker_payload["task"]["matched"] is False
            assert marker.id is not None
            claimed_ids.append(claimed_work.id)
            last_work = claimed_work
            last_work_id = claimed_work.id
            last_worker_id = worker_id

            await db.refresh(auto_task)
            deliveries = auto_task.extra["terminal_watch_deliveries"]
            assert deliveries[str(claimed_work.id)]["consumed"] is True
            if index == 0:
                other_work_id = next(work_id for work_id in wait_by_work_id if work_id != claimed_work.id)
                assert deliveries[str(other_work_id)]["consumed"] is False
                assert auto_task.reply_status == BackgroundTaskReplyStatus.PENDING
                other_pending = await background_task_crud.list_pending_terminal_watch_results(
                    db,
                    uid=UID,
                    session_id=SESSION_ID,
                    profile_id=PROFILE_ID,
                    origin_work_id=other_work_id,
                    origin_work_identity=wait_by_work_id[other_work_id][0].dedupe_key,
                )
                assert [task.id for task in other_pending] == [auto_task.id]
                assert await session_reply_work_item_crud.mark_terminal(
                    db,
                    work_id=claimed_work.id,
                    worker_id=worker_id,
                    status=SessionReplyWorkStatus.SUCCEEDED,
                )
            else:
                assert auto_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
                assert all(delivery["consumed"] for delivery in deliveries.values())

        assert set(claimed_ids) == set(wait_by_work_id)
        events_before_repeat = await session_event_ids()
        markers_before_repeat = await result_markers()
        assert len(markers_before_repeat) == 2
        assert len({marker.id for marker in markers_before_repeat}) == 2
        assert {json.loads(marker.content or "{}")["task_id"] for marker in markers_before_repeat} == {subscription.id for _work, subscription, _queued, _body in waits}

        assert last_work is not None
        assert last_work_id is not None
        await db.refresh(auto_task)
        assert await route_terminal_auto_watch_reply(db, auto_task)
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (last_work_id, last_worker_id)
        assert await prepare_terminal_auto_watch_resume(db, last_work, last_worker_id)

        markers_after_repeat = await result_markers()
        events_after_repeat = await session_event_ids()
        assert {marker.id for marker in markers_after_repeat} == {marker.id for marker in markers_before_repeat}
        assert set(events_after_repeat) == set(events_before_repeat)
        assert await summary_items() == []

        await db.refresh(auto_task)
        assert auto_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        assert all(item["consumed"] and item["routed"] for item in auto_task.extra["terminal_watch_deliveries"].values())
        for _work, subscription, queued_result, queued_body in waits:
            await db.refresh(subscription)
            assert subscription.status == BackgroundTaskStatus.SUCCEEDED
            stored_queued_result = await db.get(Message, queued_result.id)
            assert stored_queued_result is not None
            assert InternalMessage.model_validate_json(stored_queued_result.content or "{}").content == queued_body
        assert await active_auto_watch() is None


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
            None,
            id="error-stops",
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

        next_task = await create_terminal_auto_watch(
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
            "request_context": [],
            "purpose": None,
        }
        assert "observation_started_at" not in next_task.extra
        assert next_task.created_at != task.created_at
        assert next_task.created_at.isoformat() != observed_content["observed_at"]
        assert next_task.started_at is None
        assert next_task.finished_at is None

        await db.refresh(task)
        assert task.extra.get("next_auto_watch_task_id") == next_task.id
        tasks = await _terminal_watch_tasks(db)
        assert len(tasks) == 2


@pytest.mark.asyncio
async def test_terminal_auto_watch_completion_renews_before_reply_is_consumed(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    worker_id = "terminal-auto-watch-coordinator-worker"
    content = {
        "event": "timeout",
        "eof": False,
        "next_offset": 0,
        "watch_states": {"17": {"cursor": 0, "suffix": "AB"}},
    }

    async with terminal_auto_watch_database() as db:
        origin_work = await _create_claimed_reply_work(db, worker_id=worker_id)
        task = await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        assert task is not None
        assert task.id is not None
        task_id = task.id
        assert task.extra["origin_work_id"] == origin_work.id
        assert task.extra["origin_work_identity"] == origin_work.dedupe_key

        claimed = await background_task_crud.try_claim(
            db,
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert claimed is not None
        assert await complete_terminal_watch_round(
            db,
            task=task,
            worker_id=worker_id,
            content=content,
        )

        await db.refresh(task)
        assert task.status == BackgroundTaskStatus.SUCCEEDED
        assert task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert task.result is not None
        assert task.result["content"] == content
        next_task_id = task.extra.get("next_auto_watch_task_id")
        assert isinstance(next_task_id, int)
        next_task = await db.get(BackgroundTask, next_task_id)
        assert next_task is not None
        assert next_task.status == BackgroundTaskStatus.PENDING
        assert next_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert next_task.extra["origin_work_id"] == origin_work.id
        assert next_task.extra["origin_work_identity"] == origin_work.dedupe_key
        assert next_task.extra["watch_states"] == content["watch_states"]
        assert "observation_started_at" not in next_task.extra
        assert next_task.created_at != task.created_at
        assert next_task.started_at is None
        assert next_task.finished_at is None
        pending_observations = [candidate for candidate in await _terminal_watch_tasks(db) if candidate.id != task_id and candidate.status == BackgroundTaskStatus.PENDING]
        assert [candidate.id for candidate in pending_observations] == [next_task_id]

        repeated_next = await create_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            previous_task_id=task_id,
        )
        assert repeated_next is not None
        assert repeated_next.id == next_task_id
        assert len(await _terminal_watch_tasks(db)) == 2


@pytest.mark.asyncio
async def test_terminal_auto_watch_completion_supersedes_only_previous_timeout_reply(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    worker_id = "terminal-auto-watch-two-round-worker"
    first_content = {
        "event": "timeout",
        "eof": False,
        "next_offset": 0,
        "watch_states": {"17": {"cursor": 0, "suffix": "AB"}},
    }
    second_content = {
        "event": "timeout",
        "eof": False,
        "next_offset": 0,
        "watch_states": {"17": {"cursor": 0, "suffix": "ABCD"}},
    }

    async with terminal_auto_watch_database() as db:
        origin_work = await _create_claimed_reply_work(db, worker_id=worker_id)
        first_task = await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        assert first_task is not None
        assert first_task.id is not None
        first_task_id = first_task.id
        assert (
            await background_task_crud.try_claim(
                db,
                task_id=first_task_id,
                worker_id=worker_id,
                lease_seconds=300,
            )
            is not None
        )
        assert await complete_terminal_watch_round(
            db,
            task=first_task,
            worker_id=worker_id,
            content=first_content,
        )

        await db.refresh(first_task)
        assert first_task.reply_status == BackgroundTaskReplyStatus.PENDING
        first_result = copy.deepcopy(first_task.result)
        second_task_id = first_task.extra.get("next_auto_watch_task_id")
        assert isinstance(second_task_id, int)
        second_task = await db.get(BackgroundTask, second_task_id)
        assert second_task is not None
        assert second_task.status == BackgroundTaskStatus.PENDING
        assert second_task.reply_status == BackgroundTaskReplyStatus.PENDING

        assert (
            await background_task_crud.try_claim(
                db,
                task_id=second_task_id,
                worker_id=worker_id,
                lease_seconds=300,
            )
            is not None
        )
        assert await complete_terminal_watch_round(
            db,
            task=second_task,
            worker_id=worker_id,
            content=second_content,
        )

        await db.refresh(first_task)
        await db.refresh(second_task)
        assert first_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        assert first_task.result == first_result
        assert second_task.status == BackgroundTaskStatus.SUCCEEDED
        assert second_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert second_task.result is not None
        assert second_task.result["content"] == second_content

        pending_timeout_replies = [
            candidate for candidate in await _terminal_watch_tasks(db) if candidate.status == BackgroundTaskStatus.SUCCEEDED and candidate.reply_status == BackgroundTaskReplyStatus.PENDING and candidate.extra.get("origin_work_id") == origin_work.id and candidate.extra.get("terminal_session_id") == TERMINAL_SESSION_ID
        ]
        assert [candidate.id for candidate in pending_timeout_replies] == [second_task_id]
        active_observations = [candidate for candidate in await _terminal_watch_tasks(db) if candidate.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING} and candidate.extra.get("auto_terminal_watch") is True]
        assert len(active_observations) == 1
        assert active_observations[0].reply_status == BackgroundTaskReplyStatus.PENDING

        persisted_work = await db.get(SessionReplyWorkItem, origin_work.id)
        assert persisted_work is not None
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING


@pytest.mark.asyncio
async def test_terminal_auto_watch_observation_error_completes_subscription_without_retry(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    worker_id = "terminal-auto-watch-observation-error-worker"
    content = {
        "event": "observation_error",
        "eof": False,
        "next_offset": 0,
        "error": "terminal output read failed",
        "matched_watch_task_ids": [],
    }

    async with terminal_auto_watch_database() as db:
        origin_work = await _create_claimed_reply_work(db, worker_id=worker_id)
        auto_task = await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        assert auto_task is not None
        assert auto_task.id is not None
        subscription = await create_terminal_watch_subscription(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="terminal-auto-watch-unmatched-subscription",
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 0,
                "condition": "output_contains",
                "text": "never-seen",
            },
            extra={
                "origin_work_id": origin_work.id,
                "origin_work_identity": origin_work.dedupe_key,
            },
        )
        assert subscription is not None
        assert subscription.status == BackgroundTaskStatus.PENDING
        assert subscription.auto_reply is False

        auto_task_id = auto_task.id
        assert (
            await background_task_crud.try_claim(
                db,
                task_id=auto_task_id,
                worker_id=worker_id,
                lease_seconds=300,
            )
            is not None
        )
        assert await complete_terminal_watch_round(
            db,
            task=auto_task,
            worker_id=worker_id,
            content=content,
        )

        await db.refresh(auto_task)
        await db.refresh(subscription)
        assert auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert auto_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert auto_task.result is not None
        assert auto_task.result["content"]["error"] == content["error"]
        assert "next_auto_watch_task_id" not in auto_task.extra
        assert subscription.status == BackgroundTaskStatus.SUCCEEDED
        assert subscription.reply_status == BackgroundTaskReplyStatus.NONE
        assert subscription.result is not None
        assert subscription.result["content"] == {
            "event": "observation_error",
            "eof": False,
            "next_offset": 0,
            "error": content["error"],
            "condition": "output_contains",
            "matched": False,
        }

        tasks = await _terminal_watch_tasks(db)
        assert len([task for task in tasks if task.extra.get("auto_terminal_watch") is True]) == 1
        assert [task for task in tasks if task.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}] == []
        assert (
            await background_task_crud.list_terminal_watch_subscriptions(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                terminal_session_id=TERMINAL_SESSION_ID,
            )
            == []
        )


@pytest.mark.parametrize(
    ("event", "terminal_status", "exit_code", "failure_reason"),
    [
        pytest.param(
            "terminal_error",
            TerminalSessionStatus.FAILED,
            None,
            "terminal process failed",
            id="terminal-error-stops",
        ),
        pytest.param(
            "process_exit",
            TerminalSessionStatus.EXITED,
            7,
            None,
            id="process-exit-stops",
        ),
    ],
)
@pytest.mark.asyncio
async def test_terminal_auto_watch_final_terminal_events_do_not_renew(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    event: str,
    terminal_status: TerminalSessionStatus,
    exit_code: int | None,
    failure_reason: str | None,
) -> None:
    worker_id = f"terminal-auto-watch-{event}-worker"
    content = {
        "event": event,
        "eof": True,
        "next_offset": 0,
        "session_status": terminal_status.value,
        "exit_code": exit_code,
        "failure_reason": failure_reason,
    }

    async with terminal_auto_watch_database() as db:
        origin_work = await _create_claimed_reply_work(db, worker_id=worker_id)
        task = await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        assert task is not None
        assert task.id is not None
        task_id = task.id
        assert (
            await background_task_crud.try_claim(
                db,
                task_id=task_id,
                worker_id=worker_id,
                lease_seconds=300,
            )
            is not None
        )

        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        terminal_session.status = terminal_status
        terminal_session.exit_code = exit_code
        terminal_session.failure_reason = failure_reason
        db.add(terminal_session)
        await db.commit()

        assert await complete_terminal_watch_round(
            db,
            task=task,
            worker_id=worker_id,
            content=content,
        )

        await db.refresh(task)
        persisted_terminal = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert persisted_terminal is not None
        assert persisted_terminal.status == terminal_status
        assert persisted_terminal.exit_code == exit_code
        assert persisted_terminal.failure_reason == failure_reason
        assert task.status == BackgroundTaskStatus.SUCCEEDED
        assert task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert task.result is not None
        assert task.result["content"] == content
        assert "next_auto_watch_task_id" not in task.extra
        assert [candidate for candidate in await _terminal_watch_tasks(db) if candidate.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING} and candidate.extra.get("auto_terminal_watch") is True] == []

        persisted_work = await db.get(SessionReplyWorkItem, origin_work.id)
        assert persisted_work is not None
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING


@pytest.mark.asyncio
async def test_terminal_auto_watch_stop_between_summary_delivery_and_renewal_deletes_terminal_session_and_control_command_index(
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

    async def cleanup_terminal_process_identity(_process_identity: dict | None) -> SimpleNamespace:
        return SimpleNamespace(errors=())

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", terminal_auto_watch_database)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", controlled_send_session_event)

    execution_task = asyncio.create_task(executor_lifecycle_module.execute_session_reply_work(work_id, worker_id))
    try:
        await asyncio.wait_for(event_reached.wait(), timeout=2)
        async with terminal_auto_watch_database() as stop_db:
            monkeypatch.setattr(
                terminal_audit_lifecycle_module,
                "cleanup_terminal_process_identity",
                cleanup_terminal_process_identity,
            )
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
        control_commands = list(
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
    assert terminal_session is None
    assert control_commands == []
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


@pytest.mark.parametrize(
    ("terminal_status", "event", "exit_code", "failure_reason"),
    [
        pytest.param(
            TerminalSessionStatus.EXITED,
            "process_exit",
            0,
            None,
            id="exited-after-timeout",
        ),
        pytest.param(
            TerminalSessionStatus.FAILED,
            "terminal_error",
            None,
            "terminal failed",
            id="failed-after-timeout",
        ),
    ],
)
@pytest.mark.parametrize(
    "closing_before_renewal",
    [False, True],
    ids=["final-before-timeout", "closing-before-renewal"],
)
@pytest.mark.asyncio
async def test_terminal_auto_watch_timeout_snapshot_reaches_final_event_after_terminal_transition(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: TerminalSessionStatus,
    event: str,
    exit_code: int | None,
    failure_reason: str | None,
    closing_before_renewal: bool,
) -> None:
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", terminal_auto_watch_database)
    monkeypatch.setattr(watcher_module, "AsyncSessionLocal", terminal_auto_watch_database)
    timeout_content = {"event": "timeout", "eof": False, "next_offset": 0}
    observer_worker_id = f"terminal-auto-watch-timeout-{event}-observer"
    final_worker_id = f"terminal-auto-watch-timeout-{event}-final"

    async with terminal_auto_watch_database() as db:
        work, timeout_task = await _create_claimed_auto_watch(db, worker_id=observer_worker_id)
        assert work.id is not None
        assert timeout_task.id is not None
        timeout_task_id = timeout_task.id

        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        assert terminal_session.status == TerminalSessionStatus.RUNNING
        assert terminal_session.oldest_output_offset == 0
        assert terminal_session.next_output_offset == 0

        if closing_before_renewal:
            terminal_session.status = TerminalSessionStatus.CLOSING
            terminal_session.exit_code = None
            terminal_session.failure_reason = None
        else:
            terminal_session.status = terminal_status
            terminal_session.exit_code = exit_code
            terminal_session.failure_reason = failure_reason
        db.add(terminal_session)
        await db.commit()

        await _complete_auto_watch(
            db,
            task=timeout_task,
            worker_id=observer_worker_id,
            content=timeout_content,
        )
        await db.refresh(timeout_task)
        timeout_result = copy.deepcopy(timeout_task.result)
        next_task_id = timeout_task.extra.get("next_auto_watch_task_id")
        assert isinstance(next_task_id, int)
        assert timeout_task.status == BackgroundTaskStatus.SUCCEEDED
        assert timeout_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert timeout_task.result is not None
        assert timeout_task.result["content"] == timeout_content

        pending_auto_tasks = [candidate for candidate in await _terminal_watch_tasks(db) if candidate.extra.get("auto_terminal_watch") is True and candidate.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}]
        assert [candidate.id for candidate in pending_auto_tasks] == [next_task_id]
        next_task = pending_auto_tasks[0]
        assert next_task.arguments["after_offset"] == 0
        assert next_task.reply_status == BackgroundTaskReplyStatus.PENDING

        if closing_before_renewal:
            await db.refresh(terminal_session)
            assert terminal_session.status == TerminalSessionStatus.CLOSING
            assert terminal_session.exit_code is None
            assert terminal_session.failure_reason is None
            terminal_session.status = terminal_status
            terminal_session.exit_code = exit_code
            terminal_session.failure_reason = failure_reason
            db.add(terminal_session)
            await db.commit()

    await runner_module.run_background_task(next_task_id, worker_id=final_worker_id)

    async with terminal_auto_watch_database() as db:
        timeout_task = await db.get(BackgroundTask, timeout_task_id)
        final_task = await db.get(BackgroundTask, next_task_id)
        persisted_terminal = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        persisted_work = await db.get(SessionReplyWorkItem, work.id)
        assert timeout_task is not None
        assert final_task is not None
        assert persisted_terminal is not None
        assert persisted_work is not None
        assert persisted_terminal.status == terminal_status
        assert persisted_terminal.exit_code == exit_code
        assert persisted_terminal.failure_reason == failure_reason
        assert persisted_terminal.oldest_output_offset == 0
        assert persisted_terminal.next_output_offset == 0
        assert final_task.status == BackgroundTaskStatus.SUCCEEDED
        assert final_task.result is not None
        assert final_task.extra.get("next_auto_watch_task_id") is None
        final_content = final_task.result["content"]
        assert final_content["event"] == event
        assert final_content["eof"] is True
        assert final_content["terminal_session_id"] == TERMINAL_SESSION_ID
        assert final_content["session_status"] == terminal_status.value
        assert final_content["exit_code"] == exit_code
        assert final_content["failure_reason"] == failure_reason
        assert final_content["output"] == ""
        assert final_content["next_offset"] == 0

        assert await route_terminal_auto_watch_reply(db, final_task)
        await db.refresh(timeout_task)
        await db.refresh(final_task)
        await db.refresh(persisted_work)
        pending_results = await background_task_crud.list_pending_terminal_watch_results(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            origin_work_id=work.id,
            origin_work_identity=work.dedupe_key,
        )
        assert [candidate.id for candidate in pending_results] == [next_task_id]
        assert pending_results[0].result["content"]["event"] == event
        assert timeout_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        assert timeout_task.result == timeout_result
        assert final_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING
        assert (
            await background_task_crud.get_active_terminal_watch(
                db,
                UID,
                SESSION_ID,
                TERMINAL_SESSION_ID,
                automatic_only=True,
            )
            is None
        )
        assert [candidate for candidate in await _terminal_watch_tasks(db) if candidate.extra.get("auto_terminal_watch") is True and candidate.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}] == []


@pytest.mark.asyncio
async def test_terminal_closing_subscription_creates_auto_watch_and_delivers_process_exit(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", terminal_auto_watch_database)
    monkeypatch.setattr(watcher_module, "AsyncSessionLocal", terminal_auto_watch_database)
    worker_id = "terminal-auto-watch-closing-subscription-worker"
    observer_worker_id = "terminal-auto-watch-closing-observer-worker"

    async with terminal_auto_watch_database() as db:
        work = await _create_claimed_reply_work(db, worker_id=worker_id)
        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        terminal_session.status = TerminalSessionStatus.CLOSING
        terminal_session.exit_code = None
        db.add(terminal_session)
        await db.commit()
        assert not [task for task in await _terminal_watch_tasks(db) if task.extra.get("auto_terminal_watch") is True]

        subscription, tool_call, queued_result, queued_body = await _submit_terminal_watch_subscription(
            db,
            tool_call_id="terminal-auto-watch-closing-subscription",
        )
        assert subscription.status == BackgroundTaskStatus.PENDING
        active_auto_tasks = [task for task in await _terminal_watch_tasks(db) if task.extra.get("auto_terminal_watch") is True and task.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}]
        assert len(active_auto_tasks) == 1
        auto_task = active_auto_tasks[0]
        assert auto_task.status == BackgroundTaskStatus.PENDING
        assert auto_task.extra["origin_work_id"] == work.id
        assert auto_task.extra["origin_work_identity"] == work.dedupe_key

        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[tool_call],
            tool_results=[queued_result],
            response_id="terminal-auto-watch-closing-subscription-response",
        )
        assert wait_response is not None
        assert wait_response["status"] == "waiting_external_work"
        assert len(wait_response["pending_tools"]) == 1
        pending_tool = wait_response["pending_tools"][0]
        assert pending_tool["task_id"] == subscription.id
        assert pending_tool["tool_result_message_id"] == queued_result.id
        assert await external_work_crud.suspend_claimed(
            db,
            work=work,
            worker_id=worker_id,
            pending_tools=wait_response["pending_tools"],
        )
        await db.refresh(work)
        assert work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        terminal_session.status = TerminalSessionStatus.EXITED
        terminal_session.exit_code = 0
        db.add(terminal_session)
        await db.commit()
        assert auto_task.id is not None
        auto_task_id = auto_task.id
        assert work.id is not None
        work_id = work.id

    await runner_module.run_background_task(auto_task_id, worker_id=observer_worker_id)

    async with terminal_auto_watch_database() as db:
        auto_task = await db.get(BackgroundTask, auto_task_id)
        subscription = await db.get(BackgroundTask, subscription.id)
        work = await db.get(SessionReplyWorkItem, work_id)
        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert auto_task is not None
        assert subscription is not None
        assert work is not None
        assert terminal_session is not None
        assert terminal_session.status == TerminalSessionStatus.EXITED
        assert terminal_session.exit_code == 0
        assert terminal_session.oldest_output_offset == 0
        assert terminal_session.next_output_offset == 0
        assert auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert auto_task.result is not None
        auto_content = auto_task.result["content"]
        assert auto_content["event"] == "process_exit"
        assert auto_content["eof"] is True
        assert auto_content["output"] == "" and auto_content["next_offset"] == 0
        assert subscription.status == BackgroundTaskStatus.SUCCEEDED and subscription.result is not None
        subscription_content = subscription.result["content"]
        assert subscription_content["event"] == "process_exit"
        assert subscription_content["matched"] is False

        assert await route_terminal_auto_watch_reply(db, auto_task)
        await db.refresh(work)
        assert work.status == SessionReplyWorkStatus.READY_FOR_LLM

        resume_worker_id = "terminal-auto-watch-closing-resume-worker"
        claimed_work = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=resume_worker_id,
            lease_seconds=300,
        )
        assert claimed_work is not None
        assert claimed_work.id == work_id
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, resume_worker_id)
        assert await prepare_terminal_auto_watch_resume(db, claimed_work, resume_worker_id)
        checkpoint = (claimed_work.execution_state or {}).get("dispatcher_checkpoint") or {}
        pending_resume_ids = checkpoint.get("pending_resume_message_ids")
        assert isinstance(pending_resume_ids, list) and len(pending_resume_ids) == 1
        marker = await db.get(Message, pending_resume_ids[0])
        assert marker is not None
        marker_payload = json.loads(marker.content or "{}")
        assert marker_payload["task_id"] == subscription.id
        assert marker_payload["original_tool_result_message_id"] == queued_result.id
        assert marker_payload["task"]["event"] == "process_exit"
        assert marker_payload["task"]["matched"] is False

        queued_row = await db.get(Message, queued_result.id)
        assert queued_row is not None
        assert InternalMessage.model_validate_json(queued_row.content or "{}").content == queued_body
        assert not [task for task in await _terminal_watch_tasks(db) if task.extra.get("auto_terminal_watch") is True and task.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}]
        summary_items = [item for item in (await db.execute(select(SessionReplyWorkItem))).scalars().all() if item.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY]
        assert summary_items == []


@pytest.mark.parametrize("route_before_checkpoint", [True, False])
@pytest.mark.asyncio
async def test_terminal_auto_watch_route_preserves_reclaimed_work_checkpoint(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    route_before_checkpoint: bool,
) -> None:
    old_worker_id = "terminal-auto-watch-expired-worker"
    new_worker_id = "terminal-auto-watch-reclaimed-worker"
    observer_worker_id = "terminal-auto-watch-observer-worker"
    observation_error = {
        "event": "observation_error",
        "eof": False,
        "next_offset": 0,
        "terminal_session_id": TERMINAL_SESSION_ID,
        "error": "checkpoint race observation failed",
        "matched_watch_task_ids": [],
    }
    checkpoint = {
        "messages": [],
        "test_marker": f"reclaimed-checkpoint-{route_before_checkpoint}",
    }

    async with terminal_auto_watch_database() as setup_db:
        old_work, auto_task = await _create_claimed_auto_watch(setup_db, worker_id=old_worker_id)
        assert old_work.id is not None
        assert auto_task.id is not None
        work_id = old_work.id
        auto_task_id = auto_task.id
        old_identity = old_work.dedupe_key
        assert await session_reply_work_item_crud.update_claimed(
            setup_db,
            work_id=work_id,
            worker_id=old_worker_id,
            values={"lock_until": 0},
        )

    async with terminal_auto_watch_database() as worker_db:
        stale_work = await worker_db.get(SessionReplyWorkItem, work_id)
        assert stale_work is not None
        assert stale_work.status == SessionReplyWorkStatus.RUNNING
        assert stale_work.locked_by == old_worker_id
        assert stale_work.lock_until == 0
        await worker_db.commit()

        async with terminal_auto_watch_database() as recovery_db:
            recovered_count, terminal_claims = await session_reply_work_item_crud.recover_expired(recovery_db)
            assert recovered_count == 1
            assert terminal_claims == []
            reclaimed_work = await session_reply_work_item_crud.claim_next(
                recovery_db,
                worker_id=new_worker_id,
                lease_seconds=300,
            )
            assert reclaimed_work is not None
            assert reclaimed_work.id == work_id
            assert reclaimed_work.locked_by == new_worker_id
            reclaimed_lock_until = reclaimed_work.lock_until
            assert reclaimed_lock_until is not None

        async with terminal_auto_watch_database() as observer_db:
            observed_auto = await observer_db.get(BackgroundTask, auto_task_id)
            assert observed_auto is not None
            await _complete_auto_watch(
                observer_db,
                task=observed_auto,
                worker_id=observer_worker_id,
                content=observation_error,
            )
            await observer_db.refresh(observed_auto)
            assert observed_auto.result is not None
            assert observed_auto.result["content"] == observation_error
            assert observed_auto.extra.get("next_auto_watch_task_id") is None

        worker_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, new_worker_id)
        if route_before_checkpoint:
            async with terminal_auto_watch_database() as routing_db:
                routing_task = await routing_db.get(BackgroundTask, auto_task_id)
                assert routing_task is not None
                assert await route_terminal_auto_watch_reply(routing_db, routing_task)

        await executor_interactive_module._save_interactive_work_execution_checkpoint(
            worker_db,
            copy.deepcopy(checkpoint),
            work=stale_work,
            worker_id=new_worker_id,
        )

        if not route_before_checkpoint:
            async with terminal_auto_watch_database() as routing_db:
                routing_task = await routing_db.get(BackgroundTask, auto_task_id)
                assert routing_task is not None
                assert await route_terminal_auto_watch_reply(routing_db, routing_task)

    async with terminal_auto_watch_database() as verify_db:
        persisted_work = await verify_db.get(SessionReplyWorkItem, work_id)
        persisted_auto = await verify_db.get(BackgroundTask, auto_task_id)
        assert persisted_work is not None
        assert persisted_auto is not None
        deliveries = persisted_auto.extra.get("terminal_watch_deliveries")
        assert isinstance(deliveries, dict)
        assert deliveries[str(work_id)] == {
            "identity": old_identity,
            "consumed": False,
            "routed": True,
        }
        state = persisted_work.execution_state or {}
        assert state["dispatcher_checkpoint"] == checkpoint
        assert state["terminal_auto_resume"] is True
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING
        assert persisted_work.locked_by == new_worker_id
        assert persisted_work.lock_until == reclaimed_lock_until

        stale_update = {
            "dispatcher_checkpoint": {
                "messages": [],
                "test_marker": "old-worker-must-not-overwrite",
            }
        }
        assert not await session_reply_work_item_crud.update_claimed(
            verify_db,
            work_id=work_id,
            worker_id=old_worker_id,
            values={"execution_state": stale_update},
        )
        await verify_db.refresh(persisted_work)
        final_state = persisted_work.execution_state or {}
        assert final_state["dispatcher_checkpoint"] == checkpoint
        assert final_state["terminal_auto_resume"] is True
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING
        assert persisted_work.locked_by == new_worker_id
        assert persisted_work.lock_until == reclaimed_lock_until


@pytest.mark.parametrize(
    ("route_before_delivery", "delivery_failed", "same_work"),
    [
        pytest.param(True, False, False, id="route-before-stop-delivery"),
        pytest.param(False, False, False, id="route-after-stop-delivery"),
        pytest.param(False, True, False, id="stop-delivery-failure-keeps-watch-fallback"),
        pytest.param(False, False, True, id="origin-work-closes-itself"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_close_reply_delivery_cancels_origin_or_preserves_watch_fallback(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    route_before_delivery: bool,
    delivery_failed: bool,
    same_work: bool,
) -> None:
    origin_worker_id = "terminal-close-origin-worker"
    stop_worker_id = "terminal-close-stop-worker"
    tool_call = InternalToolCall(
        id="terminal-close-origin-shell-call",
        name=SHELL_TOOL_NAME,
        arguments={
            "command": "echo terminal-close-origin",
            "execution_mode": "interactive",
        },
    )
    receipt_payload = {
        "status": "succeeded",
        "terminal_session_id": TERMINAL_SESSION_ID,
        "session_status": TerminalSessionStatus.RUNNING.value,
    }

    async with terminal_auto_watch_database() as db:
        origin_work, auto_task = await _create_claimed_auto_watch(db, worker_id=origin_worker_id)
        assert origin_work.id is not None
        assert auto_task.id is not None
        origin_work_id = origin_work.id
        auto_task_id = auto_task.id

        if not same_work:
            tool_result = await save_tool_response(
                db,
                SESSION_ID,
                UID,
                PROFILE_ID,
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tool_call.id,
                    content=json.dumps(receipt_payload, ensure_ascii=False),
                ),
                [],
                [],
            )
            assert tool_result.id is not None
            wait_response = await build_terminal_wait_response(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                tool_calls=[tool_call],
                tool_results=[tool_result],
                response_id="terminal-close-origin-wait-response",
            )
            assert wait_response is not None
            assert wait_response["status"] == "waiting_external_work"
            assert len(wait_response["pending_tools"]) == 1
            pending_tool = wait_response["pending_tools"][0]
            assert pending_tool["task_id"] == auto_task_id
            assert pending_tool["terminal_auto_watch"] is True
            assert await external_work_crud.suspend_claimed(
                db,
                work=origin_work,
                worker_id=origin_worker_id,
                pending_tools=wait_response["pending_tools"],
            )

        if same_work:
            stop_work_id = origin_work_id
            stop_worker_id = origin_worker_id
        else:
            stop_work = await _create_claimed_reply_work(db, worker_id=stop_worker_id)
            assert stop_work.id is not None
            stop_work_id = stop_work.id

        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        terminal_session.status = TerminalSessionStatus.EXITED
        terminal_session.locked_by = None
        terminal_session.lock_until = None
        terminal_session.process_identity = None
        terminal_session.exit_code = 15
        db.add(terminal_session)
        await db.commit()

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", terminal_auto_watch_database)
    monkeypatch.setattr(terminal_tool_module, "AsyncSessionLocal", terminal_auto_watch_database)

    async def execute_foreground(db, work, _worker_id):
        profile = await db.get(Profile, PROFILE_ID)
        assert profile is not None
        close_executor = TerminalCloseExecutor(project_root=".", uid=work.uid)
        close_executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 1}}))
        close_executor.set_runtime_context(
            dispatch_context=DispatchContext(
                mode="interactive",
                source="test",
                uid=work.uid,
                session_id=work.session_id,
                profile=profile,
                db=db,
                tool_call_id="terminal-close-stop-tool-call",
            )
        )
        close_response = json.loads(await close_executor.execute(TERMINAL_SESSION_ID))
        assert close_response["action"] == TerminalAction.CLOSE.value
        assert close_response["terminal_session_id"] == TERMINAL_SESSION_ID
        assert close_response["session_status"] == TerminalSessionStatus.EXITED.value
        assert close_response["exit_code"] == 15
        assert close_response["resources_released"] is True

        if route_before_delivery:
            routed_auto = await db.get(BackgroundTask, auto_task_id)
            assert routed_auto is not None
            if same_work:
                await route_terminal_auto_watch_reply(db, routed_auto)
            else:
                assert await route_terminal_auto_watch_reply(db, routed_auto)
            routed_origin = await db.get(SessionReplyWorkItem, origin_work_id)
            assert routed_origin is not None
            await db.refresh(routed_origin)
            if same_work:
                assert routed_origin.status == SessionReplyWorkStatus.SUCCEEDED
            else:
                assert routed_origin.status == SessionReplyWorkStatus.READY_FOR_LLM

        await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="terminal close stop response"),
            work.profile_id,
            dedupe_key=_result_message_dedupe_key(work),
            commit=False,
        )
        return {
            "choices": [
                {
                    "message": {
                        "role": MessageRole.ASSISTANT,
                        "content": "terminal close stop response",
                    },
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    delivered_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event: dict) -> None:
        delivered_events.append(event)

    async def fail_session_event(_uid: str, _session_id: str, _event: dict) -> None:
        raise RuntimeError("terminal close stop reply transport failed")

    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(
        executor_lifecycle_module,
        "send_session_event",
        fail_session_event if delivery_failed else capture_session_event,
    )

    if delivery_failed:
        with pytest.raises(RuntimeError, match="terminal close stop reply transport failed"):
            await executor_lifecycle_module.execute_session_reply_work(stop_work_id, stop_worker_id)
    else:
        await executor_lifecycle_module.execute_session_reply_work(stop_work_id, stop_worker_id)
        assert delivered_events and delivered_events[-1]["work_id"] == stop_work_id

    if not route_before_delivery:
        async with terminal_auto_watch_database() as routing_db:
            routed_auto = await routing_db.get(BackgroundTask, auto_task_id)
            assert routed_auto is not None
            if same_work:
                await route_terminal_auto_watch_reply(routing_db, routed_auto)
            else:
                assert await route_terminal_auto_watch_reply(routing_db, routed_auto)
            routed_origin = await routing_db.get(SessionReplyWorkItem, origin_work_id)
            assert routed_origin is not None
            await routing_db.refresh(routed_origin)
            if delivery_failed:
                assert routed_origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert routed_auto.auto_reply is True
            elif same_work:
                assert routed_origin.status == SessionReplyWorkStatus.SUCCEEDED
            else:
                assert routed_origin.status == SessionReplyWorkStatus.CANCELLED

    async with terminal_auto_watch_database() as db:
        origin_work = await db.get(SessionReplyWorkItem, origin_work_id)
        stop_work = await db.get(SessionReplyWorkItem, stop_work_id)
        auto_task = await db.get(BackgroundTask, auto_task_id)
        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        second_terminal = await db.get(TerminalSession, SECOND_TERMINAL_SESSION_ID)
        assistant_texts = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.role == MessageRole.ASSISTANT,
                        Message.type == MessageType.TEXT,
                    )
                )
            )
            .scalars()
            .all()
        )

        assert origin_work is not None
        assert stop_work is not None
        assert auto_task is not None
        assert terminal_session is None
        assert second_terminal is not None
        assert second_terminal.status == TerminalSessionStatus.RUNNING
        assert second_terminal.locked_by is None
        assert second_terminal.process_identity == {"pid": 2}
        assert second_terminal.exit_code is None
        assert auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert auto_task.result is not None
        auto_content = auto_task.result["content"]
        assert auto_content["event"] == "process_exit"
        assert auto_content["eof"] is True
        assert auto_content["terminal_session_id"] == TERMINAL_SESSION_ID
        assert auto_content["exit_code"] == 15
        assert len(assistant_texts) == 1
        assert assistant_texts[0].content == "terminal close stop response"

        if same_work:
            assert stop_work.id == origin_work_id
            assert stop_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert stop_work.event_sent is True
            assert origin_work.status == SessionReplyWorkStatus.SUCCEEDED
        elif delivery_failed:
            assert stop_work.status == SessionReplyWorkStatus.RUNNING
            assert stop_work.event_sent is False
            assert origin_work.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert auto_task.auto_reply is True
        else:
            assert stop_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert stop_work.event_sent is True
            assert origin_work.status == SessionReplyWorkStatus.CANCELLED
            assert origin_work.locked_by is None

    if not delivery_failed:
        async with terminal_auto_watch_database() as db:
            assert (
                await session_reply_work_item_crud.claim_next(
                    db,
                    worker_id="terminal-close-must-not-claim-origin",
                    lease_seconds=300,
                )
                is None
            )


@pytest.mark.parametrize(
    ("route_before_delivery", "other_ready"),
    [
        pytest.param(False, False, id="route-after-delivery"),
        pytest.param(True, False, id="route-before-delivery"),
        pytest.param(True, True, id="route-before-delivery-with-ready-other-terminal"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_close_replies_clear_only_the_closed_terminal_watch_slot(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    route_before_delivery: bool,
    other_ready: bool,
) -> None:
    origin_worker_id = "terminal-close-two-origin-worker"
    first_terminal_worker_id = "terminal-close-two-first-worker"
    second_terminal_worker_id = "terminal-close-two-second-worker"
    terminal_calls = [
        InternalToolCall(
            id="terminal-close-two-first-shell-call",
            name=SHELL_TOOL_NAME,
            arguments={"command": "echo terminal-close-two-first", "execution_mode": "interactive"},
        ),
        InternalToolCall(
            id="terminal-close-two-second-shell-call",
            name=SHELL_TOOL_NAME,
            arguments={"command": "echo terminal-close-two-second", "execution_mode": "interactive"},
        ),
    ]
    terminal_ids = [TERMINAL_SESSION_ID, SECOND_TERMINAL_SESSION_ID]
    second_ready_content = {
        "event": "observation_error",
        "terminal_session_id": SECOND_TERMINAL_SESSION_ID,
        "session_status": TerminalSessionStatus.RUNNING.value,
        "eof": False,
        "error": "second observation ready",
        "after_offset": 0,
        "start_offset": 0,
        "next_offset": 0,
        "latest_offset": 0,
        "output": "",
    }

    async with terminal_auto_watch_database() as db:
        origin_work, first_auto_task = await _create_claimed_auto_watch(db, worker_id=origin_worker_id)
        assert origin_work.id is not None
        assert first_auto_task.id is not None
        second_auto_task = await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=SECOND_TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        assert second_auto_task is not None
        assert second_auto_task.id is not None
        assert second_auto_task.extra["origin_work_id"] == origin_work.id
        assert second_auto_task.extra["origin_work_identity"] == origin_work.dedupe_key

        tool_results = []
        for tool_call, terminal_session_id in zip(terminal_calls, terminal_ids, strict=True):
            tool_result = await save_tool_response(
                db,
                SESSION_ID,
                UID,
                PROFILE_ID,
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tool_call.id,
                    content=json.dumps(
                        {
                            "status": "succeeded",
                            "terminal_session_id": terminal_session_id,
                            "session_status": TerminalSessionStatus.RUNNING.value,
                        },
                        ensure_ascii=False,
                    ),
                ),
                [],
                [],
            )
            assert tool_result.id is not None
            tool_results.append(tool_result)

        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=terminal_calls,
            tool_results=tool_results,
            response_id="terminal-close-two-origin-wait-response",
        )
        assert wait_response is not None
        assert wait_response["status"] == "waiting_external_work"
        assert {pending["task_id"] for pending in wait_response["pending_tools"]} == {
            first_auto_task.id,
            second_auto_task.id,
        }
        assert all(pending["terminal_auto_watch"] is True for pending in wait_response["pending_tools"])
        assert await external_work_crud.suspend_claimed(
            db,
            work=origin_work,
            worker_id=origin_worker_id,
            pending_tools=wait_response["pending_tools"],
        )
        await db.refresh(origin_work)
        assert origin_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len((origin_work.execution_state or {}).get("terminal_watch_waits", [])) == 2

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", terminal_auto_watch_database)
    monkeypatch.setattr(terminal_tool_module, "AsyncSessionLocal", terminal_auto_watch_database)

    async def close_terminal(
        terminal_session_id: str,
        worker_id: str,
        tool_call_id: str,
        response_content: str,
        expected_assistant_count: int,
    ) -> int:
        async with terminal_auto_watch_database() as db:
            stop_work = await _create_claimed_reply_work(db, worker_id=worker_id)
            assert stop_work.id is not None
            terminal_session = await db.get(TerminalSession, terminal_session_id)
            assert terminal_session is not None
            terminal_session.status = TerminalSessionStatus.EXITED
            terminal_session.locked_by = None
            terminal_session.lock_until = None
            terminal_session.process_identity = None
            terminal_session.exit_code = 15
            db.add(terminal_session)
            await db.commit()
            stop_work_id = stop_work.id

        async def execute_foreground(db, work, _worker_id):
            profile = await db.get(Profile, PROFILE_ID)
            assert profile is not None
            close_executor = TerminalCloseExecutor(project_root=".", uid=work.uid)
            close_executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 1}}))
            close_executor.set_runtime_context(
                dispatch_context=DispatchContext(
                    mode="interactive",
                    source="test",
                    uid=work.uid,
                    session_id=work.session_id,
                    profile=profile,
                    db=db,
                    tool_call_id=tool_call_id,
                )
            )
            close_response = json.loads(await close_executor.execute(terminal_session_id))
            assert close_response["action"] == TerminalAction.CLOSE.value
            assert close_response["terminal_session_id"] == terminal_session_id
            assert close_response["session_status"] == TerminalSessionStatus.EXITED.value
            assert close_response["exit_code"] == 15
            assert close_response["resources_released"] is True
            if route_before_delivery and terminal_session_id == TERMINAL_SESSION_ID:
                routed_auto = await db.get(BackgroundTask, first_auto_task.id)
                assert routed_auto is not None
                assert await route_terminal_auto_watch_reply(db, routed_auto)
                routed_origin = await db.get(SessionReplyWorkItem, origin_work.id)
                assert routed_origin is not None
                await db.refresh(routed_origin)
                assert routed_origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert (routed_origin.execution_state or {}).get("terminal_auto_resume") is True
            if other_ready and terminal_session_id == TERMINAL_SESSION_ID:
                assert route_before_delivery
                second_routed_auto = await db.get(BackgroundTask, second_auto_task.id)
                assert second_routed_auto is not None
                await _complete_auto_watch(
                    db,
                    task=second_routed_auto,
                    worker_id="terminal-close-two-second-observer",
                    content=second_ready_content,
                )
                await db.refresh(second_routed_auto)
                assert second_routed_auto.status == BackgroundTaskStatus.SUCCEEDED
                assert await route_terminal_auto_watch_reply(db, second_routed_auto)
                routed_origin = await db.get(SessionReplyWorkItem, origin_work.id)
                assert routed_origin is not None
                await db.refresh(routed_origin)
                assert routed_origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            await save_message(
                db,
                work.session_id,
                work.uid,
                MessageRole.ASSISTANT,
                MessageType.TEXT,
                InternalMessage(role=MessageRole.ASSISTANT, content=response_content),
                work.profile_id,
                dedupe_key=_result_message_dedupe_key(work),
                commit=False,
            )
            return {
                "choices": [
                    {
                        "message": {"role": MessageRole.ASSISTANT, "content": response_content},
                        "finish_reason": "stop",
                    }
                ],
                "history": [],
                "files": None,
            }

        delivered_events: list[dict] = []

        async def capture_session_event(_uid: str, _session_id: str, event: dict) -> None:
            delivered_events.append(event)

        monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)
        await executor_lifecycle_module.execute_session_reply_work(stop_work_id, worker_id)
        assert delivered_events and delivered_events[-1]["work_id"] == stop_work_id

        async with terminal_auto_watch_database() as db:
            persisted_stop = await db.get(SessionReplyWorkItem, stop_work_id)
            assert persisted_stop is not None
            assert persisted_stop.status == SessionReplyWorkStatus.SUCCEEDED
            assert persisted_stop.event_sent is True
            assert persisted_stop.result_message_id is not None
            result_message = await db.get(Message, persisted_stop.result_message_id)
            assert result_message is not None
            assert result_message.content == response_content
            assistant_texts = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == SESSION_ID,
                            Message.role == MessageRole.ASSISTANT,
                            Message.type == MessageType.TEXT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(assistant_texts) == expected_assistant_count
        return stop_work_id

    first_stop_work_id = await close_terminal(
        TERMINAL_SESSION_ID,
        first_terminal_worker_id,
        "terminal-close-two-first-stop-call",
        "first terminal close response",
        1,
    )

    async with terminal_auto_watch_database() as db:
        origin_work = await db.get(SessionReplyWorkItem, origin_work.id)
        first_auto_task = await db.get(BackgroundTask, first_auto_task.id)
        second_auto_task = await db.get(BackgroundTask, second_auto_task.id)
        first_terminal = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        second_terminal = await db.get(TerminalSession, SECOND_TERMINAL_SESSION_ID)
        assert origin_work is not None
        assert first_auto_task is not None
        assert second_auto_task is not None
        assert first_terminal is None
        assert second_terminal is not None
        assert second_terminal.status == TerminalSessionStatus.RUNNING
        assert first_auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert first_auto_task.result["content"]["exit_code"] == 15
        if other_ready:
            assert origin_work.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert second_auto_task.status == BackgroundTaskStatus.SUCCEEDED
            resume_worker_id = "terminal-close-two-other-ready-resume-worker"
            claimed_origin = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=resume_worker_id,
                lease_seconds=300,
            )
            assert claimed_origin is not None
            assert claimed_origin.id == origin_work.id
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (claimed_origin.id, resume_worker_id)
            assert await prepare_terminal_auto_watch_resume(db, claimed_origin, resume_worker_id)
            await db.refresh(claimed_origin)
            assert claimed_origin.status == SessionReplyWorkStatus.RUNNING
            checkpoint = (claimed_origin.execution_state or {}).get("dispatcher_checkpoint") or {}
            pending_resume_ids = checkpoint.get("pending_resume_message_ids")
            assert isinstance(pending_resume_ids, list) and pending_resume_ids
            marker_payloads = []
            for marker_id in pending_resume_ids:
                marker = await db.get(Message, marker_id)
                assert marker is not None
                marker_payloads.append(json.loads(marker.content or "{}"))
            assert any(payload.get("task_id") == second_auto_task.id and payload.get("task") == second_ready_content for payload in marker_payloads)
            return

        assert origin_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        waits = (origin_work.execution_state or {}).get("terminal_watch_waits") or []
        assert [slot["task_id"] for slot in waits] == [second_auto_task.id]
        active_second = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            SECOND_TERMINAL_SESSION_ID,
            automatic_only=True,
        )
        assert active_second is not None
        assert active_second.id == second_auto_task.id
        assert (
            await session_reply_work_item_crud.claim_next(
                db,
                worker_id="terminal-close-two-must-still-wait",
                lease_seconds=300,
            )
            is None
        )

    second_stop_work_id = await close_terminal(
        SECOND_TERMINAL_SESSION_ID,
        second_terminal_worker_id,
        "terminal-close-two-second-stop-call",
        "second terminal close response",
        2,
    )

    async with terminal_auto_watch_database() as db:
        origin_work = await db.get(SessionReplyWorkItem, origin_work.id)
        first_auto_task = await db.get(BackgroundTask, first_auto_task.id)
        second_auto_task = await db.get(BackgroundTask, second_auto_task.id)
        first_stop_work = await db.get(SessionReplyWorkItem, first_stop_work_id)
        second_stop_work = await db.get(SessionReplyWorkItem, second_stop_work_id)
        first_terminal = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        second_terminal = await db.get(TerminalSession, SECOND_TERMINAL_SESSION_ID)
        assert origin_work is not None
        assert first_auto_task is not None
        assert second_auto_task is not None
        assert first_stop_work is not None
        assert second_stop_work is not None
        assert origin_work.status == SessionReplyWorkStatus.CANCELLED
        assert (
            await session_reply_work_item_crud.claim_next(
                db,
                worker_id="terminal-close-two-must-not-claim-origin",
                lease_seconds=300,
            )
            is None
        )
        assert first_stop_work.status == SessionReplyWorkStatus.SUCCEEDED
        assert first_stop_work.event_sent is True
        assert second_stop_work.status == SessionReplyWorkStatus.SUCCEEDED
        assert second_stop_work.event_sent is True
        assert first_terminal is None
        assert second_terminal is None
        assert first_auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert second_auto_task.status == BackgroundTaskStatus.SUCCEEDED
        assert first_auto_task.result["content"]["exit_code"] == 15
        assert second_auto_task.result["content"]["exit_code"] == 15
