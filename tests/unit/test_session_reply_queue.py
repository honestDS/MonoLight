from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.crud.session_reply_work_item import CRUDSessionReplyWorkItem
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor as executor_module
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    Message.__table__,
                    SessionReplySequence.__table__,
                    SessionReplyWorkItem.__table__,
                    SessionReplyStreamEvent.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _add_message(db: AsyncSession, message_id: int, content: str) -> Message:
    message = Message(
        id=message_id,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content=content,
        is_processed=False,
    )
    db.add(message)
    await db.flush()
    return message


async def _enqueue(
    crud: CRUDSessionReplyWorkItem,
    db: AsyncSession,
    *,
    work_type: SessionReplyWorkType,
    source_id: int,
    dedupe_key: str,
) -> SessionReplyWorkItem:
    work, _created = await crud.enqueue(
        db,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        work_type=work_type,
        source_type=SessionReplySourceType.USER_MESSAGE if work_type == SessionReplyWorkType.FOREGROUND_REPLY else SessionReplySourceType.BACKGROUND_TASK,
        source_id=source_id,
        dedupe_key=dedupe_key,
        commit=False,
    )
    return work


@pytest.mark.asyncio
async def test_sequence_numbers_are_shared_by_all_work_types(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    second = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=8, dedupe_key="background-task-summary:8")
    third = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await db_session.commit()

    assert [first.sequence_no, second.sequence_no, third.sequence_no] == [1, 2, 3]


@pytest.mark.asyncio
async def test_duplicate_enqueue_returns_existing_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await db_session.commit()

    duplicate, created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=1,
        dedupe_key="foreground-message:1",
    )

    saved = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    assert created is False
    assert duplicate.id == first.id
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_claim_does_not_skip_an_earlier_waiting_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    second = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    first.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    db_session.add(first)
    await db_session.commit()

    claimed = await crud.claim_next(db_session, worker_id="worker-1", lease_seconds=300)

    assert claimed is None
    await db_session.refresh(second)
    assert second.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_claim_allows_different_sessions_to_run_concurrently(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    other, _created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-2",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=2,
        dedupe_key="foreground-message:2",
        commit=False,
    )
    await db_session.commit()

    claimed_first = await crud.claim_next(db_session, worker_id="worker-1", lease_seconds=300)
    claimed_second = await crud.claim_next(db_session, worker_id="worker-2", lease_seconds=300)

    assert {claimed_first.id, claimed_second.id} == {first.id, other.id}


@pytest.mark.asyncio
async def test_cancel_session_only_cancels_owned_non_terminal_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    ready = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    succeeded = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=2, dedupe_key="background-task-summary:2")
    succeeded.status = SessionReplyWorkStatus.SUCCEEDED
    other, _created = await crud.enqueue(
        db_session,
        uid="user-2",
        session_id="session-1",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=3,
        dedupe_key="foreground-message:3",
        commit=False,
    )
    await db_session.commit()

    cancelled_count = await crud.cancel_session(
        db_session,
        session_id="session-1",
        uid="user-1",
    )

    await db_session.refresh(ready)
    await db_session.refresh(succeeded)
    await db_session.refresh(other)
    assert cancelled_count == 1
    assert ready.status == SessionReplyWorkStatus.CANCELLED
    assert succeeded.status == SessionReplyWorkStatus.SUCCEEDED
    assert other.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_recover_expired_does_not_retry_work_with_emitted_stream_content(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    streamed = await _enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:1",
    )
    retryable = await _enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:2",
    )
    streamed.status = SessionReplyWorkStatus.RUNNING
    streamed.locked_by = "lost-worker-1"
    streamed.lock_until = 0
    retryable.status = SessionReplyWorkStatus.RUNNING
    retryable.locked_by = "lost-worker-2"
    retryable.lock_until = 0
    db_session.add(
        SessionReplyStreamEvent(
            work_id=streamed.id,
            sequence_no=1,
            event={"type": "content", "content": "partial"},
        )
    )
    await db_session.commit()

    recovered_count, exhausted_claims = await crud.recover_expired(db_session)

    await db_session.refresh(streamed)
    await db_session.refresh(retryable)
    assert recovered_count == 2
    assert exhausted_claims == [
        (
            streamed.id,
            "lost-worker-1",
            "Stream interrupted after partial response",
        )
    ]
    assert streamed.status == SessionReplyWorkStatus.RUNNING
    assert streamed.locked_by == "lost-worker-1"
    assert retryable.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_recover_expired_fails_work_at_max_attempts(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    exhausted = await _enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:1",
    )
    exhausted.status = SessionReplyWorkStatus.RUNNING
    exhausted.locked_by = "lost-worker"
    exhausted.lock_until = 0
    exhausted.attempt_count = exhausted.max_attempts
    await db_session.commit()

    recovered_count, exhausted_claims = await crud.recover_expired(db_session)

    await db_session.refresh(exhausted)
    assert recovered_count == 1
    assert exhausted_claims == [
        (
            exhausted.id,
            "lost-worker",
            "Maximum retry attempts reached after worker interruption",
        )
    ]
    assert exhausted.status == SessionReplyWorkStatus.RUNNING
    assert exhausted.locked_by == "lost-worker"


@pytest.mark.asyncio
async def test_scheduled_claim_respects_profile_limit_and_still_claims_other_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    running = await _enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
        source_id=1,
        dedupe_key="scheduled-task-summary:1",
    )
    running.status = SessionReplyWorkStatus.RUNNING
    running.locked_by = "worker-1"
    running.lock_until = 9999999999

    scheduled, _created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-2",
        profile_id=1,
        work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id=2,
        dedupe_key="scheduled-task-summary:2",
        commit=False,
    )
    foreground, _created = await crud.enqueue(
        db_session,
        uid="user-1",
        session_id="session-3",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=3,
        dedupe_key="foreground-message:3",
        commit=False,
    )
    await db_session.commit()

    claimed = await crud.claim_next(
        db_session,
        worker_id="worker-2",
        lease_seconds=300,
        scheduled_profile_limits={1: 1},
    )

    assert claimed.id == foreground.id
    await db_session.refresh(scheduled)
    assert scheduled.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_foreground_freeze_merges_only_until_background_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await _add_message(db_session, 1, "A")
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await _enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=9, dedupe_key="background-task-summary:9")
    await _add_message(db_session, 2, "B")
    later = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    content, attachments, message_ids = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    assert content == "A"
    assert attachments == []
    assert message_ids == [1]
    await db_session.refresh(later)
    assert later.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_foreground_freeze_merges_contiguous_work_and_is_stable(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await _add_message(db_session, 1, "B")
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await _add_message(db_session, 2, "C")
    merged = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    first_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await _add_message(db_session, 3, "D")
    await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=3, dedupe_key="foreground-message:3")
    await db_session.commit()
    await db_session.refresh(first)
    second_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    assert first_result == ("B\nC", [], [1, 2])
    assert second_result == first_result
    await db_session.refresh(merged)
    assert merged.status == SessionReplyWorkStatus.MERGED
    assert merged.merged_into_id == first.id
    processed = list((await db_session.execute(select(Message).where(Message.id.in_([1, 2])))).scalars().all())
    assert all(message.is_processed for message in processed)


@pytest.mark.asyncio
async def test_running_foreground_work_absorbs_later_contiguous_messages(db_session: AsyncSession, monkeypatch):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await _add_message(db_session, 1, "first")
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await _add_message(db_session, 2, "second")
    second = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await _add_message(db_session, 3, "third")
    third = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=3, dedupe_key="foreground-message:3")
    await db_session.commit()

    logged_messages: list[str] = []

    class CapturingLogger:
        def bind(self, **kwargs):
            return self

        def info(self, message):
            logged_messages.append(message)

    async def skip_runtime_instructions(db, session_id, message):
        return None

    monkeypatch.setattr("app.core.session_reply_queue.manager.logger", CapturingLogger())
    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)

    additional_messages = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=first.id,
        worker_id="worker-1",
    )

    assert len(additional_messages) == 1
    assert additional_messages[0].content == "second\nthird"
    assert additional_messages[0].id == 3
    assert len(logged_messages) == 1
    assert "second\nthird" in logged_messages[0]
    await db_session.refresh(first)
    await db_session.refresh(second)
    await db_session.refresh(third)
    assert first.input_message_ids == [1, 2, 3]
    assert second.status == SessionReplyWorkStatus.MERGED
    assert second.merged_into_id == first.id
    assert third.status == SessionReplyWorkStatus.MERGED
    assert third.merged_into_id == first.id


@pytest.mark.asyncio
async def test_running_foreground_work_does_not_absorb_across_background_boundary(db_session: AsyncSession, monkeypatch):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await _add_message(db_session, 1, "first")
    first = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await _add_message(db_session, 2, "before boundary")
    before_boundary = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await _enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=9, dedupe_key="background-task-summary:9")
    await _add_message(db_session, 3, "after boundary")
    after_boundary = await _enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=3, dedupe_key="foreground-message:3")
    await db_session.commit()

    async def skip_runtime_instructions(db, session_id, message):
        return None

    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)

    additional_messages = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=first.id,
        worker_id="worker-1",
    )

    assert len(additional_messages) == 1
    assert additional_messages[0].content == "before boundary"
    await db_session.refresh(before_boundary)
    await db_session.refresh(after_boundary)
    assert before_boundary.status == SessionReplyWorkStatus.MERGED
    assert before_boundary.merged_into_id == first.id
    assert after_boundary.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert after_boundary.merged_into_id is None


@pytest.mark.asyncio
async def test_wait_for_result_returns_resolved_work_id(monkeypatch):
    manager = SessionReplyQueueManager()
    resolved_work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={
            "response": {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "result"},
                        "finish_reason": True,
                    }
                ]
            }
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id):
        return resolved_work

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "resolve_merged_target", resolve_merged_target)

    response = await manager.wait_for_result(9)

    assert response["work_id"] == 7
    assert response["choices"][0]["message"]["content"] == "result"


@pytest.mark.asyncio
async def test_foreground_executor_resumes_dispatcher_checkpoint(monkeypatch):
    checkpoint = {
        "messages": [
            {"role": "user", "content": "original"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tool-1", "name": "execute_shell", "arguments": {"command": "echo 1"}}],
            },
            {"role": "tool", "content": "1", "tool_call_id": "tool-1"},
        ],
        "turn_messages": [],
        "files_to_user": [],
        "current_turn": 1,
    }
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
        input_message_ids=[1],
        execution_state={"stream_requested": False, "dispatcher_checkpoint": checkpoint},
    )
    dispatch_kwargs = {}
    checkpoint_updates = []

    class FakeDb:
        async def refresh(self, instance) -> None:
            return None

    class EventDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def freeze_foreground_input(db, *, work, worker_id):
        return "original", [], [1]

    async def latest_sequence(db, *, work_id):
        return 0

    async def dispatch(**kwargs):
        dispatch_kwargs.update(kwargs)
        await kwargs["execution_checkpoint_callback"]({"messages": [{"role": "user", "content": "updated"}]})
        return {"choices": []}

    async def update_claimed(db, **kwargs):
        checkpoint_updates.append(kwargs)
        return True

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_queue_manager, "freeze_foreground_input", freeze_foreground_input)
    monkeypatch.setattr(executor_module.session_reply_stream_event_crud, "get_latest_sequence", latest_sequence)
    monkeypatch.setattr(executor_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)

    response = await executor_module._execute_foreground(FakeDb(), work, "worker-1")

    assert response == {"choices": []}
    assert dispatch_kwargs["execution_resume_state"] == checkpoint
    assert dispatch_kwargs["message"] == "original"
    assert checkpoint_updates[0]["values"]["execution_state"]["stream_requested"] is False
    assert checkpoint_updates[0]["values"]["execution_state"]["dispatcher_checkpoint"]["messages"][0]["content"] == "updated"


@pytest.mark.asyncio
async def test_executor_resumes_from_persisted_result_without_calling_llm(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
    )
    persisted_result = SimpleNamespace(id=9, content="saved response")
    sent_events = []
    update_calls = []
    terminal_calls = []
    llm_calls = []

    class FakeSession:
        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id: int):
        return work

    async def get_result(db, dedupe_key: str):
        return persisted_result

    async def execute_foreground(db, current_work, worker_id: str):
        llm_calls.append((current_work.id, worker_id))
        return {}

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        return True

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        sent_events.append((uid, session_id, event))

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_module, "send_session_event", send_event)

    await executor_module.execute_session_reply_work(work_id=7, worker_id="worker-1")

    assert llm_calls == []
    assert update_calls[0]["values"]["result_message_id"] == 9
    assert update_calls[0]["values"]["execution_state"]["response"]["choices"][0]["message"]["content"] == "saved response"
    assert sent_events[0][2]["event_id"] == "session-reply-work:7:event"
    assert terminal_calls[0]["status"] == SessionReplyWorkStatus.SUCCEEDED
    assert terminal_calls[0]["result_message_id"] == 9


@pytest.mark.asyncio
async def test_consumer_recovery_runs_full_failure_flow_for_exhausted_claims(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    failure_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def recover_expired(db):
        return 1, [
            (
                7,
                "lost-worker",
                "Maximum retry attempts reached after worker interruption",
            )
        ]

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "recover_expired", recover_expired)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)

    await consumer._recover_expired()

    assert failure_calls == [
        (
            7,
            "lost-worker",
            "Maximum retry attempts reached after worker interruption",
        )
    ]


@pytest.mark.asyncio
async def test_consumer_does_not_retry_after_stream_content_was_emitted(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    failure_calls = []
    retry_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise RuntimeError("stream interrupted")

    async def has_events(db, *, work_id: int) -> bool:
        return True

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=3)

    assert len(failure_calls) == 1
    assert failure_calls[0][:2] == (7, "worker-1")
    assert retry_calls == []
