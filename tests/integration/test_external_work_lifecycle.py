import asyncio
import json
from dataclasses import dataclass
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
from app.core.session_reply_queue.manager_result import SessionReplyResult
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


def _messages_of_type(snapshot: dict[str, Any], message_type: MessageType) -> list[Message]:
    return [message for message in snapshot["messages"] if message.type == message_type]


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

    result_row = next(message for message in snapshot["messages"] if message.id == case.result_message_ids["call-1"])
    stored_result = InternalMessage.model_validate_json(result_row.content or "{}")
    assert result_row.id == case.result_message_ids["call-1"]
    assert stored_result.tool_call_id == "call-1"
    assert stored_result.content == "completed-once"
    assert stored_result.tool_execution_status == "succeeded"

    markers = _messages_of_type(snapshot, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 1
    marker_payload = json.loads(markers[0].content or "{}")
    assert marker_payload["type"] == "background_tool_result"
    assert marker_payload["task"] == "completed-once"

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
    stored_result = InternalMessage.model_validate_json(original_tool_row.content or "{}")
    assert stored_result.tool_call_id == case.tool_calls[0].id
    assert stored_result.content == final_content

    projected_events = [event for event in first_status.get("session_events", []) if event.get("type") == "tool_results_update"]
    persisted_events = [event for event in first_snapshot["session_events"] if event.event.get("type") == "tool_results_update"]
    if show_tool_calls:
        assert len(projected_events) == 1
        assert len(persisted_events) == 1
        projected_event = projected_events[0]
        projected_message = projected_event["messages"][0]
        assert projected_message["id"] == original_tool_row.id
        assert projected_message["db_id"] == original_tool_row.id
        assert projected_message["content"] == original_tool_row.content
        projected_result = InternalMessage.model_validate_json(projected_message["content"] or "{}")
        assert projected_result.tool_call_id == case.tool_calls[0].id
        assert projected_result.content == final_content

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
    assert len(_messages_of_type(snapshot, MessageType.BACKGROUND_TASK_RESULT)) == 1
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
    assert _tool_result(partial, case.result_message_ids["call-1"]).tool_execution_status is None

    await _complete_task(case, "call-2", "second-result")
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-2"]) is True
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is False

    complete = await _snapshot(case)
    assert complete["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert len(_messages_of_type(complete, MessageType.BACKGROUND_TASK_RESULT)) == 2
    assert len([event for event in complete["stream_events"] if event.event.get("type") == "tool_end"]) == 2
    assert len([event for event in complete["session_events"] if event.event.get("type") == "work_ready"]) == 1
    assert _tool_result(complete, case.result_message_ids["call-1"]).tool_execution_status == "succeeded"
    assert _tool_result(complete, case.result_message_ids["call-2"]).tool_execution_status == "succeeded"


@pytest.mark.asyncio
async def test_recover_interrupted_external_work_marks_unknown_and_is_idempotent(external_work_database):
    case = await _create_case(external_work_database)

    assert await external_work_module.recover_interrupted_external_work() == 1
    first = await _snapshot(case)
    work = first["work"]
    task = first["tasks"][0]
    stored_result = _tool_result(first, case.result_message_ids["call-1"])
    assert task.status == BackgroundTaskStatus.FAILED
    assert task.result["status"] == "execution_unknown"
    assert task.extra["execution_unknown"] is True
    assert task.extra["restart_interrupted"] is True
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None
    assert work.lock_until is None
    assert stored_result.tool_execution_status == "execution_unknown"
    assert json.loads(stored_result.content or "{}") == {
        "status": "execution_unknown",
        "error": PROMPT_TOOL_INTERRUPTED,
    }

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
    assert _tool_result(second, case.result_message_ids["call-1"]).model_dump() == stored_result.model_dump()


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
