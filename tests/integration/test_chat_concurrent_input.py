import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import WebSocketDisconnect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.providers.database as database_provider
from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.api.v1 import chat as chat_api
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import LLMException
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue import executor_replies as executor_replies_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.dispatcher.save_message import save_message
from app.models.audit import AuditConfirmationClaim, AuditRecord
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
)


@pytest_asyncio.fixture
async def concurrent_queue_session_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'chat-concurrent-input.db'}",
        connect_args={"timeout": 30},
    )
    tables = [
        Profile.__table__,
        Message.__table__,
        ChatSession.__table__,
        AuditRecord.__table__,
        AuditConfirmationClaim.__table__,
        SessionReplySequence.__table__,
        SessionReplyWorkItem.__table__,
        SessionReplyStreamEvent.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=tables))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as setup_session:
        setup_session.add(Profile(id=1, uid="owner", name="queue-test", configs={}))
        await setup_session.commit()
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _wait_for_work(session_factory, *, session_id: str, expected_count: int) -> list[SessionReplyWorkItem]:
    for _ in range(100):
        async with session_factory() as db:
            works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.sequence_no))).scalars().all())
        if len(works) >= expected_count:
            return works
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected_count} queued works for {session_id}")


async def _collect_events(stream, queued: asyncio.Event | None = None) -> list[dict]:
    events: list[dict] = []
    async for event in stream:
        events.append(event)
        if queued is not None and event.get("type") == "input_queued":
            queued.set()
    return events


@pytest.mark.asyncio
async def test_concurrent_web_and_websocket_input_is_absorbed_and_replayed_from_persisted_stream_events(
    concurrent_queue_session_factory,
    monkeypatch,
):
    profile = Profile(id=1, uid="owner", name="queue-test", configs={})
    first_dispatch_started = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    websocket_input_queued = asyncio.Event()
    absorbed_contents: list[str] = []

    async def resolve_profile(*_args, **_kwargs):
        return profile

    async def validate_initial_message(*_args, **_kwargs):
        return None

    async def ensure_writable(*_args, **_kwargs):
        return None

    async def controlled_dispatch_stream(**kwargs):
        additional_user_messages_fetcher = kwargs["additional_user_messages_fetcher"]
        yield {"type": "agent_loop_start", "response_id": "response-a", "turn": 1}
        yield {"type": "content", "content": "A first turn", "response_id": "response-a", "turn": 1}
        first_dispatch_started.set()
        await release_first_dispatch.wait()

        additional = await additional_user_messages_fetcher()
        assert additional is not None
        absorbed_contents.extend(str(message.content) for message in additional.messages)
        yield {"type": "turn_end", "content": "A first turn", "response_id": "response-a", "turn": 1}
        yield {"type": "agent_loop_start", "response_id": "response-bc", "turn": 2}
        yield {"type": "content", "content": "B and C processed", "response_id": "response-bc", "turn": 2}
        yield {"type": "turn_end", "content": "B and C processed", "response_id": "response-bc", "turn": 2}
        yield {
            "type": "done",
            "response_id": "response-final",
            "response": {
                "content": "B and C processed",
                "history": [],
                "files": None,
            },
        }

    monkeypatch.setattr("app.adapters.chat_web.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr("app.adapters.chat_ws.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr(ChatDispatcher, "validate_initial_message_before_save", validate_initial_message)
    monkeypatch.setattr("app.adapters.chat_web.ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr(database_provider, "AsyncSessionLocal", concurrent_queue_session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", concurrent_queue_session_factory)
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch_stream", controlled_dispatch_stream)

    async def submit_first_web_connection():
        async with concurrent_queue_session_factory() as db:
            return await _collect_events(
                ws_chat_adapter.chat(
                    db,
                    "A",
                    uid="owner",
                    session_id="session-primary",
                    request_id="request-a",
                )
            )

    first_connection = asyncio.create_task(submit_first_web_connection())
    first_work = (await _wait_for_work(concurrent_queue_session_factory, session_id="session-primary", expected_count=1))[0]

    async with concurrent_queue_session_factory() as worker_db:
        claimed = await session_reply_work_item_crud.claim_next(worker_db, worker_id="worker-primary", lease_seconds=300)
        assert claimed is not None
        assert claimed.id == first_work.id
        execution = asyncio.create_task(executor_replies_module._execute_foreground(worker_db, claimed, "worker-primary"))
        await first_dispatch_started.wait()

        async def submit_web_connection():
            async with concurrent_queue_session_factory() as db:
                return await web_chat_adapter.submit(
                    db,
                    "B",
                    uid="owner",
                    session_id="session-primary",
                    request_id="request-b",
                )

        async def submit_websocket_connection():
            async with concurrent_queue_session_factory() as db:
                return await _collect_events(
                    ws_chat_adapter.chat(
                        db,
                        "C",
                        uid="owner",
                        session_id="session-primary",
                        request_id="request-c",
                    ),
                    queued=websocket_input_queued,
                )

        web_connection = asyncio.create_task(submit_web_connection())
        websocket_connection = asyncio.create_task(submit_websocket_connection())
        await websocket_input_queued.wait()
        web_response = await web_connection
        assert web_response["submission_status"] == "queued"
        assert web_response["work_id"] is not None
        queued_works = await _wait_for_work(concurrent_queue_session_factory, session_id="session-primary", expected_count=3)
        assert [work.sequence_no for work in queued_works] == [1, 2, 3]

        release_first_dispatch.set()
        response = await execution
        updated = await session_reply_work_item_crud.update_claimed(
            worker_db,
            work_id=claimed.id,
            worker_id="worker-primary",
            values={"execution_state": {**(claimed.execution_state or {}), "response": response}},
            commit=False,
        )
        assert updated is True
        marked_terminal = await session_reply_work_item_crud.mark_terminal(
            worker_db,
            work_id=claimed.id,
            worker_id="worker-primary",
            status=SessionReplyWorkStatus.SUCCEEDED,
            commit=False,
        )
        assert marked_terminal is True
        await worker_db.commit()

    first_events, websocket_events = await asyncio.gather(first_connection, websocket_connection)

    assert [event["type"] for event in websocket_events].count("input_queued") == 1
    assert web_response["submission_status"] == "queued"
    assert web_response["work_id"] is not None
    for events in (first_events, websocket_events):
        done_events = [event for event in events if event.get("type") == "done"]
        assert len(done_events) == 1
        assert done_events[0]["response_id"] == "response-final"

    async with concurrent_queue_session_factory() as db:
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == "session-primary").order_by(SessionReplyWorkItem.sequence_no))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-primary").order_by(Message.id))).scalars().all())
        persisted_events = await session_reply_stream_event_crud.list_after_sequence(
            db,
            work_id=first_work.id,
            after_sequence_no=0,
        )

    assert [work.sequence_no for work in works] == [1, 2, 3]
    assert works[0].status == SessionReplyWorkStatus.SUCCEEDED
    assert all(work.status == SessionReplyWorkStatus.MERGED for work in works[1:])
    message_by_id = {message.id: message for message in messages}
    assert absorbed_contents == ["\n".join(str(message_by_id[int(work.source_id)].content) for work in works[1:])]
    assert [event.sequence_no for event in persisted_events] == list(range(1, len(persisted_events) + 1))
    assert [event.event["type"] for event in persisted_events] == [
        "input_dequeued",
        "agent_loop_start",
        "content",
        "input_dequeued",
        "turn_end",
        "agent_loop_start",
        "content",
        "turn_end",
    ]
    dequeued_request_ids = [request_id for event in persisted_events if event.event["type"] == "input_dequeued" for request_id in event.event["request_ids"]]
    expected_dequeued_request_ids = [
        "request-a",
        *[request_id for work in works[1:] for request_id in work.execution_state["request_ids"]],
    ]
    assert dequeued_request_ids == expected_dequeued_request_ids
    assert len(dequeued_request_ids) == len(set(dequeued_request_ids)) == 3
    assert set(dequeued_request_ids) == {"request-a", "request-b", "request-c"}

    replayed_events = [event async for event in session_reply_queue_manager.wait_for_stream(first_work.id)]
    assert [event["event_sequence_no"] for event in replayed_events if "event_sequence_no" in event] == [event.sequence_no for event in persisted_events]
    replayed_done = [event for event in replayed_events if event.get("type") == "done"]
    assert len(replayed_done) == 1
    assert replayed_done[0]["response_id"] == "response-final"

    async with concurrent_queue_session_factory() as db:
        await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-independent-a",
            profile=profile,
            message="independent A",
            attachments=None,
            source="http",
        )
        await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-independent-b",
            profile=profile,
            message="independent B",
            attachments=None,
            source="ws",
        )

    async with concurrent_queue_session_factory() as db:
        first_independent_claim = await session_reply_work_item_crud.claim_next(db, worker_id="worker-independent-a", lease_seconds=300)
        second_independent_claim = await session_reply_work_item_crud.claim_next(db, worker_id="worker-independent-b", lease_seconds=300)

    assert first_independent_claim is not None
    assert second_independent_claim is not None
    assert {first_independent_claim.session_id, second_independent_claim.session_id} == {
        "session-independent-a",
        "session-independent-b",
    }


def _patch_queue_runtime_database(monkeypatch, session_factory) -> None:
    monkeypatch.setattr(database_provider, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)


async def _persist_fake_foreground_reply(kwargs: dict, content: str) -> dict:
    await save_message(
        kwargs["db"],
        kwargs["session_id"],
        kwargs["uid"],
        MessageRole.ASSISTANT,
        MessageType.TEXT,
        InternalMessage(role=MessageRole.ASSISTANT, content=content),
        kwargs["persisted_profile_id"],
        dedupe_key=kwargs["final_message_dedupe_key"],
    )
    return {
        "choices": [{"message": {"role": MessageRole.ASSISTANT, "content": content}, "finish_reason": "stop"}],
        "history": [],
        "files": None,
    }


@pytest.mark.asyncio
async def test_foreground_work_retries_transient_failure_then_completes_through_consumer(
    concurrent_queue_session_factory,
    monkeypatch,
):
    profile = Profile(id=1, uid="owner", name="queue-test", configs={})
    _patch_queue_runtime_database(monkeypatch, concurrent_queue_session_factory)
    monkeypatch.setattr(consumer_module, "retry_delay_seconds", lambda _attempt: 0)
    sent_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event: dict) -> None:
        sent_events.append(event)

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)
    dispatch_attempts = 0

    async def transient_dispatch(**kwargs):
        nonlocal dispatch_attempts
        dispatch_attempts += 1
        if dispatch_attempts == 1:
            raise RuntimeError("transient dispatcher failure")
        return await _persist_fake_foreground_reply(kwargs, "retry succeeded")

    monkeypatch.setattr(ChatDispatcher, "dispatch", transient_dispatch)

    async with concurrent_queue_session_factory() as db:
        _message, work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-retry",
            profile=profile,
            message="retry me",
            attachments=None,
            source="http",
            stream_requested=False,
            request_id="request-retry",
        )
        assert submission_status == "accepted"
        first_claim = await session_reply_work_item_crud.claim_next(db, worker_id="worker-retry-1", lease_seconds=300)

    assert first_claim is not None
    assert first_claim.id == work.id
    consumer = consumer_module.SessionReplyConsumer()
    await consumer._run_claimed(first_claim.id, "worker-retry-1", first_claim.attempt_count, first_claim.max_attempts)

    async with concurrent_queue_session_factory() as db:
        released = await session_reply_work_item_crud.get(db, work.id)
        assert released is not None
        assert released.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert released.locked_by is None
        assert released.attempt_count == 1
        assert isinstance(released.error, str) and released.error
        second_claim = await session_reply_work_item_crud.claim_next(db, worker_id="worker-retry-2", lease_seconds=300)

    assert second_claim is not None
    assert second_claim.id == work.id
    assert second_claim.attempt_count == 2
    await consumer._run_claimed(second_claim.id, "worker-retry-2", second_claim.attempt_count, second_claim.max_attempts)

    async with concurrent_queue_session_factory() as db:
        completed = await session_reply_work_item_crud.get(db, work.id)
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-retry").order_by(Message.id))).scalars().all())

    assert dispatch_attempts == 2
    assert completed is not None
    assert completed.status == SessionReplyWorkStatus.SUCCEEDED
    assert completed.attempt_count == 2
    assert completed.result_message_id is not None
    assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "retry succeeded"
    assert completed.result_message_id == assistant_messages[0].id
    assert len(sent_events) == 1


@pytest.mark.asyncio
async def test_foreground_business_failure_is_terminal_without_queue_retry(
    concurrent_queue_session_factory,
    monkeypatch,
):
    profile = Profile(id=1, uid="owner", name="queue-test", configs={})
    _patch_queue_runtime_database(monkeypatch, concurrent_queue_session_factory)
    sent_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event: dict) -> None:
        sent_events.append(event)

    async def failed_dispatch(**_kwargs):
        raise LLMException(message="ERR_LLM_CONNECTION_FAILED", detail="provider unavailable")

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)
    monkeypatch.setattr(ChatDispatcher, "dispatch", failed_dispatch)

    async with concurrent_queue_session_factory() as db:
        _message, work, _submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-business-failure",
            profile=profile,
            message="fail once",
            attachments=None,
            source="http",
            stream_requested=False,
            request_id="request-business-failure",
        )
        claim = await session_reply_work_item_crud.claim_next(db, worker_id="worker-business-failure", lease_seconds=300)

    assert claim is not None
    consumer = consumer_module.SessionReplyConsumer()
    await consumer._run_claimed(claim.id, "worker-business-failure", claim.attempt_count, claim.max_attempts)

    async with concurrent_queue_session_factory() as db:
        failed = await session_reply_work_item_crud.get(db, work.id)
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-business-failure").order_by(Message.id))).scalars().all())

    assert failed is not None
    assert failed.status == SessionReplyWorkStatus.FAILED
    assert failed.attempt_count == 1
    assert failed.result_message_id is not None
    error_messages = [message for message in messages if message.role == MessageRole.ERR]
    assert len(error_messages) == 1
    assert error_messages[0].id == failed.result_message_id
    assert len(sent_events) == 1


@pytest.mark.asyncio
async def test_streamed_foreground_failure_is_terminal_after_persisted_output(
    concurrent_queue_session_factory,
    monkeypatch,
):
    profile = Profile(id=1, uid="owner", name="queue-test", configs={})
    _patch_queue_runtime_database(monkeypatch, concurrent_queue_session_factory)
    sent_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event: dict) -> None:
        sent_events.append(event)

    async def failing_stream(**_kwargs):
        yield {"type": "agent_loop_start", "response_id": "response-stream-failure", "turn": 1}
        yield {"type": "content", "content": "partial", "response_id": "response-stream-failure", "turn": 1}
        raise RuntimeError("stream interrupted")

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)
    monkeypatch.setattr(ChatDispatcher, "dispatch_stream", failing_stream)

    async with concurrent_queue_session_factory() as db:
        _message, work, _submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-stream-failure",
            profile=profile,
            message="stream me",
            attachments=None,
            source="ws",
            stream_requested=True,
            request_id="request-stream-failure",
        )
        claim = await session_reply_work_item_crud.claim_next(db, worker_id="worker-stream-failure", lease_seconds=300)

    assert claim is not None
    consumer = consumer_module.SessionReplyConsumer()
    await consumer._run_claimed(claim.id, "worker-stream-failure", claim.attempt_count, claim.max_attempts)

    async with concurrent_queue_session_factory() as db:
        failed = await session_reply_work_item_crud.get(db, work.id)
        persisted_events = await session_reply_stream_event_crud.list_after_sequence(db, work_id=work.id, after_sequence_no=0)

    assert failed is not None
    assert failed.status == SessionReplyWorkStatus.FAILED
    assert failed.attempt_count == 1
    assert [event.event["type"] for event in persisted_events] == ["input_dequeued", "agent_loop_start", "content"]
    assert len(sent_events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_before_resume", [False, True])
async def test_websocket_resume_preserves_history_gap_and_drains_completed_stream(
    concurrent_queue_session_factory,
    monkeypatch,
    completed_before_resume,
):
    factory = concurrent_queue_session_factory
    monkeypatch.setattr(database_provider, "AsyncSessionLocal", factory)
    monkeypatch.setattr(chat_api, "AsyncSessionLocal", factory)

    async def runtime_settings(*args, **kwargs):
        return SimpleNamespace(log_locale="zh")

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_api.system_setting_crud, "get_runtime_settings", runtime_settings)
    monkeypatch.setattr(chat_api.session_notifier, "register", noop)
    monkeypatch.setattr(chat_api.session_notifier, "unregister", noop)
    async with factory() as db:
        db.add(ChatSession(session_id="resume-session", uid="owner", profile_id=1, source="ws"))
        db.add(Message(id=101, session_id="resume-session", uid="owner", profile_id=1, role=MessageRole.ASSISTANT, content="already visible"))
        work = SessionReplyWorkItem(
            uid="owner",
            session_id="resume-session",
            profile_id=1,
            sequence_no=1,
            work_type="foreground_reply",
            source_type="user_message",
            source_id="100",
            dedupe_key="resume-work",
            status=SessionReplyWorkStatus.RUNNING,
            execution_state={"response": {"content": "finished", "history": []}},
        )
        db.add(work)
        await db.commit()
        await db.refresh(work)
        work_id = work.id
        history = await chat_api.get_session_history(
            session_id="resume-session",
            page=1,
            size=40,
            db=db,
            current_user=SimpleNamespace(uid="owner"),
        )
        history_ids = [item.id for item in history.data]
        assert history_ids == [101]

        db.add(Message(id=102, session_id="resume-session", uid="owner", profile_id=1, role=MessageRole.ASSISTANT, content="gap reply"))
        events = [
            {"type": "reasoning", "content": "old reasoning", "response_id": "old"},
            {"type": "turn_end", "message_id": 101, "response_id": "old"},
            {"type": "reasoning", "content": "gap reasoning", "response_id": "gap"},
            {"type": "content", "content": "gap reply", "response_id": "gap"},
            {"type": "turn_end", "message_id": 102, "response_id": "gap"},
            {"type": "tool_start", "tool_call_id": "tool-1", "response_id": "gap", "name": "test", "arguments": {}},
            {"type": "tool_end", "tool_call_id": "tool-1", "response_id": "gap", "result": "result"},
            *[{"type": "content", "content": str(index), "response_id": "last"} for index in range(120)],
        ]
        for sequence, event in enumerate(events, 1):
            db.add(
                SessionReplyStreamEvent(
                    work_id=work_id,
                    sequence_no=sequence,
                    event={**event, "session_id": "resume-session", "work_id": work_id, "event_sequence_no": sequence},
                )
            )
        if completed_before_resume:
            work.status = SessionReplyWorkStatus.SUCCEEDED
            db.add(work)
        await db.commit()

        following = SessionReplyWorkItem(
            uid="owner",
            session_id="resume-session",
            profile_id=1,
            sequence_no=2,
            work_type="foreground_reply",
            source_type="user_message",
            source_id="103",
            dedupe_key="following-resume-work",
            status=SessionReplyWorkStatus.SUCCEEDED,
            execution_state={"response": {"content": "following reply", "history": []}},
        )
        db.add(Message(id=104, session_id="resume-session", uid="owner", profile_id=1, role=MessageRole.ASSISTANT, content="following reply"))
        db.add(following)
        await db.flush()
        db.add(
            SessionReplyStreamEvent(
                work_id=following.id,
                sequence_no=1,
                event={"type": "turn_end", "session_id": "resume-session", "work_id": following.id, "message_id": 104},
            )
        )
        await db.commit()

    class Socket:
        query_params = {}

        def __init__(self):
            self.sent = []
            self.requested = False
            self.finished = asyncio.Event()

        async def accept(self):
            pass

        async def receive_json(self):
            if not self.requested:
                self.requested = True
                return {"type": "resume", "session_id": "resume-session", "history_message_id": max(history_ids)}
            await self.finished.wait()
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, event):
            self.sent.append(event)
            if event.get("content") == "119" and not completed_before_resume:
                async with factory() as db:
                    current = await db.get(SessionReplyWorkItem, work_id)
                    current.status = SessionReplyWorkStatus.SUCCEEDED
                    db.add(current)
                    await db.commit()
            if event["type"] == "resume_complete":
                self.finished.set()

    socket = Socket()
    await asyncio.wait_for(chat_api.chat_websocket(socket, SimpleNamespace(uid="owner")), timeout=5)
    assert all(event.get("content") != "old reasoning" for event in socket.sent)
    assert any(event.get("content") == "gap reasoning" for event in socket.sent)
    assert any(event.get("message_id") == 102 for event in socket.sent)
    assert [event["content"] for event in socket.sent if event.get("response_id") == "last"] == [str(index) for index in range(120)]
    assert [event["type"] for event in socket.sent].count("done") == 2
    assert any(event.get("message_id") == 104 for event in socket.sent)
    assert socket.sent[-1]["type"] == "resume_complete"
    async with factory() as db:
        works = list((await db.execute(select(SessionReplyWorkItem))).scalars().all())
        assert len(works) == 2

    assert [
        event
        async for event in session_reply_queue_manager.wait_for_session_stream(
            uid="owner",
            session_id="resume-session",
            history_message_id=104,
        )
    ] == []
    assert [
        event
        async for event in session_reply_queue_manager.wait_for_session_stream(
            uid="another-user",
            session_id="resume-session",
            history_message_id=0,
        )
    ] == []
