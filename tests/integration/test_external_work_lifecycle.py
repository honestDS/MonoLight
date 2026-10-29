import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.background_tasks import external_work as external_work_module
from app.core.background_tasks.manager import BackgroundTaskManager
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor_common as executor_common_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.manager_result import SessionReplyResult
from app.core.tools.background_tasks import ManageBackgroundTasksExecutor
from app.core.utils.dispatcher.external_work import build_external_wait_response
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User

UID = "external-work-user"
SESSION_ID = "external-work-session"
PROFILE_ID = 1
REPLY_WORKER = "reply-worker"


@dataclass(frozen=True)
class ExternalWorkCase:
    factory: async_sessionmaker[AsyncSession]
    uid: str
    session_id: str
    profile_id: int
    worker_id: str
    work_id: int
    dedupe_key: str
    initial_sequence_no: int
    user_message_id: int
    assistant: InternalMessage
    tool_calls: tuple[InternalToolCall, ...]
    task_ids: dict[str, int]
    result_message_ids: dict[str, int]


@pytest_asyncio.fixture
async def external_work_database(database_factory, monkeypatch):
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="external-work-lifecycle.sqlite3",
    ) as session_factory:
        monkeypatch.setattr(external_work_module, "AsyncSessionLocal", session_factory)
        async with session_factory() as db:
            db.add_all(
                [
                    User(uid=UID, username="external-work-user"),
                    Profile(id=PROFILE_ID, uid=UID, name="external-work-profile", configs={}),
                    ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID),
                ]
            )
            await db.commit()
        yield session_factory


async def _create_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    call_ids: tuple[str, ...] = ("call-1",),
    suspend: bool = True,
) -> ExternalWorkCase:
    tool_calls = tuple(
        InternalToolCall(
            id=call_id,
            name="external_test_tool",
            arguments={"value": call_id},
        )
        for call_id in call_ids
    )
    dedupe_key = f"external-work:{SESSION_ID}:{call_ids[0]}"

    async with session_factory() as db:
        profile = await db.get(Profile, PROFILE_ID)
        assert profile is not None

        user_message = await save_message(
            db,
            SESSION_ID,
            UID,
            MessageRole.USER,
            MessageType.TEXT,
            InternalMessage.from_user_input(content="开始外部工作"),
            PROFILE_ID,
        )
        assistant = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=list(tool_calls))
        assistant_row = await save_message(
            db,
            SESSION_ID,
            UID,
            MessageRole.ASSISTANT,
            MessageType.TOOL_CALL,
            assistant,
            PROFILE_ID,
        )
        assert user_message.id is not None and assistant_row.id is not None
        assistant.id = assistant_row.id

        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=user_message.id,
            dedupe_key=dedupe_key,
            commit=False,
        )
        assert created and work.id is not None
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=REPLY_WORKER,
            lease_seconds=3600,
        )
        assert claimed is not None and claimed.id == work.id
        assert claimed.sequence_no is not None

        checkpoint = {
            "messages": [
                user_message.model_dump(mode="json", exclude_none=True),
                assistant.model_dump(mode="json", exclude_none=True),
            ],
            "turn_messages": [assistant.model_dump(mode="json", exclude_none=True)],
        }
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=claimed.id,
            worker_id=REPLY_WORKER,
            values={
                "input_message_ids": [user_message.id],
                "execution_state": {
                    "dispatcher_checkpoint": checkpoint,
                    "request_ids": ["external-request-1"],
                    "show_tool_calls": True,
                },
            },
        )

        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (claimed.id, REPLY_WORKER)
        manager = BackgroundTaskManager()
        task_ids: dict[str, int] = {}
        result_message_ids: dict[str, int] = {}
        for tool_call in tool_calls:
            task = await manager.submit(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile=profile,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                messages=[user_message, assistant],
            )
            assert task.id is not None
            assert task.auto_reply is False
            assert task.reply_status.value == "none"
            assert task.extra["origin_work_id"] == claimed.id
            assert task.extra["origin_work_identity"] == dedupe_key
            task_ids[tool_call.id] = task.id

            queued_result = await save_message(
                db,
                SESSION_ID,
                UID,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tool_call.id,
                    content=json.dumps({"status": "queued", "task_id": task.id}),
                ),
                PROFILE_ID,
            )
            assert queued_result.id is not None
            result_message_ids[tool_call.id] = queued_result.id

        initial_sequence_no = claimed.sequence_no

    case = ExternalWorkCase(
        factory=session_factory,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        worker_id=REPLY_WORKER,
        work_id=work.id,
        dedupe_key=dedupe_key,
        initial_sequence_no=initial_sequence_no,
        user_message_id=user_message.id,
        assistant=assistant,
        tool_calls=tool_calls,
        task_ids=task_ids,
        result_message_ids=result_message_ids,
    )
    if suspend:
        await _suspend_case(case)
    return case


async def _suspend_case(case: ExternalWorkCase) -> None:
    async with case.factory() as db:
        work = await db.get(SessionReplyWorkItem, case.work_id)
        assert work is not None
        pending_results: list[InternalMessage] = []
        for call_id in case.task_ids:
            row = await db.get(Message, case.result_message_ids[call_id])
            assert row is not None and row.id is not None
            pending = InternalMessage.model_validate_json(row.content or "{}")
            pending.id = row.id
            pending_results.append(pending)

        response = build_external_wait_response(
            list(case.tool_calls),
            pending_results,
            response_id="external-response-1",
        )
        assert response is not None
        assert response["status"] == "waiting_external_work"
        assert await external_work_crud.suspend_claimed(
            db,
            work=work,
            worker_id=case.worker_id,
            pending_tools=response["pending_tools"],
        )


async def _complete_task(case: ExternalWorkCase, call_id: str, content: str | None = None) -> None:
    task_id = case.task_ids[call_id]
    task_worker = f"external-task-worker-{task_id}"
    async with case.factory() as db:
        claimed = await background_task_crud.try_claim(
            db,
            task_id=task_id,
            worker_id=task_worker,
            lease_seconds=3600,
        )
        assert claimed is not None
        assert claimed.status == BackgroundTaskStatus.RUNNING
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=task_id,
            worker_id=task_worker,
            result={"content": content or f"completed-{call_id}"},
            auto_reply=False,
        )


async def _fail_task(case: ExternalWorkCase, call_id: str, *, error: str, content: str) -> None:
    task_id = case.task_ids[call_id]
    task_worker = f"external-task-worker-{task_id}"
    async with case.factory() as db:
        claimed = await background_task_crud.try_claim(
            db,
            task_id=task_id,
            worker_id=task_worker,
            lease_seconds=3600,
        )
        assert claimed is not None
        assert claimed.status == BackgroundTaskStatus.RUNNING
        assert await background_task_crud.mark_failed(
            db,
            task_id=task_id,
            worker_id=task_worker,
            error=error,
            result={"content": content},
            auto_reply=False,
        )


async def _claim_next(
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
) -> int | None:
    async with session_factory() as db:
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=3600,
        )
        return claimed.id if claimed is not None else None


async def _enqueue_followup(case: ExternalWorkCase) -> tuple[int, int]:
    async with case.factory() as db:
        message = await save_message(
            db,
            case.session_id,
            case.uid,
            MessageRole.USER,
            MessageType.TEXT,
            InternalMessage.from_user_input(content="后续用户工作"),
            case.profile_id,
        )
        assert message.id is not None
        followup, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=case.uid,
            session_id=case.session_id,
            profile_id=case.profile_id,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=message.id,
            dedupe_key=f"external-followup:{case.work_id}",
            commit=True,
        )
        assert created and followup.id is not None and followup.sequence_no is not None
        return followup.id, followup.sequence_no


async def _snapshot(case: ExternalWorkCase) -> dict[str, Any]:
    async with case.factory() as db:
        work = await db.get(SessionReplyWorkItem, case.work_id)
        assert work is not None
        task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == case.session_id).order_by(BackgroundTask.id))
        message_result = await db.execute(select(Message).where(Message.session_id == case.session_id, Message.uid == case.uid).order_by(Message.id))
        stream_result = await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == case.work_id).order_by(SessionReplyStreamEvent.sequence_no))
        session_event_result = await db.execute(select(SessionEvent).where(SessionEvent.session_id == case.session_id, SessionEvent.uid == case.uid).order_by(SessionEvent.id))
        return {
            "work": work,
            "tasks": list(task_result.scalars().all()),
            "messages": list(message_result.scalars().all()),
            "stream_events": list(stream_result.scalars().all()),
            "session_events": list(session_event_result.scalars().all()),
        }


def _tool_result(snapshot: dict[str, Any], message_id: int) -> InternalMessage:
    row = next(message for message in snapshot["messages"] if message.id == message_id)
    return InternalMessage.model_validate_json(row.content or "{}")


def _assert_queued_tool_result(snapshot: dict[str, Any], case: ExternalWorkCase, call_id: str) -> InternalMessage:
    result = _tool_result(snapshot, case.result_message_ids[call_id])
    assert result.tool_call_id == call_id
    assert result.tool_execution_status is None
    assert json.loads(result.content or "{}") == {
        "status": "queued",
        "task_id": case.task_ids[call_id],
    }
    return result


def _assert_background_result_marker(marker: Message, *, task: Any, execution_status: str) -> dict[str, Any]:
    assert marker.type == MessageType.BACKGROUND_TASK_RESULT
    payload = json.loads(marker.content or "{}")
    assert payload["task"] == task
    assert payload["execution_status"] == execution_status
    return payload


def _messages_of_type(snapshot: dict[str, Any], message_type: MessageType) -> list[Message]:
    return [message for message in snapshot["messages"] if message.type == message_type]


@pytest.mark.asyncio
async def test_consumer_does_not_cancel_reply_during_external_resume_handoff(
    external_work_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _create_case(external_work_database, suspend=False)
    completed_content = "completed-during-resume-handoff"
    await _complete_task(case, "call-1", completed_content)

    queued_snapshot = await _snapshot(case)
    queued_result = _tool_result(queued_snapshot, case.result_message_ids["call-1"])
    queued_result.id = case.result_message_ids["call-1"]
    wait_response = build_external_wait_response(
        list(case.tool_calls),
        [queued_result],
        response_id="external-handoff-response",
    )
    assert wait_response is not None

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", external_work_database)
    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", external_work_database)

    async def execute_foreground(_db, _work, _worker_id):
        return wait_response

    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)

    resume_started = asyncio.Event()
    release_resume = asyncio.Event()
    original_get_session = external_work_module.session_crud.get_by_session_id

    async def wait_after_lock(db, session_id):
        resume_started.set()
        await release_resume.wait()
        return await original_get_session(db, session_id)

    monkeypatch.setattr(external_work_module.session_crud, "get_by_session_id", wait_after_lock)

    consumer = consumer_module.SessionReplyConsumer()
    consumer_after_cancel_check = asyncio.Event()

    async def runtime_settings(_db):
        consumer_after_cancel_check.set()
        consumer._stop_event.set()
        return SimpleNamespace(session_reply_max_concurrency=0)

    async def skip_recovery():
        return None

    async def skip_cleanup(_now):
        return None

    monkeypatch.setattr(consumer_module.system_setting_crud, "get_runtime_settings", runtime_settings)
    monkeypatch.setattr(consumer, "_recover_expired", skip_recovery)
    monkeypatch.setattr(consumer, "_cleanup_terminal_items_if_due", skip_cleanup)

    reply_task: asyncio.Task | None = None
    consumer_task: asyncio.Task | None = None
    try:
        reply_task = asyncio.create_task(executor_lifecycle_module.execute_session_reply_work(case.work_id, case.worker_id))
        await asyncio.wait_for(resume_started.wait(), timeout=2)

        loop = asyncio.get_running_loop()
        consumer._last_lease_renewal_at = loop.time()
        consumer._running[case.work_id] = (reply_task, case.worker_id)
        consumer_task = asyncio.create_task(consumer._run())
        await asyncio.wait_for(consumer_after_cancel_check.wait(), timeout=2)
        await asyncio.wait_for(consumer_task, timeout=2)

        release_resume.set()
        await asyncio.gather(reply_task, return_exceptions=True)

        assert not reply_task.cancelled()
        snapshot = await _snapshot(case)
        assert snapshot["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
        assert snapshot["work"].execution_state["external_resume_pending"] is True
        _assert_queued_tool_result(snapshot, case, "call-1")

        markers = _messages_of_type(snapshot, MessageType.BACKGROUND_TASK_RESULT)
        assert len(markers) == 1
        _assert_background_result_marker(
            markers[0],
            task=completed_content,
            execution_status="succeeded",
        )

        result_manager = SessionReplyResult()
        async with external_work_database() as db:
            status = await result_manager.get_work_status(db, case.work_id, uid=case.uid)
        assert status["status"] == SessionReplyWorkStatus.READY_FOR_LLM.value
        queried_message = status["session_events"][0]["messages"][0]
        assert queried_message["id"] == markers[0].id
        assert json.loads(queried_message["content"])["task"] == completed_content

        async with external_work_database() as db:
            lock_probe = Message(
                session_id=case.session_id,
                uid=case.uid,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content="lock-probe",
                profile_id=case.profile_id,
            )
            db.add(lock_probe)
            await db.commit()
            assert lock_probe.id is not None
    finally:
        release_resume.set()
        if consumer_task is not None and not consumer_task.done():
            consumer_task.cancel()
        if reply_task is not None and not reply_task.done():
            reply_task.cancel()
        pending_tasks = [task for task in (consumer_task, reply_task) if task is not None]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        consumer._running.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidation",
    ["cancelled", "deleted", "taken-over"],
    ids=["user-stop", "session-delete", "worker-takeover"],
)
async def test_consumer_cancels_reply_after_claim_is_invalidated(
    external_work_database,
    monkeypatch: pytest.MonkeyPatch,
    invalidation: str,
) -> None:
    case = await _create_case(external_work_database, suspend=False)
    before = await _snapshot(case)
    queued_before = _assert_queued_tool_result(before, case, "call-1")

    llm_started = asyncio.Event()
    release_llm = asyncio.Event()

    async def controlled_foreground(_db, _work, _worker_id):
        llm_started.set()
        await release_llm.wait()
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "must not persist"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", external_work_database)
    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", external_work_database)
    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", controlled_foreground)

    reply_finished = asyncio.Event()
    consumer_checked_claim = asyncio.Event()
    consumer = consumer_module.SessionReplyConsumer()
    reply_task: asyncio.Task | None = None
    consumer_task: asyncio.Task | None = None

    async def runtime_settings(_db):
        consumer_checked_claim.set()
        consumer._stop_event.set()
        return SimpleNamespace(session_reply_max_concurrency=0)

    async def skip_recovery():
        return None

    async def skip_cleanup(_now):
        return None

    monkeypatch.setattr(consumer_module.system_setting_crud, "get_runtime_settings", runtime_settings)
    monkeypatch.setattr(consumer, "_recover_expired", skip_recovery)
    monkeypatch.setattr(consumer, "_cleanup_terminal_items_if_due", skip_cleanup)

    try:
        reply_task = asyncio.create_task(executor_lifecycle_module.execute_session_reply_work(case.work_id, case.worker_id))
        reply_task.add_done_callback(lambda _task: reply_finished.set())
        await asyncio.wait_for(llm_started.wait(), timeout=2)

        async with external_work_database() as db:
            if invalidation == "cancelled":
                assert (
                    await session_reply_work_item_crud.cancel_session(
                        db,
                        session_id=case.session_id,
                        uid=case.uid,
                    )
                    == 1
                )
            elif invalidation == "deleted":
                assert (
                    await session_reply_work_item_crud.delete_by_session(
                        db,
                        session_id=case.session_id,
                        uid=case.uid,
                    )
                    == 1
                )
            else:
                work = await db.get(SessionReplyWorkItem, case.work_id)
                assert work is not None
                work.status = SessionReplyWorkStatus.RUNNING
                work.locked_by = "other-worker"
                await db.commit()

        loop = asyncio.get_running_loop()
        consumer._last_lease_renewal_at = loop.time()
        consumer._running[case.work_id] = (reply_task, case.worker_id)
        consumer_task = asyncio.create_task(consumer._run())
        await asyncio.wait_for(consumer_checked_claim.wait(), timeout=2)
        await asyncio.wait_for(consumer_task, timeout=2)
        await asyncio.wait_for(reply_finished.wait(), timeout=2)
        assert reply_task.cancelled()

        async with external_work_database() as db:
            work = await db.get(SessionReplyWorkItem, case.work_id)
            task = await db.get(BackgroundTask, case.task_ids["call-1"])
            queued_row = await db.get(Message, case.result_message_ids["call-1"])
            messages = list((await db.execute(select(Message).where(Message.session_id == case.session_id, Message.uid == case.uid).order_by(Message.id))).scalars().all())
            stream_events = list((await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == case.work_id))).scalars().all())
            session_events = list(
                (
                    await db.execute(
                        select(SessionEvent).where(
                            SessionEvent.session_id == case.session_id,
                            SessionEvent.uid == case.uid,
                        )
                    )
                )
                .scalars()
                .all()
            )

        if invalidation == "deleted":
            assert work is None
        else:
            assert work is not None
            assert work.status == (SessionReplyWorkStatus.CANCELLED if invalidation == "cancelled" else SessionReplyWorkStatus.RUNNING)
            assert work.locked_by == (None if invalidation == "cancelled" else "other-worker")

        assert task is not None
        assert task.status == BackgroundTaskStatus.PENDING
        assert task.result is None
        assert queued_row is not None
        queued_after = InternalMessage.model_validate_json(queued_row.content or "{}")
        assert queued_after.model_dump() == queued_before.model_dump()
        assert not _messages_of_type(
            {"messages": messages},
            MessageType.BACKGROUND_TASK_RESULT,
        )
        assert stream_events == []
        assert session_events == []
    finally:
        release_llm.set()
        if consumer_task is not None and not consumer_task.done():
            consumer_task.cancel()
        if reply_task is not None and not reply_task.done():
            reply_task.cancel()
        pending_tasks = [task for task in (consumer_task, reply_task) if task is not None]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        consumer._running.clear()


@pytest.mark.asyncio
async def test_external_work_suspension_releases_lease_and_preserves_identity(external_work_database):
    case = await _create_case(external_work_database)
    waiting = await _snapshot(case)
    waiting_work = waiting["work"]
    assert waiting_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert waiting_work.locked_by is None
    assert waiting_work.lock_until is None
    assert waiting_work.result_message_id is None

    followup_id, followup_sequence = await _enqueue_followup(case)
    claimed_ids = await asyncio.gather(
        _claim_next(external_work_database, "followup-worker-a"),
        _claim_next(external_work_database, "followup-worker-b"),
    )
    assert [work_id for work_id in claimed_ids if work_id is not None] == [followup_id]

    await _complete_task(case, "call-1")
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"])

    resumed = await _snapshot(case)
    resumed_work = resumed["work"]
    assert resumed_work.id == case.work_id
    assert resumed_work.dedupe_key == case.dedupe_key
    assert resumed_work.sequence_no > followup_sequence > case.initial_sequence_no
    assert resumed_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert resumed_work.result_message_id is None
    assert resumed_work.locked_by is None
    assert resumed_work.lock_until is None
    assert await _claim_next(external_work_database, "same-session-worker") is None


@pytest.mark.asyncio
async def test_parallel_external_resume_is_idempotent_and_persists_protocol_events(external_work_database):
    case = await _create_case(external_work_database)
    await _complete_task(case, "call-1", "completed-once")

    resume_results = await asyncio.gather(
        external_work_module.resume_external_work_for_task(case.task_ids["call-1"]),
        external_work_module.resume_external_work_for_task(case.task_ids["call-1"]),
    )
    assert sorted(resume_results) == [False, True]

    snapshot = await _snapshot(case)
    work = snapshot["work"]
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert work.sequence_no > case.initial_sequence_no
    assert work.execution_state["external_resume_pending"] is True
    assert "pending_tools" not in work.execution_state

    _assert_queued_tool_result(snapshot, case, "call-1")
    result_row = next(message for message in snapshot["messages"] if message.id == case.result_message_ids["call-1"])
    assert result_row.id == case.result_message_ids["call-1"]

    markers = _messages_of_type(snapshot, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 1
    marker_payload = _assert_background_result_marker(markers[0], task="completed-once", execution_status="succeeded")
    assert marker_payload["type"] == "background_tool_result"

    tool_end_events = [event for event in snapshot["stream_events"] if event.event.get("type") == "tool_end"]
    assert len(tool_end_events) == 1
    assert tool_end_events[0].event["tool_call_id"] == "call-1"
    assert tool_end_events[0].event["result"] == "completed-once"

    ready_events = [event for event in snapshot["session_events"] if event.event.get("type") == "work_ready"]
    assert len(ready_events) == 1
    assert ready_events[0].event["work_id"] == case.work_id
    assert snapshot["tasks"][0].status == BackgroundTaskStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_external_resume_projects_tool_result_notification(external_work_database, show_tool_calls: bool):
    case = await _create_case(external_work_database)
    final_content = "completed-for-status-projection"

    async with external_work_database() as db:
        session = await db.get(ChatSession, case.session_id)
        assert session is not None
        session.show_tool_calls = show_tool_calls
        await db.commit()

    await _complete_task(case, "call-1", final_content)
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is True

    result_manager = SessionReplyResult()
    async with external_work_database() as db:
        first_status = await result_manager.get_work_status(db, case.work_id, uid=case.uid)
    first_snapshot = await _snapshot(case)

    assert first_snapshot["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    tool_result_rows = _messages_of_type(first_snapshot, MessageType.TOOL_RESULT)
    assert len(tool_result_rows) == 1
    original_tool_row = tool_result_rows[0]
    assert original_tool_row.id == case.result_message_ids["call-1"]
    _assert_queued_tool_result(first_snapshot, case, "call-1")

    markers = _messages_of_type(first_snapshot, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 1
    marker = markers[0]
    _assert_background_result_marker(marker, task=final_content, execution_status="succeeded")

    projected_events = [event for event in first_status.get("session_events", []) if event.get("type") == "tool_results_update"]
    persisted_events = [event for event in first_snapshot["session_events"] if event.event.get("type") == "tool_results_update"]
    if show_tool_calls:
        assert len(projected_events) == 1
        assert len(persisted_events) == 1
        projected_event = projected_events[0]
        projected_message = projected_event["messages"][0]
        assert projected_message["id"] == marker.id
        assert projected_message["db_id"] == marker.id
        assert projected_message["role"] == "assistant"
        assert projected_message["type"] == MessageType.BACKGROUND_TASK_RESULT.value
        assert projected_message["content"] == marker.content
        projected_payload = json.loads(projected_message["content"] or "{}")
        assert projected_payload["task"] == final_content
        assert projected_payload["execution_status"] == "succeeded"

        persisted_event = persisted_events[0].event
        assert persisted_event["type"] == projected_event["type"]
        assert persisted_event["session_id"] == projected_event["session_id"]
        assert persisted_event["work_id"] == projected_event["work_id"]
        assert persisted_event["messages"] == projected_event["messages"]
    else:
        assert not projected_events
        assert not persisted_events
        assert final_content not in json.dumps(first_status, ensure_ascii=False)
        assert final_content not in json.dumps([event.event for event in first_snapshot["session_events"]], ensure_ascii=False)

    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is False
    async with external_work_database() as db:
        second_status = await result_manager.get_work_status(db, case.work_id, uid=case.uid)
    second_snapshot = await _snapshot(case)

    assert second_status == first_status
    assert len(_messages_of_type(second_snapshot, MessageType.TOOL_RESULT)) == 1
    assert len(_messages_of_type(second_snapshot, MessageType.BACKGROUND_TASK_RESULT)) == 1
    assert len([event for event in second_snapshot["session_events"] if event.event.get("type") == "tool_results_update"]) == (1 if show_tool_calls else 0)


@pytest.mark.asyncio
async def test_reconcile_external_work_completes_task_before_suspension(external_work_database):
    case = await _create_case(external_work_database, suspend=False)
    await _complete_task(case, "call-1", "completed-before-wait")
    await _suspend_case(case)

    assert await external_work_module.reconcile_external_work() == 1
    assert await external_work_module.reconcile_external_work() == 0

    snapshot = await _snapshot(case)
    assert snapshot["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert snapshot["tasks"][0].status == BackgroundTaskStatus.SUCCEEDED
    _assert_queued_tool_result(snapshot, case, "call-1")
    markers = _messages_of_type(snapshot, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 1
    _assert_background_result_marker(markers[0], task="completed-before-wait", execution_status="succeeded")
    assert len([event for event in snapshot["session_events"] if event.event.get("type") == "work_ready"]) == 1


@pytest.mark.asyncio
async def test_external_resume_waits_for_all_tasks_in_one_round(external_work_database):
    case = await _create_case(external_work_database, call_ids=("call-1", "call-2"))
    await _complete_task(case, "call-1", "first-result")

    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is False
    partial = await _snapshot(case)
    assert partial["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert not _messages_of_type(partial, MessageType.BACKGROUND_TASK_RESULT)
    assert not [event for event in partial["stream_events"] if event.event.get("type") == "tool_end"]
    assert not [event for event in partial["session_events"] if event.event.get("type") == "work_ready"]
    _assert_queued_tool_result(partial, case, "call-1")

    await _complete_task(case, "call-2", "second-result")
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-2"]) is True
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is False

    complete = await _snapshot(case)
    assert complete["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    _assert_queued_tool_result(complete, case, "call-1")
    _assert_queued_tool_result(complete, case, "call-2")
    markers = _messages_of_type(complete, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 2
    assert {(json.loads(marker.content or "{}")["task"], json.loads(marker.content or "{}")["execution_status"]) for marker in markers} == {
        ("first-result", "succeeded"),
        ("second-result", "succeeded"),
    }
    assert len([event for event in complete["stream_events"] if event.event.get("type") == "tool_end"]) == 2
    assert len([event for event in complete["session_events"] if event.event.get("type") == "work_ready"]) == 1


@pytest.mark.asyncio
async def test_recover_interrupted_external_work_marks_unknown_and_is_idempotent(external_work_database):
    case = await _create_case(external_work_database)

    assert await external_work_module.recover_interrupted_external_work() == 1
    first = await _snapshot(case)
    work = first["work"]
    task = first["tasks"][0]
    stored_result = _assert_queued_tool_result(first, case, "call-1")
    assert task.status == BackgroundTaskStatus.FAILED
    assert task.result["status"] == "execution_unknown"
    assert task.extra["execution_unknown"] is True
    assert task.extra["restart_interrupted"] is True
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None
    assert work.lock_until is None
    markers = _messages_of_type(first, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 1
    _assert_background_result_marker(
        markers[0],
        task={"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED},
        execution_status="execution_unknown",
    )

    async with external_work_database() as db:
        assert (
            await background_task_crud.try_claim(
                db,
                task_id=task.id,
                worker_id="must-not-claim",
            )
            is None
        )
        assert (
            await session_reply_work_item_crud.claim_next(
                db,
                worker_id="must-not-claim-work",
                lease_seconds=300,
            )
            is None
        )

    marker_count = len(_messages_of_type(first, MessageType.BACKGROUND_TASK_RESULT))
    ready_count = len([event for event in first["session_events"] if event.event.get("type") == "work_ready"])
    tool_end_count = len([event for event in first["stream_events"] if event.event.get("type") == "tool_end"])
    assert await external_work_module.recover_interrupted_external_work() == 0
    second = await _snapshot(case)
    assert len(_messages_of_type(second, MessageType.BACKGROUND_TASK_RESULT)) == marker_count
    assert len([event for event in second["session_events"] if event.event.get("type") == "work_ready"]) == ready_count
    assert len([event for event in second["stream_events"] if event.event.get("type") == "tool_end"]) == tool_end_count
    assert _assert_queued_tool_result(second, case, "call-1").model_dump() == stored_result.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["owner", "origin"])
async def test_external_resume_rejects_owner_or_origin_mismatch(external_work_database, mismatch: str):
    case = await _create_case(external_work_database)
    await _complete_task(case, "call-1", "must-remain-queued")

    async with external_work_database() as db:
        task = await db.get(BackgroundTask, case.task_ids["call-1"])
        assert task is not None
        if mismatch == "owner":
            task.uid = "different-owner"
        else:
            task.extra = {**task.extra, "origin_work_identity": "wrong-origin-identity"}
        await db.commit()

    before = await _snapshot(case)
    before_result = next(message for message in before["messages"] if message.id == case.result_message_ids["call-1"])
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is False
    after = await _snapshot(case)
    after_result = next(message for message in after["messages"] if message.id == case.result_message_ids["call-1"])
    assert after["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert after["work"].locked_by is None
    assert after_result.id == before_result.id
    assert after_result.content == before_result.content
    assert not _messages_of_type(after, MessageType.BACKGROUND_TASK_RESULT)
    assert not [event for event in after["stream_events"] if event.event.get("type") == "tool_end"]
    assert not [event for event in after["session_events"] if event.event.get("type") == "work_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "resume_before_delivery", "remaining_task"),
    [
        ("running", False, False),
        ("failed", False, False),
        ("failed", True, False),
        ("succeeded", False, False),
        ("succeeded", True, False),
        ("succeeded", False, True),
    ],
    ids=[
        "running-not-resumed",
        "failed-not-resumed",
        "failed-resumed",
        "succeeded-not-resumed",
        "succeeded-resumed",
        "succeeded-with-remaining-task",
    ],
)
async def test_cancel_reply_delivery_cancels_original_external_work(
    external_work_database,
    monkeypatch: pytest.MonkeyPatch,
    task_status: str,
    resume_before_delivery: bool,
    remaining_task: bool,
) -> None:
    case = await _create_case(external_work_database, call_ids=("call-1", "call-remaining") if remaining_task else ("call-1",))
    unrelated_case = await _create_case(external_work_database, call_ids=("call-2",))
    task_id = case.task_ids["call-1"]

    if task_status == "running":
        async with external_work_database() as db:
            claimed = await background_task_crud.try_claim(
                db,
                task_id=task_id,
                worker_id=f"external-task-worker-{task_id}",
                lease_seconds=3600,
            )
            assert claimed is not None
            assert claimed.status == BackgroundTaskStatus.RUNNING
    elif task_status == "failed":
        await _fail_task(case, "call-1", error="failed-before-cancel", content="failed-result-before-cancel")
    else:
        await _complete_task(case, "call-1", "succeeded-result-before-cancel")

    before = await _snapshot(case)
    before_task = next(task for task in before["tasks"] if task.id == task_id)
    before_task_result = before_task.result
    before_task_error = before_task.error
    before_remaining_task = next((task for task in before["tasks"] if task.id == case.task_ids.get("call-remaining")), None)
    assert before["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    if remaining_task:
        assert before_remaining_task is not None
        assert before_remaining_task.status == BackgroundTaskStatus.PENDING

    stop_work_id, _ = await _enqueue_followup(case)
    assert await _claim_next(external_work_database, "stop-worker") == stop_work_id

    if resume_before_delivery:
        assert task_status != "running"
        assert await external_work_module.resume_external_work_for_task(task_id) is (not remaining_task)
        resumed_before_delivery = await _snapshot(case)
        assert resumed_before_delivery["work"].status == (SessionReplyWorkStatus.WAITING_EXTERNAL_WORK if remaining_task else SessionReplyWorkStatus.READY_FOR_LLM)

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", external_work_database)

    async def execute_foreground(db, work, _worker_id):
        cancel_executor = ManageBackgroundTasksExecutor(project_root=".", uid=work.uid)
        cancel_executor.set_runtime_context(db=db, session_id=work.session_id)
        cancel_response = json.loads(await cancel_executor.execute(operation="cancel", task_id=task_id))
        assert cancel_response["status"] == "success"
        assert cancel_response["operation"] == "cancel"
        await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="stop response"),
            work.profile_id,
            dedupe_key=executor_common_module._result_message_dedupe_key(work),
            commit=False,
        )
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "stop response"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    delivered_events: list[dict[str, Any]] = []

    async def capture_session_event(_uid: str, _session_id: str, event: dict[str, Any]) -> None:
        delivered_events.append(event)

    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)

    await executor_lifecycle_module.execute_session_reply_work(stop_work_id, "stop-worker")

    assert delivered_events and delivered_events[-1]["work_id"] == stop_work_id
    after = await _snapshot(case)
    assert after["work"].status == (SessionReplyWorkStatus.WAITING_EXTERNAL_WORK if remaining_task else SessionReplyWorkStatus.CANCELLED)
    assert await _claim_next(external_work_database, "must-not-claim") is None
    assert await external_work_module.resume_external_work_for_task(task_id) is False

    task = next(task for task in after["tasks"] if task.id == task_id)
    expected_task_status = BackgroundTaskStatus.CANCELLED if task_status == "running" else BackgroundTaskStatus(task_status)
    assert task.status == expected_task_status
    assert task.result == before_task_result
    assert task.error == before_task_error
    _assert_queued_tool_result(after, case, "call-1")
    if remaining_task:
        remaining_task_row = next(task for task in after["tasks"] if task.id == case.task_ids["call-remaining"])
        assert remaining_task_row.status == BackgroundTaskStatus.PENDING
        assert remaining_task_row.result == before_remaining_task.result
        assert remaining_task_row.error == before_remaining_task.error

    async with external_work_database() as db:
        stop_work = await db.get(SessionReplyWorkItem, stop_work_id)
    assert stop_work is not None
    assert stop_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert stop_work.event_sent is True

    assistant_texts = [message for message in after["messages"] if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(assistant_texts) == 1
    assert assistant_texts[0].content == "stop response"

    if remaining_task:
        await _complete_task(case, "call-remaining", "remaining-result")
        assert await external_work_module.resume_external_work_for_task(case.task_ids["call-remaining"]) is True
        resumed = await _snapshot(case)
        assert resumed["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
        _assert_queued_tool_result(resumed, case, "call-1")
        _assert_queued_tool_result(resumed, case, "call-remaining")
        completed_remaining_task = next(task for task in resumed["tasks"] if task.id == case.task_ids["call-remaining"])
        assert completed_remaining_task.status == BackgroundTaskStatus.SUCCEEDED
        assert completed_remaining_task.result == {"content": "remaining-result"}
        assert completed_remaining_task.error is None

    unrelated = await _snapshot(unrelated_case)
    assert unrelated["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert unrelated["work"].locked_by is None
    _assert_queued_tool_result(unrelated, unrelated_case, "call-2")
    unrelated_task = next(task for task in unrelated["tasks"] if task.id == unrelated_case.task_ids["call-2"])
    assert unrelated_task.status == BackgroundTaskStatus.PENDING


@pytest.mark.asyncio
async def test_cancel_completed_external_work_waits_for_stop_reply_delivery(
    external_work_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _create_case(external_work_database)
    task_id = case.task_ids["call-1"]
    await _complete_task(case, "call-1", "completed-before-transport-failure")
    before = await _snapshot(case)
    before_task = next(task for task in before["tasks"] if task.id == task_id)

    stop_work_id, _ = await _enqueue_followup(case)
    assert await _claim_next(external_work_database, "stop-worker") == stop_work_id

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", external_work_database)

    async def execute_foreground(db, work, _worker_id):
        cancel_executor = ManageBackgroundTasksExecutor(project_root=".", uid=work.uid)
        cancel_executor.set_runtime_context(db=db, session_id=work.session_id)
        cancel_response = json.loads(await cancel_executor.execute(operation="cancel", task_id=task_id))
        assert cancel_response["status"] == "success"
        await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="stop response after failed delivery"),
            work.profile_id,
            dedupe_key=executor_common_module._result_message_dedupe_key(work),
            commit=False,
        )
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "stop response after failed delivery"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    async def fail_session_event(_uid: str, _session_id: str, _event: dict[str, Any]) -> None:
        raise RuntimeError("stop reply transport failed")

    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", fail_session_event)

    with pytest.raises(RuntimeError, match="stop reply transport failed"):
        await executor_lifecycle_module.execute_session_reply_work(stop_work_id, "stop-worker")

    waiting = await _snapshot(case)
    assert waiting["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    task = next(task for task in waiting["tasks"] if task.id == task_id)
    assert task.status == BackgroundTaskStatus.SUCCEEDED
    assert task.result == before_task.result
    assert task.error == before_task.error

    async with external_work_database() as db:
        stop_work = await db.get(SessionReplyWorkItem, stop_work_id)
    assert stop_work is not None
    assert stop_work.status == SessionReplyWorkStatus.RUNNING
    assert stop_work.event_sent is False

    assert await external_work_module.resume_external_work_for_task(task_id) is True
    resumed = await _snapshot(case)
    assert resumed["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    _assert_queued_tool_result(resumed, case, "call-1")
