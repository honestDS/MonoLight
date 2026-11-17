import copy
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.terminal.watcher as watcher_module
import app.core.tools.terminal as terminal_tool_module
from app.core.background_tasks import runner as runner_module
from app.core.background_tasks.terminal_watch_reply import prepare_terminal_auto_watch_resume, route_terminal_auto_watch_reply
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY, SHELL_TOOL_NAME
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.dispatchers.context import DispatchContext
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.tools.terminal import TerminalToolExecutor
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.dispatcher.external_work import build_terminal_wait_response
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalSession
from tests.integration.terminal.terminal_auto_watch_support import (
    PROFILE_ID,
    SECOND_TERMINAL_SESSION_ID,
    SESSION_ID,
    TERMINAL_SESSION_ID,
    UID,
    _complete_auto_watch,
    _create_claimed_auto_watch,
    _create_claimed_reply_work,
    _submit_terminal_watch_subscription,
    _terminal_watch_tasks,
    terminal_auto_watch_database,
)

__all__ = ("terminal_auto_watch_database",)


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
        work.execution_state = {**work.execution_state, "show_tool_calls": True}
        original_sequence_no = work.sequence_no
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
        stream_events = await session_reply_stream_event_crud.list_after_sequence(
            db,
            work_id=work_id,
            after_sequence_no=0,
        )
        tool_end_events = [event for event in stream_events if event.event.get("type") == "tool_end"]
        assert len(tool_end_events) == 1
        tool_end = tool_end_events[0]
        assert tool_end.event["tool_call_id"] == tool_call.id
        assert tool_end.event["work_sequence_no"] == claimed_work.sequence_no
        assert claimed_work.sequence_no > original_sequence_no
        checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, claimed_work.id) or {})
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
        persisted_checkpoint = await session_reply_work_item_crud.get_checkpoint(verify_db, persisted_work.id)
        assert persisted_checkpoint == checkpoint
        state = persisted_work.execution_state or {}
        assert state["terminal_auto_resume"] is True
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING
        assert persisted_work.locked_by == new_worker_id
        assert persisted_work.lock_until == reclaimed_lock_until

        stale_update = {
            "messages": [],
            "test_marker": "old-worker-must-not-overwrite",
        }
        assert not await session_reply_work_item_crud.update_claimed(
            verify_db,
            work_id=work_id,
            worker_id=old_worker_id,
            values={"execution_state": {"stale_worker": True}},
            checkpoint=stale_update,
        )
        await verify_db.refresh(persisted_work)
        final_state = persisted_work.execution_state or {}
        final_checkpoint = await session_reply_work_item_crud.get_checkpoint(verify_db, persisted_work.id)
        assert final_checkpoint == checkpoint
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
        close_executor = TerminalToolExecutor(project_root=".", uid=work.uid)
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
        close_response = json.loads(
            await close_executor.execute(
                operation="close",
                terminal_session_id=TERMINAL_SESSION_ID,
            )
        )
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
            close_executor = TerminalToolExecutor(project_root=".", uid=work.uid)
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
            close_response = json.loads(
                await close_executor.execute(
                    operation="close",
                    terminal_session_id=terminal_session_id,
                )
            )
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
            checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, claimed_origin.id) or {})
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
