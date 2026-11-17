import copy
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.background_tasks import reply_trigger as reply_trigger_module
from app.core.background_tasks.terminal_watch_reply import (
    prepare_terminal_auto_watch_resume,
    route_terminal_auto_watch_reply,
)
from app.core.constants import (
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SHELL_TOOL_NAME,
    TERMINAL_WATCH_TOOL_NAME,
)
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.terminal.schemas import TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.dispatcher.external_work import build_terminal_wait_response
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplySequence, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalSession
from tests.integration.terminal.terminal_auto_watch_support import (
    AFTER_OFFSET,
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
            checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, claimed_work.id) or {})
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
            checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, claimed_work.id) or {})
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
        checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, work.id) or {})

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
