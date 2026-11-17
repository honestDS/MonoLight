import asyncio
import copy
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.background_tasks import reply_trigger as reply_trigger_module
from app.core.background_tasks.terminal_watch_coordinator import (
    complete_terminal_watch_round,
)
from app.core.constants import SHELL_TOOL_NAME, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.terminal.schemas import TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.terminal_watch_snapshot import append_terminal_watch_snapshot, load_current_terminal_watch_snapshot
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalConversationSummary, InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from tests.integration.terminal.terminal_auto_watch_support import (
    AFTER_OFFSET,
    PROFILE_ID,
    SECOND_TERMINAL_SESSION_ID,
    SESSION_ID,
    TERMINAL_SESSION_ID,
    UID,
    _create_claimed_reply_work,
    _terminal_watch_tasks,
    terminal_auto_watch_database,
)

__all__ = ("terminal_auto_watch_database",)


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
        frozen_context = task.extra["frozen_context_messages"]
        frozen_context_text = json.dumps(frozen_context, ensure_ascii=False)
        assert request_content in frozen_context_text
        assert purpose in frozen_context_text

        snapshot = await load_current_terminal_watch_snapshot(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
        )
        assert snapshot is not None
        terminal = next(item for item in snapshot["terminals"] if item["terminal_session_id"] == TERMINAL_SESSION_ID)
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
        assert next_task.extra["frozen_context_messages"] == frozen_context
        assert later_request.id not in [item["id"] for item in next_task.extra["request_context"]]
        assert later_request_content not in json.dumps(next_task.extra, ensure_ascii=False)

        renewed_snapshot = await load_current_terminal_watch_snapshot(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
        )
        assert renewed_snapshot is not None
        renewed_terminal = next(item for item in renewed_snapshot["terminals"] if item["terminal_session_id"] == TERMINAL_SESSION_ID)
        assert renewed_terminal["request_context"] == expected_request_context
        assert renewed_terminal["purpose"] == purpose
        assert later_request_content not in json.dumps(renewed_terminal, ensure_ascii=False)

        summary_base = "compressed history"
        summary_message = InternalMessage(
            role=MessageRole.USER,
            content="preserved original request",
            conversation_summary=InternalConversationSummary(
                through_message_id=10,
                content=summary_base,
            ),
        )
        original_request_snapshot = request_message.model_dump(mode="json")
        original_summary_snapshot = summary_message.model_dump(mode="json")
        compressed_messages = append_terminal_watch_snapshot([request_message, summary_message], renewed_snapshot)
        assert is_context_summary_message(summary_message)
        assert is_context_summary_message(compressed_messages[1])
        assert compressed_messages[0].content == request_content
        assert compressed_messages[1].content == "preserved original request"
        assert request_message.model_dump(mode="json") == original_request_snapshot
        assert summary_message.model_dump(mode="json") == original_summary_snapshot

        summary_meta = compressed_messages[1].conversation_summary
        assert summary_meta is not None
        assert summary_meta.content == summary_base
        assert compressed_messages[0].current_terminal_watch is None
        assert compressed_messages[1].current_terminal_watch == renewed_snapshot

        compressed_terminal = next(item for item in compressed_messages[1].current_terminal_watch["terminals"] if item["terminal_session_id"] == TERMINAL_SESSION_ID)
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
        resume_work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == SESSION_ID,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME,
                    )
                )
            )
            .scalars()
            .all()
        )
        checkpoint_work = resume_work_items[0]
        assert checkpoint_work.id is not None
        checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, checkpoint_work.id) or {})

    assert persisted_manual is not None
    assert persisted_manual.status == manual_status
    assert persisted_auto is not None
    assert persisted_auto.status == BackgroundTaskStatus.SUCCEEDED
    assert persisted_auto.reply_status == BackgroundTaskReplyStatus.RUNNING
    assert len(result_messages) == 0
    assert len(resume_work_items) == 1
    resume_work = resume_work_items[0]
    assert resume_work.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME
    assert resume_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert resume_work.source_id == str(auto_task_id)
    resume_state = dict(resume_work.execution_state or {})
    external_resume_context = resume_state.get("external_resume_context")
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
        resume_work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == SESSION_ID,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME,
                    )
                )
            )
            .scalars()
            .all()
        )
        checkpoint_work = resume_work_items[0]
        assert checkpoint_work.id is not None
        checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, checkpoint_work.id) or {})

    assert persisted_manual is not None
    assert persisted_manual.status == BackgroundTaskStatus.PENDING
    assert persisted_auto is not None
    assert persisted_auto.status == BackgroundTaskStatus.SUCCEEDED
    assert persisted_auto.reply_status == BackgroundTaskReplyStatus.RUNNING
    assert len(result_messages) == 0
    assert len(resume_work_items) == 1
    resume_work = resume_work_items[0]
    assert resume_work.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME
    assert resume_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert resume_work.source_id == str(auto_task_id)
    resume_state = dict(resume_work.execution_state or {})
    external_resume_context = resume_state.get("external_resume_context")
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
    assert checkpoint["reload_history"] is True
    assert checkpoint["deferred_external_results"] is True
