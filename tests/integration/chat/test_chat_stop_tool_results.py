import asyncio
import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.session.tool_cancellation as tool_cancellation_module
from app.core.constants import (
    ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.event import session_event_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.utils.context_summary.boundary import ContextSummaryTriggerMode, resolve_context_summary_boundary
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot, iter_persistent_summary_rounds
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.chat.test_chat_stop_workflow import (
    _build_app,
    _make_work,
    _patch_queue_runtime_database,
    _seed_tool_call_stop_case,
)
from tests.integration.chat.test_chat_stop_workflow import (
    db_session as _db_session,
)

db_session = _db_session


async def _append_stop_user_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    content: str,
) -> int:
    async with session_factory() as db:
        message = Message(session_id=session_id, uid="owner", profile_id=1, role=MessageRole.USER, type=MessageType.TEXT, content=content, is_processed=False)
        db.add(message)
        await db.commit()
        assert message.id is not None
        return message.id


async def _load_tool_stop_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
) -> tuple[ChatSession, list[Message], list[SessionEvent]]:
    async with session_factory() as db:
        session = await db.get(ChatSession, session_id)
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all())
    assert session is not None
    return session, messages, events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_started",
    [False, True],
    ids=["pending-task", "started-task"],
)
async def test_http_stop_preserves_queued_background_tool_result_and_adds_cancel_notification(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    task_started: bool,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    session_id = f"tool-call-stop-background-{task_started}"
    case = await _seed_tool_call_stop_case(
        db_session,
        session_id=session_id,
        stream_requested=False,
        show_tool_calls=True,
    )
    queued_call = case["tool_calls"][1]

    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, case["work_id"])
        assert work is not None and work.id is not None
        origin_work_id = work.id
        origin_work_identity = work.dedupe_key
        task = await background_task_crud.create_task(
            db,
            uid="owner",
            session_id=session_id,
            profile_id=1,
            tool_call_id=queued_call.id,
            tool_name=queued_call.name,
            arguments=queued_call.arguments,
            auto_reply=False,
            extra={
                "origin_work_id": origin_work_id,
                "origin_work_identity": origin_work_identity,
            },
        )
        assert task.id is not None
        task_id = task.id
        result = await db.execute(select(Message).where(Message.session_id == session_id, Message.type == MessageType.TOOL_RESULT).order_by(Message.id))
        queued_result = next(message for message in result.scalars().all() if InternalMessage.model_validate_json(message.content or "{}").tool_call_id == queued_call.id)
        assert queued_result.id is not None
        queued_result_id = queued_result.id
        original_tool_result_content = queued_result.content
        queued_payload = json.loads(InternalMessage.model_validate_json(queued_result.content or "{}").content or "{}")
        assert queued_payload == {"status": "queued", "task_id": task_id}
        if task_started:
            assert await background_task_crud.try_claim(db, task_id=task_id, worker_id="background-task-worker") is not None

    app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
        reply_status = await client.get(f"/api/v1/chat/reply-works/{origin_work_id}")
        history = await client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": session_id, "page": 1, "size": 100},
        )
        repeated = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
        repeated_history = await client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": session_id, "page": 1, "size": 100},
        )

    assert stopped.status_code == 200
    assert stopped.json()["code"] == 200
    assert stopped.json()["data"] == {"session_id": session_id, "cancelled_count": 1}
    assert repeated.status_code == 200
    assert repeated.json()["code"] == 200
    assert repeated.json()["data"] == {"session_id": session_id, "cancelled_count": 0}
    history_data = history.json()["data"]
    assert repeated_history.json()["data"] == history_data

    _, messages, events = await _load_tool_stop_state(db_session, session_id=session_id)
    markers = [message for message in messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
    assert len(markers) == 1
    marker = markers[0]
    history_marker = next(item for item in history_data if item["type"] == MessageType.BACKGROUND_TASK_RESULT.value)
    assert history_marker["id"] == marker.id
    assert history_marker["content"] == marker.content
    assert reply_status.status_code == 200
    reply_data = reply_status.json()["data"]
    assert reply_data["status"] == SessionReplyWorkStatus.CANCELLED.value
    reply_event = next(item for item in reply_data["session_events"] if item["type"] == "tool_results_update")
    reply_marker = next(item for item in reply_event["messages"] if item["id"] == marker.id)
    assert reply_marker["content"] == marker.content

    assert len(events) == 1
    event = events[0].event
    assert event["type"] == "tool_results_update"
    event_marker = next(message for message in event["messages"] if message["id"] == marker.id)
    assert event_marker["content"] == marker.content

    expected_execution_status = "execution_unknown" if task_started else "failed"
    marker_payload = json.loads(marker.content or "{}")
    assert marker_payload["type"] == "background_tool_result"
    assert marker_payload["task_id"] == task_id
    assert marker_payload["original_tool_result_message_id"] == queued_result_id
    assert marker_payload["tool_cancelled"] is True
    assert marker_payload["execution_status"] == expected_execution_status
    assert marker_payload["tool_call"] == {
        "id": queued_call.id,
        "name": queued_call.name,
        "arguments": queued_call.arguments,
    }
    assert marker_payload["task"]["status"] == "cancelled"

    queued_results = []
    for message in messages:
        if message.type == MessageType.TOOL_RESULT and InternalMessage.model_validate_json(message.content or "{}").tool_call_id == queued_call.id:
            queued_results.append(message)
    assert len(queued_results) == 1
    assert queued_results[0].id == queued_result_id
    assert queued_results[0].content == original_tool_result_content

    async with db_session() as db:
        cancelled_task = await db.get(BackgroundTask, task_id)
    assert cancelled_task is not None
    assert cancelled_task.status == BackgroundTaskStatus.CANCELLED
    assert cancelled_task.extra["origin_work_id"] == origin_work_id
    assert cancelled_task.extra["origin_work_identity"] == origin_work_identity
    assert cancelled_task.tool_call_id == queued_call.id
    assert cancelled_task.tool_name == queued_call.name
    assert cancelled_task.arguments == queued_call.arguments
    assert (cancelled_task.started_at is not None) is task_started

    parsed_marker = parse_db_messages_to_internal([marker])
    assert len(parsed_marker) == 1
    parsed_payload = json.loads(parsed_marker[0].content or "{}")
    assert parsed_payload["tool_call_id"] == queued_call.id
    assert parsed_payload["tool_name"] == queued_call.name
    assert parsed_payload["status"] == "cancelled"
    assert parsed_payload["execution_status"] == expected_execution_status
    assert "instruction" not in parsed_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_started",
    [False, True],
    ids=["pending-task", "started-task"],
)
async def test_http_stop_fills_missing_bound_background_tool_result(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    task_started: bool,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    session_id = f"tool-call-stop-missing-background-{task_started}"
    case = await _seed_tool_call_stop_case(
        db_session,
        session_id=session_id,
        stream_requested=False,
        show_tool_calls=True,
        save_queued_result=False,
    )
    queued_call = case["tool_calls"][1]
    current_user_id = await _append_stop_user_message(
        db_session,
        session_id=session_id,
        content="current user message before stop",
    )

    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, case["work_id"])
        assert work is not None and work.id is not None
        origin_work_id = work.id
        origin_work_identity = work.dedupe_key
        task = await background_task_crud.create_task(
            db,
            uid="owner",
            session_id=session_id,
            profile_id=1,
            tool_call_id=queued_call.id,
            tool_name=queued_call.name,
            arguments=queued_call.arguments,
            auto_reply=False,
            extra={
                "origin_work_id": origin_work_id,
                "origin_work_identity": origin_work_identity,
            },
        )
        assert task.id is not None
        task_id = task.id
        if task_started:
            assert await background_task_crud.try_claim(db, task_id=task_id, worker_id="background-task-worker") is not None

    _, before_messages, _ = await _load_tool_stop_state(db_session, session_id=session_id)
    assistant_before = next(message for message in before_messages if message.type == MessageType.TOOL_CALL)
    successful_before = next(message for message in before_messages if message.type == MessageType.TOOL_RESULT and InternalMessage.model_validate_json(message.content or "{}").tool_call_id == case["tool_calls"][0].id)
    original_successful_content = successful_before.content
    previous_user = next(message for message in before_messages if message.content == "hold this reply")

    async with db_session() as db:
        before_boundary = await resolve_context_summary_boundary(
            db,
            session_id=session_id,
            uid="owner",
            expected_summary_message_id=None,
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=current_user_id,
        )
    assert before_boundary.target_message_id is None

    app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
        reply_status = await client.get(f"/api/v1/chat/reply-works/{case['work_id']}")

    assert stopped.status_code == 200
    assert stopped.json()["data"] == {"session_id": session_id, "cancelled_count": 1}
    assert reply_status.status_code == 200
    reply_data = reply_status.json()["data"]
    assert reply_data["status"] == SessionReplyWorkStatus.CANCELLED.value

    _, messages, _ = await _load_tool_stop_state(db_session, session_id=session_id)
    assistant_after = next(message for message in messages if message.type == MessageType.TOOL_CALL)
    successful_after = [message for message in messages if message.type == MessageType.TOOL_RESULT and InternalMessage.model_validate_json(message.content or "{}").tool_call_id == case["tool_calls"][0].id]
    cancelled_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT and InternalMessage.model_validate_json(message.content or "{}").tool_call_id == queued_call.id]
    cancelled_tool_call_ids = {json.loads(message.content or "{}").get("tool_call_id") for message in messages if message.type == MessageType.TOOL_RESULT and json.loads(message.content or "{}").get("tool_cancelled") is True}
    assert assistant_after.content == assistant_before.content
    assert len(successful_after) == 1
    assert successful_after[0].content == original_successful_content
    assert cancelled_tool_call_ids == {queued_call.id, case["tool_calls"][2].id}
    assert len(cancelled_rows) == 1
    cancelled_row = cancelled_rows[0]
    assert assistant_after.id is not None
    assert cancelled_row.provider_metadata == {"tool_cancellation_source_message_id": assistant_after.id}

    expected_execution_status = "execution_unknown" if task_started else "failed"
    expected_error = PROMPT_TOOL_INTERRUPTED if task_started else t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION)
    cancelled_payload = json.loads(cancelled_row.content or "{}")
    assert cancelled_payload["tool_call_id"] == queued_call.id
    assert cancelled_payload["tool_cancelled"] is True
    assert cancelled_payload["tool_execution_status"] == expected_execution_status
    assert json.loads(cancelled_payload["content"]) == {
        "type": "tool_cancellation",
        "status": "cancelled",
        "error": expected_error,
    }

    async with db_session() as db:
        cancelled_task = await db.get(BackgroundTask, task_id)
    assert cancelled_task is not None
    assert cancelled_task.uid == "owner"
    assert cancelled_task.session_id == session_id
    assert cancelled_task.profile_id == 1
    assert cancelled_task.tool_call_id == queued_call.id
    assert cancelled_task.status == BackgroundTaskStatus.CANCELLED
    assert cancelled_task.extra["origin_work_id"] == origin_work_id
    assert cancelled_task.extra["origin_work_identity"] == origin_work_identity
    assert (cancelled_task.started_at is not None) is task_started

    reply_events = [event for event in reply_data["session_events"] if event["type"] == "tool_results_update"]
    assert len(reply_events) == 1
    reply_messages = reply_events[0]["messages"]
    assert len(reply_messages) == 2
    assert {json.loads(message["content"] or "{}").get("tool_call_id") for message in reply_messages} == {queued_call.id, case["tool_calls"][2].id}
    target_reply_messages = [message for message in reply_messages if json.loads(message["content"] or "{}").get("tool_call_id") == queued_call.id]
    assert len(target_reply_messages) == 1
    reply_message = target_reply_messages[0]
    assert reply_message["content"] == cancelled_row.content
    reply_payload = json.loads(reply_message["content"])
    assert reply_payload["tool_call_id"] == queued_call.id
    assert reply_payload["tool_cancelled"] is True
    assert reply_payload["tool_execution_status"] == expected_execution_status

    async with db_session() as db:
        after_boundary = await resolve_context_summary_boundary(
            db,
            session_id=session_id,
            uid="owner",
            expected_summary_message_id=None,
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=current_user_id,
        )
        assert after_boundary.target_message_id is not None
        snapshot = await build_context_summary_snapshot(
            db,
            session_id=session_id,
            uid="owner",
            expected_summary_message_id=None,
            before_id=None,
            target_message_id=after_boundary.target_message_id,
            late_cancelled_tool_results=after_boundary.late_cancelled_tool_results,
        )
        summary_rounds = [
            round_messages
            async for round_messages in iter_persistent_summary_rounds(
                db,
                session_id=session_id,
                uid="owner",
                snapshot=snapshot,
            )
        ]

    assert after_boundary.target_message_id == successful_after[0].id
    assert after_boundary.target_message_id < current_user_id
    assert after_boundary.covered_user_message_id == previous_user.id
    late_results = after_boundary.late_cancelled_tool_results
    assert len(late_results) == 2
    assert {result.tool_call_id for _source_id, result in late_results} == {queued_call.id, case["tool_calls"][2].id}
    target_late_results = [(source_id, result) for source_id, result in late_results if result.tool_call_id == queued_call.id]
    assert len(target_late_results) == 1
    late_source_id, late_result = target_late_results[0]
    assert late_source_id == assistant_after.id
    assert late_result.id == cancelled_row.id
    assert late_result.tool_call_id == queued_call.id
    assert late_result.tool_execution_status == expected_execution_status
    assert json.loads(late_result.content or "{}") == {
        "type": "tool_cancellation",
        "status": "cancelled",
        "error": expected_error,
    }

    summary_messages = [message for round_messages in summary_rounds for message in round_messages]
    summary_cancelled = [message for message in summary_messages if message.tool_call_id == queued_call.id]
    assert len(summary_cancelled) == 1
    assert summary_cancelled[0].tool_execution_status == expected_execution_status
    assert json.loads(summary_cancelled[0].content or "{}") == json.loads(late_result.content or "{}")
    assert all(message.id != current_user_id for message in summary_messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_requested", "show_tool_calls"),
    [(False, False), (False, True), (True, False), (True, True)],
    ids=["non-stream-hidden", "non-stream-visible", "stream-hidden", "stream-visible"],
)
async def test_http_stop_fills_missing_tool_results(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    stream_requested: bool,
    show_tool_calls: bool,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    case = await _seed_tool_call_stop_case(
        db_session,
        session_id=f"tool-call-stop-{stream_requested}-{show_tool_calls}",
        stream_requested=stream_requested,
        show_tool_calls=show_tool_calls,
    )
    session_id = case["session_id"]
    work_id = case["work_id"]
    tool_calls = case["tool_calls"]
    missing_call_id = tool_calls[2].id

    before_user_id = await _append_stop_user_message(
        db_session,
        session_id=session_id,
        content="user message before cancellation",
    )
    async with db_session() as db:
        before_boundary = await resolve_context_summary_boundary(
            db,
            session_id=session_id,
            uid="owner",
            expected_summary_message_id=None,
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=before_user_id,
        )
        before_rows = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
    assistant_before = next(row for row in before_rows if row.type == MessageType.TOOL_CALL)
    original_result_contents = {InternalMessage.model_validate_json(row.content or "{}").tool_call_id: row.content for row in before_rows if row.type == MessageType.TOOL_RESULT}
    assert before_boundary.target_message_id is None

    app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["cancelled_count"] == 1

    after_user_id = await _append_stop_user_message(
        db_session,
        session_id=session_id,
        content="user message after cancellation",
    )
    async with db_session() as db:
        after_boundary = await resolve_context_summary_boundary(
            db,
            session_id=session_id,
            uid="owner",
            expected_summary_message_id=None,
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=after_user_id,
        )
        raw_rows = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        assert after_boundary.target_message_id is not None

        snapshot = await build_context_summary_snapshot(
            db,
            session_id=session_id,
            uid="owner",
            expected_summary_message_id=None,
            before_id=None,
            target_message_id=after_boundary.target_message_id,
        )
        summary_rounds = [
            round_messages
            async for round_messages in iter_persistent_summary_rounds(
                db,
                session_id=session_id,
                uid="owner",
                snapshot=snapshot,
            )
        ]

    parsed_rows = parse_db_messages_to_internal(raw_rows)
    tool_results = {message.tool_call_id: message for message in parsed_rows if message.role == MessageRole.TOOL and message.tool_call_id is not None}
    assert set(tool_results) == {tool_call.id for tool_call in tool_calls}
    cancelled_result = tool_results[missing_call_id]
    cancellation_row = next(row for row in raw_rows if row.type == MessageType.TOOL_RESULT and json.loads(row.content or "{}").get("tool_call_id") == missing_call_id)
    assert after_boundary.target_message_id == cancellation_row.id
    assert cancelled_result.tool_execution_status == "execution_unknown"
    cancelled_body = json.loads(cancelled_result.content or "{}")
    assert cancelled_body["status"] == "cancelled"
    assert isinstance(cancelled_body.get("error"), str) and cancelled_body["error"]

    assistant_rows = [row for row in raw_rows if row.type == MessageType.TOOL_CALL]
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == assistant_before.content
    after_result_contents = {InternalMessage.model_validate_json(row.content or "{}").tool_call_id: row.content for row in raw_rows if row.type == MessageType.TOOL_RESULT}
    assert {call_id: after_result_contents[call_id] for call_id in original_result_contents} == original_result_contents

    summary_messages = [message for round_messages in summary_rounds for message in round_messages]
    summary_cancelled = next(message for message in summary_messages if message.tool_call_id == missing_call_id)
    assert summary_cancelled.tool_execution_status == "execution_unknown"
    assert json.loads(summary_cancelled.content or "{}")["status"] == "cancelled"

    late_messages: list[InternalMessage] = []
    late_turn_messages: list[InternalMessage] = []
    async with db_session() as late_db:
        late_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, case["worker_id"])
        with pytest.raises(asyncio.CancelledError):
            await save_tool_response(
                late_db,
                session_id,
                "owner",
                1,
                InternalMessage(
                    role=MessageRole.TOOL,
                    type=MessageType.TOOL_RESULT,
                    tool_call_id=missing_call_id,
                    tool_execution_status="succeeded",
                    content=json.dumps({"status": "succeeded", "late": True}),
                ),
                late_messages,
                late_turn_messages,
            )
    assert late_messages == []
    assert late_turn_messages == []

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        reply_status = await client.get(f"/api/v1/chat/reply-works/{work_id}")
        history = await client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": session_id, "page": 1, "size": 100},
        )
        repeated = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
        repeated_status = await client.get(f"/api/v1/chat/reply-works/{work_id}")
        repeated_history = await client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": session_id, "page": 1, "size": 100},
        )
    reply_data = reply_status.json()["data"]
    assert reply_data["status"] == SessionReplyWorkStatus.CANCELLED.value
    assert repeated.json()["data"]["cancelled_count"] == 0
    assert repeated_status.json()["data"] == reply_data
    assert repeated_history.json()["data"] == history.json()["data"]

    history_results = [item for item in history.json()["data"] if item["type"] == MessageType.TOOL_RESULT.value]
    final_session, final_messages, final_events = await _load_tool_stop_state(
        db_session,
        session_id=session_id,
    )
    assert (
        final_session.context_summary,
        final_session.context_summary_revision,
        final_session.context_content_revision,
    ) == ("summary before stop", 7, 11)
    final_tool_results = [row for row in final_messages if row.type == MessageType.TOOL_RESULT]
    assert len(final_tool_results) == len(tool_calls)

    if show_tool_calls:
        assert len(final_events) == 1
        event = final_events[0].event
        assert event["type"] == "tool_results_update"
        assert event["work_id"] == work_id
        event_message = event["messages"][0]
        assert event_message["tool_call_id"] == missing_call_id
        assert event_message["content"] == cancellation_row.content
        event_wrapper = json.loads(event_message["content"])
        assert event_wrapper["tool_call_id"] == missing_call_id
        assert event_wrapper["tool_execution_status"] == "execution_unknown"
        assert json.loads(event_wrapper["content"])["status"] == "cancelled"
        assert reply_data["session_events"][0]["type"] == "tool_results_update"
        reply_wrapper = json.loads(reply_data["session_events"][0]["messages"][0]["content"])
        assert reply_wrapper["tool_call_id"] == missing_call_id
        history_cancelled = next(json.loads(item["content"]) for item in history_results if json.loads(item["content"]).get("tool_call_id") == missing_call_id)
        assert len(history_results) == len(tool_calls)
        assert history_cancelled["tool_execution_status"] == "execution_unknown"
        assert json.loads(history_cancelled["content"])["status"] == "cancelled"
    else:
        assert final_events == []
        assert "session_events" not in reply_data
        assert history_results == []


async def _seed_agent_work_tool_call_case(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    session_id = "agent-work-stop-isolation"
    worker_id = "agent-work-stop-worker"
    async with session_factory() as db:
        db.add(ChatSession(session_id=session_id, uid="owner", profile_id=1, source="http", reply_target_source="http"))
        work = _make_work(
            session_id=session_id,
            uid="owner",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.AGENT_WORK,
            source_type=SessionReplySourceType.PARENT_WORK,
            source_id="parent-work",
            dedupe_key="agent-work-stop-isolation-work",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by=worker_id,
            lock_until=2_000_000_000,
            request_id="agent-work-stop-isolation-request",
            attempt_count=1,
        )
        db.add(work)
        await db.commit()
        assert work.id is not None
        work_id = work.id

    async with session_factory() as db:
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)
        await save_assistant_message(
            db,
            session_id,
            "owner",
            1,
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content="independent tool round",
                tool_calls=[InternalToolCall(id="agent-call-missing", name="agent_tool", arguments={"value": 1})],
            ),
            dedupe_key="agent-work-stop-isolation-assistant",
        )
    return {
        "session_id": session_id,
        "work_id": work_id,
        "tool_call_id": "agent-call-missing",
    }


@pytest.mark.asyncio
async def test_http_stop_isolates_agent_work_tool_results(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    case = await _seed_agent_work_tool_call_case(db_session)
    session_id = case["session_id"]
    work_id = case["work_id"]
    tool_call_id = case["tool_call_id"]
    app = _build_app(db_session, {"uid": "owner", "is_superuser": False})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
        history = await client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": session_id, "page": 1, "size": 100},
        )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["cancelled_count"] == 1
    assert history.json()["data"] == []

    _session, messages, events = await _load_tool_stop_state(db_session, session_id=session_id)
    assert len(messages) == 2
    assert all(message.work_id == work_id for message in messages)
    result = InternalMessage.model_validate_json(next(row for row in messages if row.type == MessageType.TOOL_RESULT).content or "{}")
    assert result.tool_call_id == tool_call_id
    assert result.tool_execution_status == "execution_unknown"
    assert json.loads(result.content or "{}")["status"] == "cancelled"
    assert events == []


@pytest.mark.asyncio
async def test_http_stop_rolls_back_tool_result_event(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    case = await _seed_tool_call_stop_case(
        db_session,
        session_id="tool-call-stop-event-rollback",
        stream_requested=False,
        show_tool_calls=True,
    )
    session_id = case["session_id"]
    original_publish = session_event_crud.publish
    publish_calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise RuntimeError("session event write failed")
        return await original_publish(*args, **kwargs)

    monkeypatch.setattr(tool_cancellation_module.session_event_crud, "publish", fail_once)
    app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        failed = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
    assert failed.status_code == 500

    _session, failed_messages, failed_events = await _load_tool_stop_state(db_session, session_id=session_id)
    assert len([row for row in failed_messages if row.type == MessageType.TOOL_RESULT]) == 2
    assert failed_events == []

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        retried = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
    assert retried.status_code == 200
    assert retried.json()["data"]["cancelled_count"] == 1

    async with db_session() as db:
        result_rows = list((await db.execute(select(Message).where(Message.session_id == session_id, Message.type == MessageType.TOOL_RESULT))).scalars().all())
        events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id))).scalars().all())
    assert len(result_rows) == 3
    assert len(events) == 1
    assert events[0].event["type"] == "tool_results_update"
