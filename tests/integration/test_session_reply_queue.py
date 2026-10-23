import asyncio
import copy
import json

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.util import await_only
from sqlmodel import select

from app.core.constants import SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue import executor_replies as executor_replies_module
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.core.terminal.schemas import ShellNonInteractiveCompletedResult
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.database.client import CancellationSafeAsyncSession
from app.transformers.openai.chat_completions import OpenAIChatCompletionsTransformer
from tests.integration.session_reply_queue_fixture import concurrent_session_factory as _concurrent_session_factory
from tests.integration.session_reply_queue_fixture import db_session as _db_session
from tests.integration.session_reply_queue_test_support import AsyncBarrier, enqueue
from tests.integration.stream_event_test_support import dispatcher_stream_event
from tests.unit.response_test_support import llm_response_payload

db_session = _db_session
concurrent_session_factory = _concurrent_session_factory


@pytest.mark.asyncio
async def test_sequence_numbers_are_shared_by_all_work_types(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    second = await enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=8, dedupe_key="background-task-summary:8")
    third = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await db_session.commit()

    assert [first.sequence_no, second.sequence_no, third.sequence_no] == [1, 2, 3]
    assert [first.max_attempts, second.max_attempts, third.max_attempts] == [2, 2, 2]


def test_session_reply_work_model_defaults_to_two_attempts():
    work = SessionReplyWorkItem(
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
    )

    assert work.max_attempts == 2


@pytest.mark.asyncio
async def test_duplicate_enqueue_returns_existing_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
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
async def test_concurrent_duplicate_enqueue_does_not_consume_a_sequence_number(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
):
    crud = CRUDSessionReplyWorkItem()
    allocation_barrier = AsyncBarrier(2)
    original_allocate_sequence_no = crud.allocate_sequence_no

    async def synchronize_sequence_allocation(db: AsyncSession, session_id: str) -> int:
        await allocation_barrier.wait()
        return await original_allocate_sequence_no(db, session_id)

    monkeypatch.setattr(crud, "allocate_sequence_no", synchronize_sequence_allocation)

    async def enqueue_duplicate(source_id: int) -> tuple[int | None, int | None, bool]:
        async with concurrent_session_factory() as db:
            work, created = await crud.enqueue(
                db,
                uid="user-1",
                session_id="session-1",
                profile_id=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id=source_id,
                dedupe_key="foreground-message:duplicate",
            )
            return work.id, work.sequence_no, created

    duplicate_results = await asyncio.gather(enqueue_duplicate(30), enqueue_duplicate(10))
    async with concurrent_session_factory() as db:
        next_work, created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=20,
            dedupe_key="foreground-message:next",
        )

    assert sum(created for _work_id, _sequence_no, created in duplicate_results) == 1
    assert len({work_id for work_id, _sequence_no, _created in duplicate_results}) == 1
    assert {sequence_no for _work_id, sequence_no, _created in duplicate_results} == {1}
    assert created is True
    assert next_work.sequence_no == 2


@pytest.mark.asyncio
async def test_concurrent_claims_keep_same_session_serial_and_claim_other_session(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
):
    crud = CRUDSessionReplyWorkItem()
    async with concurrent_session_factory() as db:
        first_session_first, _created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=30,
            dedupe_key="foreground-message:30",
            commit=False,
        )
        first_session_second, _created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=10,
            dedupe_key="foreground-message:10",
            commit=False,
        )
        second_session_first, _created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-2",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=20,
            dedupe_key="foreground-message:20",
            commit=False,
        )
        await db.commit()

    claim_barrier = AsyncBarrier(2)

    async def claim_in_session(worker_id: str) -> SessionReplyWorkItem | None:
        async with concurrent_session_factory() as db:
            await claim_barrier.wait()
            return await crud.claim_next(db, worker_id=worker_id, lease_seconds=300)

    claimed = await asyncio.gather(claim_in_session("worker-1"), claim_in_session("worker-2"))

    assert {work.id for work in claimed if work is not None} == {first_session_first.id, second_session_first.id}
    assert first_session_second.id not in {work.id for work in claimed if work is not None}


@pytest.mark.asyncio
async def test_claim_does_not_skip_an_earlier_waiting_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    second = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    first.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    db_session.add(first)
    await db_session.commit()

    claimed = await crud.claim_next(db_session, worker_id="worker-1", lease_seconds=300)

    assert claimed is None
    await db_session.refresh(second)
    assert second.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_cancel_session_only_cancels_owned_non_terminal_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    ready = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    succeeded = await enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=2, dedupe_key="background-task-summary:2")
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
    streamed = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:1",
    )
    retryable = await enqueue(
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
    exhausted = await enqueue(
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
async def test_recover_expired_never_retries_confirmed_tool_execution(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    confirmed = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id=10,
        dedupe_key="confirmed-audit:10",
    )
    confirmed.status = SessionReplyWorkStatus.RUNNING
    confirmed.locked_by = "lost-worker"
    confirmed.lock_until = 0
    confirmed.attempt_count = 1
    confirmed.max_attempts = 2
    await db_session.commit()

    recovered_count, terminal_claims = await crud.recover_expired(db_session)

    await db_session.refresh(confirmed)
    assert recovered_count == 1
    assert terminal_claims == [
        (
            confirmed.id,
            "lost-worker",
            "Confirmed tool execution was interrupted; result unknown and automatic retry is forbidden",
        )
    ]
    assert confirmed.status == SessionReplyWorkStatus.RUNNING
    assert confirmed.locked_by == "lost-worker"


@pytest.mark.asyncio
async def test_recover_expired_does_not_retry_foreground_work_with_active_audit_execution(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    active = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:active-audit",
    )
    retryable = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:retryable",
    )
    active.status = SessionReplyWorkStatus.RUNNING
    active.locked_by = "lost-worker"
    active.lock_until = 0
    active.execution_state = {
        "active_audit_execution": {
            "audit_record_id": 42,
            "claim_token": "claim-token",
        }
    }
    retryable.status = SessionReplyWorkStatus.RUNNING
    retryable.locked_by = "lost-worker-2"
    retryable.lock_until = 0
    await db_session.commit()

    recovered_count, terminal_claims = await crud.recover_expired(db_session)

    await db_session.refresh(active)
    await db_session.refresh(retryable)
    assert recovered_count == 2
    assert terminal_claims == [
        (
            active.id,
            "lost-worker",
            "审计工具执行领取后被中断，结果未知，禁止自动重试",
        )
    ]
    assert active.status == SessionReplyWorkStatus.RUNNING
    assert active.locked_by == "lost-worker"
    assert retryable.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_type",
    [SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.SCHEDULED_TASK_SUMMARY],
)
async def test_recover_expired_does_not_retry_background_or_scheduled_audit_work(db_session: AsyncSession, work_type):
    """租约恢复发现后台或定时审计绑定时不得重新入队。"""
    crud = CRUDSessionReplyWorkItem()
    work = await enqueue(
        crud,
        db_session,
        work_type=work_type,
        source_id=1,
        dedupe_key=f"{work_type.value}:active-audit",
    )
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = "lost-worker"
    work.lock_until = 0
    work.execution_state = {
        SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
            "audit_record_id": 42,
            "claim_token": "claim-token",
        }
    }
    await db_session.commit()

    recovered_count, terminal_claims = await crud.recover_expired(db_session)

    await db_session.refresh(work)
    assert recovered_count == 1
    assert terminal_claims == [
        (
            work.id,
            "lost-worker",
            "审计工具执行领取后被中断，结果未知，禁止自动重试",
        )
    ]
    assert work.status == SessionReplyWorkStatus.RUNNING


@pytest.mark.asyncio
async def test_scheduled_claim_respects_profile_limit_and_still_claims_other_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    running = await enqueue(
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
@pytest.mark.parametrize("session_class", [AsyncSession, CancellationSafeAsyncSession])
async def test_stream_events_publish_while_generation_transaction_is_committing(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
    session_class: type[AsyncSession],
):
    engine = concurrent_session_factory.kw["bind"]
    session_factory = async_sessionmaker(bind=engine, class_=session_class, expire_on_commit=False)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)

    crud = CRUDSessionReplyWorkItem()
    async with session_factory() as setup_db:
        work = await enqueue(
            crud,
            setup_db,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=1,
            dedupe_key="foreground-message:1",
        )
        work.status = SessionReplyWorkStatus.RUNNING
        work.locked_by = "worker-1"
        work.lock_until = 9999999999
        work.execution_state = {
            "stream_requested": True,
            "request_ids": ["request-1"],
        }
        await setup_db.commit()
        assert work.id is not None
        work_id = work.id

    async with session_factory() as generation_db:
        generation_work = await crud.get(generation_db, work_id)
        assert generation_work is not None
        assert generation_work.execution_state["request_ids"] == ["request-1"]

        async with session_factory() as update_db:
            updated = await crud.update_claimed(
                update_db,
                work_id=work_id,
                worker_id="worker-1",
                values={
                    "execution_state": {
                        "stream_requested": True,
                        "request_ids": ["request-1", "request-2", "request-2"],
                    }
                },
            )
            assert updated is True

        commit_prepared = asyncio.Event()
        release_commit = asyncio.Event()
        commit_task: asyncio.Task[None] | None = None
        commit_listener = None
        expected_response = llm_response_payload("stream completed", history=[])
        expected_response["choices"][0]["finish_reason"] = "stop"

        async def controlled_dispatch_stream(**kwargs):
            nonlocal commit_task, commit_listener
            generation_connection = await kwargs["db"].connection()
            generation_sync_connection = generation_connection.sync_connection

            def pause_generation_commit(connection):
                if connection is generation_sync_connection:
                    commit_prepared.set()
                    await_only(asyncio.wait_for(release_commit.wait(), timeout=5))

            event.listen(engine.sync_engine, "commit", pause_generation_commit)
            commit_listener = pause_generation_commit
            commit_task = asyncio.create_task(kwargs["db"].commit())
            await asyncio.wait_for(commit_prepared.wait(), timeout=5)
            yield dispatcher_stream_event(
                "agent_loop_start",
                turn=0,
                response_id="response-recall",
                session_id=kwargs["session_id"],
            )
            release_commit.set()
            await asyncio.wait_for(asyncio.shield(commit_task), timeout=5)
            yield dispatcher_stream_event(
                "agent_loop_start",
                turn=1,
                response_id="response-main",
                session_id=kwargs["session_id"],
            )
            yield dispatcher_stream_event(
                "done",
                response_id="response-main",
                session_id=kwargs["session_id"],
                response=expected_response,
            )

        monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch_stream", controlled_dispatch_stream)
        try:
            response = await executor_interactive_module._dispatch_interactive_work(
                generation_db,
                work=generation_work,
                worker_id="worker-1",
                message="queued message",
                initial_message=InternalMessage(id=1, role=MessageRole.USER, content="queued message"),
                history_before_id=1,
                frozen_user_message_ids=[1],
                attachments=None,
                allow_additional_user_messages=False,
                execution_resume_state=None,
            )
        finally:
            release_commit.set()
            try:
                if commit_task is not None:
                    await asyncio.wait_for(asyncio.shield(commit_task), timeout=5)
            finally:
                if commit_listener is not None:
                    event.remove(engine.sync_engine, "commit", commit_listener)

    assert response == {**expected_response, "response_id": "response-main"}
    assert generation_work.execution_state["request_ids"] == ["request-1"]

    async with session_factory() as verify_db:
        result = await verify_db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == work_id).order_by(SessionReplyStreamEvent.sequence_no))
        events = list(result.scalars().all())

    assert [stream_event.event["type"] for stream_event in events] == [
        "input_dequeued",
        "agent_loop_start",
        "agent_loop_start",
    ]
    assert [stream_event.sequence_no for stream_event in events] == [1, 2, 3]
    assert events[0].event["request_ids"] == ["request-1", "request-2"]
    assert sum(stream_event.event["type"] == "input_dequeued" for stream_event in events) == 1
    assert [stream_event.work_id for stream_event in events] == [work_id, work_id, work_id]
    assert [stream_event.event["work_id"] for stream_event in events] == [work_id, work_id, work_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_type",
    [SessionReplyWorkType.FOREGROUND_REPLY, SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY],
)
async def test_failed_reply_reuses_persisted_message_id_across_notification_history_and_stream_replay(
    database_factory,
    monkeypatch,
    work_type: SessionReplyWorkType,
):
    uid = "reply-failure-user"
    session_id = f"reply-failure-session-{work_type.value}"
    worker_id = "reply-failure-worker"
    request_ids = ["reply-request-1", "reply-request-2"]
    user_error = "测试错误" + "完整错误正文" * 100
    crud = CRUDSessionReplyWorkItem()

    async with database_factory(foreign_keys=True, wal=True) as session_factory:
        async with session_factory() as db:
            profile = Profile(id=1, uid=uid, name="reply failure profile", configs={})
            db.add_all(
                [
                    User(uid=uid, username="reply-failure-user"),
                    profile,
                    ChatSession(session_id=session_id, uid=uid, profile_id=profile.id, source="http", reply_target_source="http"),
                ]
            )
            await db.flush()

            task = None
            if work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
                task = BackgroundTask(
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile.id,
                    tool_call_id="reply-failure-tool-call",
                    tool_name="reply-failure-tool",
                    status=BackgroundTaskStatus.SUCCEEDED,
                    arguments={},
                    result={"status": "succeeded"},
                    auto_reply=True,
                    reply_status=BackgroundTaskReplyStatus.RUNNING,
                )
                db.add(task)
                await db.flush()

            source_id = task.id if task is not None else 1
            work, created = await crud.enqueue(
                db,
                uid=uid,
                session_id=session_id,
                profile_id=profile.id,
                work_type=work_type,
                source_type=SessionReplySourceType.BACKGROUND_TASK if task is not None else SessionReplySourceType.USER_MESSAGE,
                source_id=source_id,
                dedupe_key=f"reply-failure:{work_type.value}",
            )
            assert created is True
            work.execution_state = {"request_ids": request_ids}
            db.add(work)
            await db.commit()

            claimed = await crud.claim_next(db, worker_id=worker_id, lease_seconds=300)
            assert claimed is not None
            assert claimed.id is not None
            work_id = claimed.id

        sent_events: list[dict] = []

        async def collect_session_event(_uid: str, _session_id: str, event: dict) -> None:
            sent_events.append(event)

        monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr("app.providers.database.AsyncSessionLocal", session_factory)
        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", collect_session_event)

        await executor_lifecycle_module.fail_session_reply_work(
            work_id,
            worker_id,
            "worker failure",
            user_error=user_error,
        )
        await executor_lifecycle_module.fail_session_reply_work(
            work_id,
            worker_id,
            "worker failure",
            user_error=user_error,
        )

        async with session_factory() as db:
            persisted_work = await crud.get(db, work_id)
            assert persisted_work is not None
            history = await message_crud.get_history(db, session_id=session_id, uid=uid)
            error_messages = [message for message in history if message.role == MessageRole.ERR]
            assert len(error_messages) == 1
            error_message = error_messages[0]
            assert error_message.id is not None
            assert error_message.content == user_error
            assert persisted_work.result_message_id == error_message.id
            assert persisted_work.status == SessionReplyWorkStatus.FAILED

            if task is not None:
                persisted_task = await db.get(BackgroundTask, task.id)
                assert persisted_task is not None
                assert persisted_task.status == BackgroundTaskStatus.SUCCEEDED
                assert persisted_task.reply_status == BackgroundTaskReplyStatus.FAILED

        assert len(sent_events) == 1
        notification = sent_events[0]
        assert notification["type"] == "proactive_reply_error"
        assert notification["message_id"] == error_message.id
        assert notification["event_id"]
        assert notification["work_id"] == work_id
        assert notification["request_ids"] == request_ids
        assert notification["content"] == user_error

        manager = SessionReplyQueueManager()
        stream_events = [event async for event in manager.wait_for_stream(work_id)]
        replayed_events = [event async for event in manager.wait_for_stream(work_id, resume_mode=True)]

        assert len(stream_events) == 1
        assert len(replayed_events) == 1
        for stream_event in [stream_events[0], replayed_events[0]]:
            assert stream_event["type"] == "error"
            assert stream_event["message_id"] == error_message.id
            assert stream_event["event_id"] == notification["event_id"]
            assert stream_event["work_id"] == notification["work_id"] == work_id
            assert stream_event["request_ids"] == notification["request_ids"] == request_ids
            assert stream_event["message"] == error_message.content == user_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["missing_single", "persisted_result", "parallel", "complete_checkpoint"],
)
async def test_foreground_reply_restores_tool_results_before_dispatch(db_session: AsyncSession, monkeypatch, scenario: str):
    session_id, uid, worker_id, profile_id = "session-1", "user-1", "worker-1", 1
    guidance = "[系统提示信息]请停止所有操作[系统提示信息结束]"
    attachments = ["/tmp/attachment.txt"]
    environment_prompt = json.dumps(
        {"environment": {"runtime_context": "runtime-context"}, "response_settings": {"markdown": True, "max_output_tokens": 321}, "platform_constraints": "platform-constraints"},
        ensure_ascii=False,
    )
    call_ids = {
        "missing_single": ["call-1"],
        "persisted_result": ["call-1"],
        "parallel": ["call-1", "call-2", "call-3"],
        "complete_checkpoint": ["call-1", "call-2", "call-3"],
    }[scenario]
    real_content = ShellNonInteractiveCompletedResult(
        stdout="真实结果",
        stderr="",
        exit_code=0,
        system_info="test-system",
    ).model_dump_json()

    def serialize(message: InternalMessage) -> dict:
        return message.model_dump(mode="json", exclude_none=True)

    def tool_call(call_id: str) -> InternalToolCall:
        return InternalToolCall(id=call_id, name="execute_shell", arguments={"command": "echo test"})

    def tool_result(call_id: str, message_id: int) -> InternalMessage:
        return InternalMessage(
            id=message_id,
            role=MessageRole.TOOL,
            tool_call_id=call_id,
            content=real_content,
            tool_execution_status="succeeded",
        )

    original = InternalMessage(id=1, role=MessageRole.USER, content="原任务")
    assistant = InternalMessage(id=2, role=MessageRole.ASSISTANT, tool_calls=[tool_call(call_id) for call_id in call_ids])
    continuation = InternalMessage(
        id=3,
        role=MessageRole.USER,
        content="继续",
        guidance_prompt=guidance,
        attachments=attachments,
        environment_prompt=environment_prompt,
    )
    checkpoint_result_specs = {"parallel": [("call-1", 4)], "complete_checkpoint": [(c, 4 + i) for i, c in enumerate(call_ids)]}.get(scenario, [])
    database_result_specs = {"persisted_result": [("call-1", 210)], "parallel": [("call-1", 4), ("call-2", 5)], "complete_checkpoint": [(c, 4 + i) for i, c in enumerate(call_ids)]}.get(scenario, [])
    checkpoint_results = {call_id: tool_result(call_id, message_id) for call_id, message_id in checkpoint_result_specs}
    database_results = {call_id: (message_id, tool_result(call_id, message_id)) for call_id, message_id in database_result_specs}
    checkpoint_tool_messages = [serialize(message) for message in checkpoint_results.values()]
    checkpoint = {
        "messages": [serialize(original), serialize(assistant), *checkpoint_tool_messages, serialize(continuation)],
        "turn_messages": [serialize(assistant), *checkpoint_tool_messages, serialize(continuation)],
        "current_turn": 7,
        "total_output_tokens": 9,
        "files_to_user": [],
    }
    initial_checkpoint = copy.deepcopy(checkpoint)

    db_session.add(ChatSession(session_id=session_id, uid=uid, profile_id=profile_id))
    message_fields = {"uid": uid, "session_id": session_id, "profile_id": profile_id, "is_processed": True}
    db_session.add_all(
        [
            Message(id=1, **message_fields, role=MessageRole.USER, type=MessageType.TEXT, content="原任务"),
            Message(id=2, **message_fields, role=MessageRole.ASSISTANT, type=MessageType.TOOL_CALL, content=assistant.model_dump_json(exclude_none=True)),
            Message(id=3, **message_fields, role=MessageRole.USER, type=MessageType.TEXT, content="继续", guidance_prompt=guidance, attachments=attachments, environment_prompt=environment_prompt),
        ]
    )
    if scenario == "persisted_result":
        db_session.add_all([Message(id=i, **message_fields, role=MessageRole.USER, type=MessageType.TEXT, content=f"填充消息 {i}") for i in range(4, 210)])
    db_session.add_all([Message(id=message_id, **message_fields, role=MessageRole.TOOL, type=MessageType.TOOL_RESULT, content=result.model_dump_json(exclude_none=True)) for _call_id, (message_id, result) in database_results.items()])
    await db_session.flush()

    work = await enqueue(
        CRUDSessionReplyWorkItem(),
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key=f"foreground-tool-recovery:{scenario}",
    )
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = 9999999999
    work.input_message_ids = [1]
    work.execution_state = {"stream_requested": False, "dispatcher_checkpoint": checkpoint}
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    await db_session.commit()

    async def load_messages() -> list[Message]:
        result = await db_session.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid).order_by(Message.id).execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def load_tool_results() -> dict[str, tuple[int, InternalMessage]]:
        result = await db_session.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid, Message.type == MessageType.TOOL_RESULT).order_by(Message.id).execution_options(populate_existing=True))
        parsed = {}
        for row in result.scalars().all():
            message = InternalMessage.model_validate_json(row.content or "{}")
            assert row.id is not None and message.tool_call_id is not None
            parsed[message.tool_call_id] = (row.id, message)
        return parsed

    def check_protocol(provider_messages: list[dict]) -> None:
        assistant_calls = [call["id"] for message in provider_messages if message.get("role") == "assistant" for call in message.get("tool_calls", [])]
        tool_messages = [(i, m) for i, m in enumerate(provider_messages) if m.get("role") == "tool"]
        tool_call_ids = [message["tool_call_id"] for _, message in tool_messages]
        assert set(assistant_calls) == set(call_ids) and len(assistant_calls) == len(call_ids)
        assert set(tool_call_ids) == set(call_ids) and len(tool_call_ids) == len(call_ids)
        assistant_index = next(i for i, m in enumerate(provider_messages) if m.get("role") == "assistant" and m.get("tool_calls"))
        continuation_index = next(i for i, m in enumerate(provider_messages) if m.get("role") == "user" and json.loads(m["content"]).get("user_message") == "继续")
        assert assistant_index < continuation_index
        assert all(assistant_index < index < continuation_index for index, _ in tool_messages)
        payload = json.loads(provider_messages[continuation_index]["content"])
        assert (payload["user_message"], payload["platform_guidance"], payload["attachment_paths"]) == ("继续", guidance, attachments)
        assert (
            payload["environment"],
            payload["response_settings"],
            payload["platform_constraints"],
        ) == (
            {"runtime_context": "runtime-context"},
            {"markdown": True, "max_output_tokens": 321},
            "platform-constraints",
        )

    dispatched_checkpoints = []
    test_session_factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", test_session_factory)
    expected_response = llm_response_payload("recovered reply", history=[])

    async def dispatch(**kwargs):
        resume_state = kwargs["execution_resume_state"]
        async with test_session_factory() as verify_db:
            saved = await verify_db.get(SessionReplyWorkItem, work.id)
            assert saved is not None
            assert saved.execution_state["dispatcher_checkpoint"] == resume_state
        internal_messages = [InternalMessage.model_validate(message) for message in resume_state["messages"]]
        materialized_messages = materialize_user_environment_prompts(internal_messages)
        provider_messages = OpenAIChatCompletionsTransformer.to_provider(materialized_messages)
        check_protocol(provider_messages)
        dispatched_checkpoints.append(copy.deepcopy(resume_state))
        return expected_response

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    initial_message_count = len(await load_messages())
    expected_new_results = {"missing_single": 1, "persisted_result": 0, "parallel": 1, "complete_checkpoint": 0}[scenario]
    result_snapshots, message_counts, checkpoint_snapshots = [], [], []

    for _ in range(2):
        assert await executor_replies_module._execute_foreground(db_session, work, worker_id) == expected_response
        await db_session.refresh(work)
        saved_work_result = await db_session.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == work.id).execution_options(populate_existing=True))
        saved_work = saved_work_result.scalar_one()
        saved_checkpoint = saved_work.execution_state["dispatcher_checkpoint"]
        checkpoint_snapshots.append(copy.deepcopy(saved_checkpoint))
        assert saved_work.status == SessionReplyWorkStatus.RUNNING
        assert saved_work.locked_by == worker_id
        assert {key: saved_checkpoint[key] for key in ("current_turn", "total_output_tokens", "files_to_user")} == {
            "current_turn": 7,
            "total_output_tokens": 9,
            "files_to_user": [],
        }
        if scenario == "complete_checkpoint":
            assert saved_checkpoint == initial_checkpoint

        checkpoint_tool_maps = []
        for key in ("messages", "turn_messages"):
            messages = [InternalMessage.model_validate(raw) for raw in saved_checkpoint[key]]
            tools = [message for message in messages if message.role == MessageRole.TOOL]
            tool_map = {message.tool_call_id: message for message in tools}
            assert len(tools) == len(call_ids) and set(tool_map) == set(call_ids)
            assistant_index = next(i for i, m in enumerate(messages) if m.role == MessageRole.ASSISTANT and m.tool_calls)
            continuation_index = next(i for i, m in enumerate(messages) if m.role == MessageRole.USER and m.content == "继续")
            assert all(assistant_index < messages.index(message) < continuation_index for message in tools)
            checkpoint_tool_maps.append(tool_map)

        def tool_signature(values):
            return {call_id: (m.id, m.content, m.tool_execution_status) for call_id, m in values.items()}

        assert tool_signature(checkpoint_tool_maps[0]) == tool_signature(checkpoint_tool_maps[1])

        persisted = await load_tool_results()
        assert set(persisted) == set(call_ids)
        for call_id, (result_id, result) in database_results.items():
            row_id, stored = persisted[call_id]
            actual = checkpoint_tool_maps[0][call_id]
            assert row_id == result_id
            assert stored.content == result.content and stored.tool_execution_status == "succeeded"
            assert actual.id == result_id and actual.id == result.id
            assert actual.content == result.content
            assert actual.tool_execution_status == "succeeded"
        for call_id in set(call_ids) - set(database_results) - set(checkpoint_results):
            row_id, stored = persisted[call_id]
            unknown = {"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED}
            assert json.loads(stored.content or "{}") == unknown and stored.tool_execution_status == "execution_unknown"
            recovered = checkpoint_tool_maps[0][call_id]
            assert recovered.id == row_id and json.loads(recovered.content or "{}") == unknown
            assert recovered.tool_execution_status == "execution_unknown"

        result_snapshots.append({call_id: (row_id, result.content, result.tool_execution_status) for call_id, (row_id, result) in persisted.items()})
        message_counts.append(len(await load_messages()))
        assert message_counts[-1] == initial_message_count + expected_new_results

    assert result_snapshots[0] == result_snapshots[1]
    assert message_counts[0] == message_counts[1]
    assert checkpoint_snapshots[0] == checkpoint_snapshots[1]
    assert len(dispatched_checkpoints) == 2 and dispatched_checkpoints[0] == dispatched_checkpoints[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "historical_checkpoint_current_missing",
        "both_rounds_from_database",
        "both_rounds_unknown",
        "same_assistant_repeated_id_unknown",
        "parallel_checkpoint_plus_unknown",
        "parallel_checkpoint_plus_database",
        "parallel_database_results_across_page",
        "later_round_result_stays_in_later_round",
        "future_database_result_is_not_borrowed",
    ],
)
async def test_foreground_reply_restores_reused_tool_call_ids_by_turn_and_occurrence(
    db_session: AsyncSession,
    monkeypatch,
    scenario: str,
):
    session_id, uid, worker_id, profile_id, call_id = "session-1", "user-1", "worker-1", 1, "reused-call"

    def result(row_id: int, label: str) -> tuple[int, str]:
        body = ShellNonInteractiveCompletedResult(
            stdout=label,
            stderr="",
            exit_code=0,
            system_info="test-system",
        ).model_dump_json()
        return row_id, body

    def turn(assistant_id: int, calls: list[str], checkpoint=(), database=(), expected=()) -> dict:
        return dict(assistant_id=assistant_id, calls=calls, checkpoint=checkpoint, database=database, expected=expected)

    hist, db1, db2 = result(20, "history"), result(20, "db-round-1"), result(50, "db-round-2")
    cp, db_parallel, page1, page2 = result(20, "checkpoint"), result(21, "parallel-db"), result(210, "page-1"), result(211, "page-2")
    later, future = result(50, "later-round"), result(40, "future-round")
    specs = {
        "historical_checkpoint_current_missing": ([turn(10, ["old"], [hist], [hist], [20]), turn(40, ["current"], expected=[None])], 1),
        "both_rounds_from_database": ([turn(10, ["first"], database=[db1], expected=[20]), turn(40, ["second"], database=[db2], expected=[50])], 1),
        "both_rounds_unknown": ([turn(10, ["round-1"], expected=[None]), turn(40, ["round-2"], expected=[None])], 1),
        "same_assistant_repeated_id_unknown": ([turn(10, ["parallel-1", "parallel-2"], expected=[None, None])], 0),
        "parallel_checkpoint_plus_unknown": ([turn(10, ["parallel-1", "parallel-2"], [cp], [cp], [20, None])], 0),
        "parallel_checkpoint_plus_database": ([turn(10, ["parallel-1", "parallel-2"], [cp], [cp, db_parallel], [20, 21])], 0),
        "parallel_database_results_across_page": ([turn(10, ["page-1", "page-2"], database=[page1, page2], expected=[210, 211])], 0),
        "later_round_result_stays_in_later_round": ([turn(10, ["early"], expected=[None]), turn(40, ["later"], [later], [later], [50])], 1),
        "future_database_result_is_not_borrowed": ([turn(10, ["current"], expected=[None])], 0),
    }
    rounds, current_index = specs[scenario]
    current = rounds[current_index]
    continuation_id = 20 if scenario == "future_database_result_is_not_borrowed" else 500

    def tool_result(row_id: int, content: str, status: str = "succeeded") -> InternalMessage:
        return InternalMessage(id=row_id, role=MessageRole.TOOL, tool_call_id=call_id, content=content, tool_execution_status=status)

    def assistant(turn_spec: dict) -> InternalMessage:
        return InternalMessage(
            id=turn_spec["assistant_id"],
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id=call_id, name="execute_shell", arguments={"command": command}) for command in turn_spec["calls"]],
        )

    def serialize(message: InternalMessage) -> dict:
        return message.model_dump(mode="json", exclude_none=True)

    assistants = [assistant(turn) for turn in rounds]
    checkpoint_results = [[tool_result(*item) for item in turn["checkpoint"]] for turn in rounds]
    continuation = InternalMessage(id=continuation_id, role=MessageRole.USER, content="继续")
    original = InternalMessage(id=1, role=MessageRole.USER, content="原任务")
    checkpoint = {
        "messages": [serialize(original), *[raw for a, rs in zip(assistants, checkpoint_results) for raw in [serialize(a), *(serialize(r) for r in rs)]], serialize(continuation)],
        "turn_messages": [serialize(assistants[current_index]), *(serialize(r) for r in checkpoint_results[current_index]), serialize(continuation)],
        "current_turn": 7,
        "total_output_tokens": 9,
        "files_to_user": [],
    }

    db_session.add(ChatSession(session_id=session_id, uid=uid, profile_id=profile_id))
    fields = {"uid": uid, "session_id": session_id, "profile_id": profile_id, "is_processed": True}
    db_messages = [
        Message(id=1, **fields, role=MessageRole.USER, type=MessageType.TEXT, content="原任务"),
        *(Message(id=a.id, **fields, role=MessageRole.ASSISTANT, type=MessageType.TOOL_CALL, content=a.model_dump_json(exclude_none=True)) for a in assistants),
        *(Message(id=row_id, **fields, role=MessageRole.TOOL, type=MessageType.TOOL_RESULT, content=tool_result(row_id, body).model_dump_json(exclude_none=True)) for t in rounds for row_id, body in t["database"]),
        *(Message(id=i, **fields, role=MessageRole.USER, type=MessageType.TEXT, content=f"fill {i}") for i in (range(11, 210) if scenario == "parallel_database_results_across_page" else ())),
        Message(id=continuation.id, **fields, role=MessageRole.USER, type=MessageType.TEXT, content="继续"),
    ]
    if scenario == "future_database_result_is_not_borrowed":
        future_assistant = assistant(turn(30, ["future-other-assistant"]))
        db_messages.extend(
            [
                Message(id=future_assistant.id, **fields, role=MessageRole.ASSISTANT, type=MessageType.TOOL_CALL, content=future_assistant.model_dump_json(exclude_none=True)),
                Message(id=future[0], **fields, role=MessageRole.TOOL, type=MessageType.TOOL_RESULT, content=tool_result(*future).model_dump_json(exclude_none=True)),
            ]
        )
    db_session.add_all(db_messages)
    await db_session.flush()

    work = await enqueue(
        CRUDSessionReplyWorkItem(),
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key=f"foreground-reused-call:{scenario}",
    )
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = 9999999999
    work.input_message_ids = [1]
    work.execution_state = {"stream_requested": False, "dispatcher_checkpoint": checkpoint}
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    await db_session.commit()

    async def load_messages() -> list[Message]:
        result = await db_session.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid))
        return list(result.scalars().all())

    async def tool_rows() -> list[tuple[int, InternalMessage]]:
        result = await db_session.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid, Message.type == MessageType.TOOL_RESULT).order_by(Message.id))
        rows = []
        for row in result.scalars().all():
            assert row.id is not None
            message = InternalMessage.model_validate_json(row.content or "{}")
            message.id = row.id
            rows.append((row.id, message))
        return rows

    def results_after(messages: list[InternalMessage], assistant_id: int) -> list[InternalMessage]:
        start = next(i for i, m in enumerate(messages) if m.role == MessageRole.ASSISTANT and m.id == assistant_id)
        end = next((i for i in range(start + 1, len(messages)) if messages[i].role in (MessageRole.ASSISTANT, MessageRole.USER)), len(messages))
        return [m for m in messages[start + 1 : end] if m.role == MessageRole.TOOL]

    def assert_provider(raw: list[dict], expected_turns: list[dict]) -> None:
        internal = [InternalMessage.model_validate(m) for m in raw]
        provider = OpenAIChatCompletionsTransformer.to_provider(internal)
        positions = [i for i, m in enumerate(provider) if m.get("role") == "assistant" and m.get("tool_calls")]
        assert len(positions) == len(expected_turns)
        covered_tools = []
        for n, turn_spec in enumerate(expected_turns):
            start = positions[n]
            end = next((i for i in range(start + 1, len(provider)) if provider[i].get("role") in ("assistant", "user")), len(provider))
            calls = provider[start]["tool_calls"]
            tools = [m for m in provider[start + 1 : end] if m.get("role") == "tool"]
            covered_tools.extend(i for i in range(start + 1, end) if provider[i].get("role") == "tool")
            raw_tools = results_after(internal, turn_spec["assistant_id"])
            assert len(calls) == len(tools) == len(raw_tools) == len(turn_spec["calls"])
            assert [c["id"] for c in calls] == [m["tool_call_id"] for m in tools] == [call_id] * len(calls)
            for call, command in zip(calls, turn_spec["calls"]):
                args = call["function"]["arguments"]
                if isinstance(args, str):
                    args = json.loads(args)
                assert args == {"command": command}
            assert [m.get("content") for m in tools] == [m.content for m in raw_tools]
        assert covered_tools == [i for i, m in enumerate(provider) if m.get("role") == "tool"]
        assert any(m.role == MessageRole.USER and m.content == "继续" for m in internal)

    test_session_factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", test_session_factory)
    expected_response = llm_response_payload("reused-call reply", history=[])

    async def dispatch(**kwargs):
        resume = kwargs["execution_resume_state"]
        async with test_session_factory() as verify_db:
            saved = await verify_db.get(SessionReplyWorkItem, work.id)
            assert saved is not None and saved.execution_state["dispatcher_checkpoint"] == resume
        assert_provider(resume["messages"], rounds)
        assert_provider(resume["turn_messages"], [current])
        return expected_response

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    initial_rows, initial_messages = await tool_rows(), await load_messages()
    initial_by_id = {row_id: message for row_id, message in initial_rows}
    unknown_count = sum(row_id is None for t in rounds for row_id in t["expected"])
    checkpoints, results_snapshots, message_counts = [], [], []
    for _ in range(2):
        unknown_checkpoint_ids = []
        assert await executor_replies_module._execute_foreground(db_session, work, worker_id) == expected_response
        await db_session.refresh(work)
        saved = await db_session.get(SessionReplyWorkItem, work.id)
        checkpoint = saved.execution_state["dispatcher_checkpoint"]
        checkpoints.append(copy.deepcopy(checkpoint))
        messages = [InternalMessage.model_validate(m) for m in checkpoint["messages"]]
        current_messages = [InternalMessage.model_validate(m) for m in checkpoint["turn_messages"]]
        assert sum(m.role == MessageRole.USER and m.content == "继续" for m in messages) == 1
        assert sum(m.role == MessageRole.USER and m.content == "继续" for m in current_messages) == 1

        for t in rounds:
            assistant = next(m for m in messages if m.role == MessageRole.ASSISTANT and m.id == t["assistant_id"])
            tools = results_after(messages, t["assistant_id"])
            assert len(assistant.tool_calls or []) == len(tools) == len(t["expected"])
            assert [c.id for c in assistant.tool_calls or []] == [m.tool_call_id for m in tools] == [call_id] * len(tools)
            for result_message, expected_id in zip(tools, t["expected"]):
                assert result_message.id is not None
                if expected_id is None:
                    assert result_message.id not in initial_by_id
                    unknown_checkpoint_ids.append(result_message.id)
                    assert result_message.tool_execution_status == "execution_unknown"
                    assert json.loads(result_message.content or "{}") == {"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED}
                else:
                    expected = initial_by_id[expected_id]
                    assert expected.tool_execution_status == "succeeded"
                    assert (result_message.id, result_message.content, result_message.tool_execution_status) == (expected_id, expected.content, expected.tool_execution_status)

        def signature(message: InternalMessage) -> tuple:
            return message.id, message.tool_call_id, message.content, message.tool_execution_status

        assert [signature(m) for m in results_after(current_messages, current["assistant_id"])] == [signature(m) for m in results_after(messages, current["assistant_id"])]
        persisted = await tool_rows()
        persisted_by_id = {row_id: message for row_id, message in persisted}
        for row_id, message in initial_rows:
            assert signature(persisted_by_id[row_id]) == signature(message)
        unknown = [(row_id, m) for row_id, m in persisted if m.tool_execution_status == "execution_unknown"]
        persisted_unknown_ids = {row_id for row_id, _ in unknown}
        assert len(unknown) == len(persisted_unknown_ids) == len(set(unknown_checkpoint_ids)) == unknown_count
        assert persisted_unknown_ids == set(unknown_checkpoint_ids)
        for t in rounds:
            for result_message in results_after(messages, t["assistant_id"]):
                assert signature(persisted_by_id[result_message.id]) == signature(result_message)
        results_snapshots.append([(row_id, signature(m)) for row_id, m in persisted])
        message_counts.append(len(await load_messages()))
        assert message_counts[-1] == len(initial_messages) + unknown_count

    assert checkpoints[0] == checkpoints[1]
    assert results_snapshots[0] == results_snapshots[1]
    assert message_counts[0] == message_counts[1]
