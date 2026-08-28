import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.providers.database as database_provider
from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.core.crud.session_reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.dispatcher import ChatDispatcher
from app.core.session_reply_queue import executor as session_reply_executor
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.audit import AuditConfirmationClaim, AuditRecord
from app.models.message import Message
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
    web_input_queued = asyncio.Event()
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
    monkeypatch.setattr(session_reply_executor, "AsyncSessionLocal", concurrent_queue_session_factory)
    monkeypatch.setattr(session_reply_executor.ChatDispatcher, "dispatch_stream", controlled_dispatch_stream)

    async def submit_first_web_connection():
        async with concurrent_queue_session_factory() as db:
            return await _collect_events(
                web_chat_adapter.chat_stream(
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
        execution = asyncio.create_task(session_reply_executor._execute_foreground(worker_db, claimed, "worker-primary"))
        await first_dispatch_started.wait()

        async def submit_web_connection():
            async with concurrent_queue_session_factory() as db:
                return await _collect_events(
                    web_chat_adapter.chat_stream(
                        db,
                        "B",
                        uid="owner",
                        session_id="session-primary",
                        request_id="request-b",
                    ),
                    queued=web_input_queued,
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
        await web_input_queued.wait()
        await websocket_input_queued.wait()
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

    first_events, web_events, websocket_events = await asyncio.gather(first_connection, web_connection, websocket_connection)

    assert [event["type"] for event in web_events].count("input_queued") == 1
    assert [event["type"] for event in websocket_events].count("input_queued") == 1
    for events in (first_events, web_events, websocket_events):
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
    work_by_source_id = {int(work.source_id): work for work in works}
    queued_messages = [message for message in messages if message.content in {"B", "C"}]
    assert [work_by_source_id[message.id].sequence_no for message in queued_messages] == sorted(work_by_source_id[message.id].sequence_no for message in queued_messages)
    message_by_id = {message.id: message for message in messages}
    assert absorbed_contents == ["\n".join(str(message_by_id[int(work.source_id)].content) for work in works[1:])]
    assert [event.sequence_no for event in persisted_events] == list(range(1, len(persisted_events) + 1))
    assert [event.event["type"] for event in persisted_events] == [
        "input_dequeued",
        "agent_loop_start",
        "content",
        "turn_end",
        "input_dequeued",
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
