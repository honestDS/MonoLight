import copy
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.background_tasks.terminal_watch_reply import prepare_terminal_auto_watch_resume, route_terminal_auto_watch_reply
from app.core.constants import (
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
)
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.terminal.schemas import TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.dispatcher.external_work import build_terminal_wait_response
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.models.agent_work import AgentWorkSpec
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalSession
from tests.integration.terminal.terminal_auto_watch_support import (
    AFTER_OFFSET,
    PROFILE_ID,
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


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        pytest.param(
            SHELL_TOOL_NAME,
            {"command": "echo terminal-auto-watch-sync", "execution_mode": "interactive"},
            id="shell",
        ),
        pytest.param(
            TERMINAL_TOOL_NAME,
            {"operation": "write", "terminal_session_id": TERMINAL_SESSION_ID, "data": "input\n"},
            id="write",
        ),
        pytest.param(
            TERMINAL_TOOL_NAME,
            {"operation": "read", "terminal_session_id": TERMINAL_SESSION_ID, "offset": 0, "read_output": True},
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

        if arguments.get("operation") != "read":
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


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("agent-profile-retained", id="agent-profile-retained"),
        pytest.param("agent-profile-deleted", id="agent-profile-deleted"),
        pytest.param("foreground-changed", id="foreground-changed"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_auto_watch_profile_change_routes_agent_work_by_work_profile(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    case: str,
) -> None:
    worker_id = f"terminal-auto-watch-profile-change-{case}"
    profile_b_id = PROFILE_ID + 1

    async with terminal_auto_watch_database() as db:
        work = await _create_claimed_reply_work(db, worker_id=worker_id)
        if case != "foreground-changed":
            work.work_type = SessionReplyWorkType.AGENT_WORK
            work.source_type = SessionReplySourceType.SCHEDULED_TASK_RUN
            work.execution_state = {
                "frozen_context_message_ids": [],
                "agent_work": AgentWorkSpec(instruction="watch the scheduled terminal").model_dump(mode="json"),
            }
        await db.flush()

        task = await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            session_status=TerminalSessionStatus.RUNNING,
        )
        assert task is not None and task.id is not None

        tool_call = InternalToolCall(
            id=f"terminal-auto-watch-profile-change-{case}-call",
            name=TERMINAL_TOOL_NAME,
            arguments={
                "operation": "write",
                "terminal_session_id": TERMINAL_SESSION_ID,
                "data": "input\n",
            },
        )
        receipt_payload = {
            "status": "succeeded",
            "terminal_session_id": TERMINAL_SESSION_ID,
            "session_status": TerminalSessionStatus.RUNNING.value,
        }
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
        wait_response = await build_terminal_wait_response(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_calls=[tool_call],
            tool_results=[tool_result],
            response_id=f"terminal-auto-watch-profile-change-{case}-response",
        )
        assert wait_response is not None and wait_response["status"] == "waiting_external_work"
        pending_tool = wait_response["pending_tools"][0]
        assert pending_tool["task_id"] == task.id
        assert pending_tool["tool_result_message_id"] == tool_result.id
        assert pending_tool["terminal_auto_watch"] is True
        assert await external_work_crud.suspend_claimed(
            db,
            work=work,
            worker_id=worker_id,
            pending_tools=wait_response["pending_tools"],
        )

        db.add(Profile(id=profile_b_id, uid=UID, name="terminal-auto-watch-profile-b", configs={}, is_default=False))
        await db.flush()
        session = await db.get(ChatSession, SESSION_ID)
        assert session is not None
        session.profile_id = profile_b_id
        session.profile_override_id = profile_b_id
        await db.flush()

        if case == "agent-profile-deleted":
            old_profile = await db.get(Profile, PROFILE_ID)
            assert old_profile is not None
            assert old_profile.is_default is False
            await db.delete(old_profile)
            await db.flush()
            assert await db.get(Profile, PROFILE_ID) is None

        completion = {
            "event": "process_exit",
            "eof": True,
            "next_offset": 0,
            "terminal_session_id": TERMINAL_SESSION_ID,
            "exit_code": 0,
            "session_status": TerminalSessionStatus.EXITED.value,
        }
        await _complete_auto_watch(db, task=task, worker_id=worker_id, content=completion)
        assert task.status == BackgroundTaskStatus.SUCCEEDED
        assert await route_terminal_auto_watch_reply(db, task)
        await db.refresh(work)
        await db.refresh(task)
        await db.refresh(session)
        assert (session.profile_id, session.profile_override_id) == (profile_b_id, profile_b_id)
        assert (work.profile_id, task.profile_id) == (PROFILE_ID, PROFILE_ID)

        if case == "agent-profile-retained":
            assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
            resume_worker_id = f"{worker_id}-resume"
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=resume_worker_id,
                lease_seconds=300,
            )
            assert claimed_work is not None
            assert claimed_work.id == work.id
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, resume_worker_id)
            assert await prepare_terminal_auto_watch_resume(db, claimed_work, resume_worker_id)
            await db.refresh(claimed_work)
            await db.refresh(task)
            assert claimed_work.status == SessionReplyWorkStatus.RUNNING
        else:
            assert work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert work.work_type == (SessionReplyWorkType.AGENT_WORK if case == "agent-profile-deleted" else SessionReplyWorkType.FOREGROUND_REPLY)
            assert task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED

        markers = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID, Message.type == MessageType.BACKGROUND_TASK_RESULT))).scalars().all())
        if case == "agent-profile-retained":
            assert len(markers) == 1
            marker = markers[0]
            assert marker.uid == UID and marker.profile_id == PROFILE_ID and marker.work_id == work.id
            assert json.loads(marker.content or "{}")["task_id"] == task.id
            assert task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        else:
            assert markers == []


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
            checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, old_work.id) or {})
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
            checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, claimed_work.id) or {})
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
