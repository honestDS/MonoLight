import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.terminal.audit_lifecycle as terminal_audit_lifecycle_module
import app.core.terminal.watcher as watcher_module
from app.core.audit.confirmation_lifecycle import stop_interactive_reply
from app.core.background_tasks import runner as runner_module
from app.core.background_tasks.terminal_watch_coordinator import (
    complete_terminal_watch_round,
    create_terminal_auto_watch,
    create_terminal_watch_subscription,
)
from app.core.background_tasks.terminal_watch_reply import route_terminal_auto_watch_reply
from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.task.background import background_task_crud
from app.core.dispatchers.context import DispatchContext
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus, TerminalWriteRequest
from app.core.tools.terminal import TerminalToolExecutor
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.models.background_task import BackgroundTask, BackgroundTaskReplyMode, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession
from tests.integration.terminal.terminal_auto_watch_support import (
    AFTER_OFFSET,
    PROFILE_ID,
    SESSION_ID,
    TERMINAL_SESSION_ID,
    UID,
    _complete_auto_watch,
    _create_claimed_auto_watch,
    _create_claimed_reply_work,
    _terminal_watch_tasks,
    terminal_auto_watch_database,
)

__all__ = ("terminal_auto_watch_database",)


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
            "reply_mode": BackgroundTaskReplyMode.RESUME_AGENT.value,
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
        executor = TerminalToolExecutor(project_root=".", uid=UID)
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
                    operation="read",
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
