import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.providers.database as database_module
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue import manager_result as manager_result_module
from app.core.session_reply_queue.manager_result import SessionReplyResult
from app.core.utils.time import get_local_time
from app.models.message import InternalMessage
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplyCheckpointMessage,
    SessionReplyCheckpointMessageReference,
    SessionReplySourceType,
    SessionReplyWorkCheckpoint,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)

SESSION_ID = "checkpoint-session"
UID = "checkpoint-user"


@pytest_asyncio.fixture
async def checkpoint_database(
    database_factory,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(foreign_keys=True, wal=True) as session_factory:
        async with session_factory() as db:
            db.add(ChatSession(session_id=SESSION_ID, uid=UID))
            await db.commit()
        yield session_factory


async def _add_session(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> None:
    async with session_factory() as db:
        db.add(ChatSession(session_id=session_id, uid=UID))
        await db.commit()


async def _enqueue_work(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dedupe_key: str,
    session_id: str = SESSION_ID,
    uid: str = UID,
    status: SessionReplyWorkStatus = SessionReplyWorkStatus.READY_FOR_LLM,
    locked_by: str | None = None,
    execution_state: dict[str, Any] | None = None,
    updated_at: datetime | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> int:
    async with session_factory() as db:
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=dedupe_key,
            dedupe_key=dedupe_key,
            commit=False,
        )
        assert created is True
        assert work.id is not None
        work.status = status
        work.locked_by = locked_by
        work.execution_state = {} if execution_state is None else execution_state
        if updated_at is not None:
            work.updated_at = updated_at
        db.add(work)
        if checkpoint is not None:
            await session_reply_work_item_crud.save_checkpoint(db, work.id, checkpoint)
        await db.commit()
        return work.id


async def _claim_work(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dedupe_key: str,
    worker_id: str,
) -> int:
    work_id = await _enqueue_work(session_factory, dedupe_key=dedupe_key)
    async with session_factory() as db:
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert claimed is not None
        assert claimed.id == work_id
    return work_id


@contextmanager
def _record_checkpoint_sql(
    session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[list[str]]:
    engine = session_factory.kw["bind"]
    statements: list[str] = []

    def record_sql(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "session_reply_work_checkpoint" in statement.lower():
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)


@contextmanager
def _record_checkpoint_message_sql(
    session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[list[str]]:
    engine = session_factory.kw["bind"]
    statements: list[str] = []

    def record_sql(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower()
        if "session_reply_checkpoint_message_reference" not in normalized and "session_reply_checkpoint_message" in normalized:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)


@pytest.mark.asyncio
async def test_large_checkpoint_is_separate_and_replaceable(checkpoint_database) -> None:
    worker_id = "checkpoint-worker"
    work_id = await _claim_work(
        checkpoint_database,
        dedupe_key="checkpoint-large",
        worker_id=worker_id,
    )
    lightweight_state = {"lightweight": "state-only"}
    first_checkpoint = {
        "revision": 1,
        "payload": "a" * (2 * 1024 * 1024),
    }

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"execution_state": lightweight_state},
            checkpoint=first_checkpoint,
        )

    async with checkpoint_database() as db:
        persisted = await session_reply_work_item_crud.get(db, work_id)
        assert persisted is not None
        assert persisted.execution_state == lightweight_state
        assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == first_checkpoint

    second_checkpoint = {
        "revision": 2,
        "payload": "b" * (2 * 1024 * 1024),
    }
    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"execution_state": lightweight_state},
            checkpoint=second_checkpoint,
        )

    async with checkpoint_database() as db:
        persisted = await session_reply_work_item_crud.get(db, work_id)
        assert persisted is not None
        assert persisted.execution_state == lightweight_state
        loaded_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work_id)
        assert loaded_checkpoint == second_checkpoint
        assert isinstance(loaded_checkpoint, dict)
        assert len(loaded_checkpoint["payload"]) >= 2 * 1024 * 1024
        row_count = await db.execute(select(func.count()).select_from(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id == work_id))
        assert row_count.scalar_one() == 1
        await session_reply_work_item_crud.delete_checkpoint(db, work_id)
        await db.commit()

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.get_checkpoint(db, work_id) is None


@pytest.mark.asyncio
async def test_checkpoint_write_requires_current_running_claim(checkpoint_database) -> None:
    running_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-lease-current",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-new",
        execution_state={"lightweight": "before"},
    )
    new_state = {"lightweight": "new"}
    new_checkpoint = {"generation": "new"}
    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=running_id,
            worker_id="worker-new",
            values={"execution_state": new_state},
            checkpoint=new_checkpoint,
        )

    async with checkpoint_database() as db:
        assert not await session_reply_work_item_crud.update_claimed(
            db,
            work_id=running_id,
            worker_id="worker-old",
            values={"execution_state": {"lightweight": "old"}},
            checkpoint={"generation": "old"},
        )

    ready_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-not-running",
        execution_state={"lightweight": "ready"},
    )
    async with checkpoint_database() as db:
        assert not await session_reply_work_item_crud.update_claimed(
            db,
            work_id=ready_id,
            worker_id="worker-any",
            values={"execution_state": {"lightweight": "changed"}},
            checkpoint={"generation": "rejected"},
        )

    async with checkpoint_database() as db:
        running = await session_reply_work_item_crud.get(db, running_id)
        ready = await session_reply_work_item_crud.get(db, ready_id)
        assert running is not None
        assert ready is not None
        assert running.status == SessionReplyWorkStatus.RUNNING
        assert running.locked_by == "worker-new"
        assert running.execution_state == new_state
        assert ready.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert ready.execution_state == {"lightweight": "ready"}
        assert await session_reply_work_item_crud.get_checkpoint(db, running_id) == new_checkpoint
        assert await session_reply_work_item_crud.get_checkpoint(db, ready_id) is None


@pytest.mark.asyncio
async def test_commit_false_rolls_back_work_state_and_checkpoint_together(checkpoint_database) -> None:
    worker_id = "rollback-worker"
    before_state = {"phase": "before"}
    before_checkpoint = {"revision": "before"}
    work_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-rollback",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=worker_id,
        execution_state=before_state,
        checkpoint=before_checkpoint,
    )

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"execution_state": {"phase": "after"}},
            checkpoint={"revision": "after"},
            commit=False,
        )
        transient_work = await session_reply_work_item_crud.get(db, work_id)
        transient_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work_id)
        assert transient_work is not None
        assert transient_work.execution_state == {"phase": "after"}
        assert transient_checkpoint == {"revision": "after"}
        await db.rollback()

    async with checkpoint_database() as db:
        persisted_work = await session_reply_work_item_crud.get(db, work_id)
        persisted_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work_id)
        assert persisted_work is not None
        assert persisted_work.execution_state == before_state
        assert persisted_work.status == SessionReplyWorkStatus.RUNNING
        assert persisted_checkpoint == before_checkpoint


@pytest.mark.asyncio
async def test_checkpoint_lifecycle_respects_terminal_retention_and_cleanup(checkpoint_database) -> None:
    success_worker = "success-worker"
    recent_checkpoint = {"label": "recent-success"}
    recent_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-recent-success",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=success_worker,
        execution_state={"response": {"content": "completed"}},
    )
    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=recent_id,
            worker_id=success_worker,
            values={"execution_state": {"response": {"content": "completed"}}},
            checkpoint=recent_checkpoint,
        )
        assert await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=recent_id,
            worker_id=success_worker,
            status=SessionReplyWorkStatus.SUCCEEDED,
        )

    old_time = get_local_time() - timedelta(hours=25)
    old_ready_checkpoint = {"label": "old-ready"}
    old_running_checkpoint = {"label": "old-running"}
    old_waiting_checkpoint = {"label": "old-waiting"}
    old_ready_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-old-ready",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        updated_at=old_time,
        checkpoint=old_ready_checkpoint,
    )
    old_running_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-old-running",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="expired-worker",
        updated_at=old_time,
        checkpoint=old_running_checkpoint,
    )
    old_waiting_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-old-waiting",
        status=SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
        updated_at=old_time,
        checkpoint=old_waiting_checkpoint,
    )

    terminal_ids: list[int] = []
    terminal_statuses = [
        SessionReplyWorkStatus.MERGED,
        SessionReplyWorkStatus.SUCCEEDED,
        SessionReplyWorkStatus.FAILED,
        SessionReplyWorkStatus.CANCELLED,
    ]
    for index, status in enumerate(terminal_statuses, start=1):
        terminal_ids.append(
            await _enqueue_work(
                checkpoint_database,
                dedupe_key=f"checkpoint-old-terminal-{index}",
                status=status,
                updated_at=old_time,
                checkpoint={"label": f"old-terminal-{index}"},
            )
        )

    async with checkpoint_database() as db:
        recent_work = await session_reply_work_item_crud.get(db, recent_id)
        assert recent_work is not None
        assert recent_work.status == SessionReplyWorkStatus.SUCCEEDED
        assert await session_reply_work_item_crud.get_checkpoint(db, recent_id) == recent_checkpoint
        active_expectations = [
            (old_ready_id, SessionReplyWorkStatus.READY_FOR_LLM, old_ready_checkpoint),
            (old_running_id, SessionReplyWorkStatus.RUNNING, old_running_checkpoint),
            (old_waiting_id, SessionReplyWorkStatus.WAITING_EXTERNAL_WORK, old_waiting_checkpoint),
        ]
        for work_id, expected_status, expected_checkpoint in active_expectations:
            work = await session_reply_work_item_crud.get(db, work_id)
            assert work is not None
            assert work.status == expected_status
            assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == expected_checkpoint

    async with checkpoint_database() as db:
        cleanup_result = await session_reply_work_item_crud.cleanup_terminal_items(db)
        assert cleanup_result.work_items == len(terminal_ids)
        assert cleanup_result.stream_events == 0
        assert cleanup_result.sequences == 0

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.get_checkpoint(db, recent_id) == recent_checkpoint
        for work_id, expected_status, expected_checkpoint in active_expectations:
            work = await session_reply_work_item_crud.get(db, work_id)
            assert work is not None
            assert work.status == expected_status
            assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == expected_checkpoint
        for work_id in terminal_ids:
            assert await session_reply_work_item_crud.get(db, work_id) is None
            assert await session_reply_work_item_crud.get_checkpoint(db, work_id) is None


@pytest.mark.asyncio
async def test_session_deletion_paths_cascade_checkpoint_without_cross_session_effects(checkpoint_database) -> None:
    other_session_id = "checkpoint-other-session"
    untouched_session_id = "checkpoint-untouched-session"
    await _add_session(checkpoint_database, other_session_id)
    await _add_session(checkpoint_database, untouched_session_id)

    primary_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-delete-by-session",
        checkpoint={"owner": "primary"},
    )
    other_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-delete-parent",
        session_id=other_session_id,
        checkpoint={"owner": "other"},
    )
    untouched_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-untouched",
        session_id=untouched_session_id,
        checkpoint={"owner": "untouched"},
    )

    async with checkpoint_database() as db:
        deleted_count = await session_reply_work_item_crud.delete_by_session(
            db,
            session_id=SESSION_ID,
            uid=UID,
            is_admin=False,
        )
        assert deleted_count == 1

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.get(db, primary_id) is None
        assert await session_reply_work_item_crud.get_checkpoint(db, primary_id) is None
        assert await session_reply_work_item_crud.get(db, other_id) is not None
        assert await session_reply_work_item_crud.get_checkpoint(db, other_id) == {"owner": "other"}

    async with checkpoint_database() as db:
        other_session = await db.get(ChatSession, other_session_id)
        assert other_session is not None
        await db.delete(other_session)
        await db.commit()

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.get(db, other_id) is None
        assert await session_reply_work_item_crud.get_checkpoint(db, other_id) is None
        untouched_work = await session_reply_work_item_crud.get(db, untouched_id)
        assert untouched_work is not None
        assert untouched_work.session_id == untouched_session_id
        assert await session_reply_work_item_crud.get_checkpoint(db, untouched_id) == {"owner": "untouched"}


@pytest.mark.asyncio
async def test_work_reads_and_wait_for_result_do_not_load_checkpoint(
    checkpoint_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_session_id = "checkpoint-claim-session"
    await _add_session(checkpoint_database, claim_session_id)
    http_state = {"message_source": "http", "lightweight": "read-only"}
    http_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-read-http",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="read-worker",
        execution_state=http_state,
        checkpoint={"opaque": "http-checkpoint"},
    )
    result_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-read-result",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="result-worker",
        execution_state={"lightweight": "pending"},
        checkpoint={"opaque": "result-checkpoint"},
    )
    result_response = {"content": "business-result", "response_id": "checkpoint-response"}
    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=result_id,
            worker_id="result-worker",
            values={"execution_state": {"response": result_response}},
            checkpoint={"opaque": "result-checkpoint-final"},
        )
        assert await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=result_id,
            worker_id="result-worker",
            status=SessionReplyWorkStatus.SUCCEEDED,
        )

    claim_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-read-claim",
        session_id=claim_session_id,
        execution_state={"lightweight": "claim-only"},
        checkpoint={"opaque": "claim-checkpoint"},
    )
    async with checkpoint_database() as db:
        with _record_checkpoint_sql(checkpoint_database) as checkpoint_sql:
            claimed = await session_reply_work_item_crud.claim_next(
                db,
                worker_id="claim-reader",
                lease_seconds=300,
            )
        assert claimed is not None
        assert claimed.id == claim_id
        assert claimed.execution_state == {"lightweight": "claim-only"}
        assert checkpoint_sql == []

    async with checkpoint_database() as db:
        with _record_checkpoint_sql(checkpoint_database) as checkpoint_sql:
            loaded = await session_reply_work_item_crud.get(db, http_id)
        assert loaded is not None
        assert loaded.execution_state == http_state
        assert checkpoint_sql == []

    async with checkpoint_database() as db:
        with _record_checkpoint_sql(checkpoint_database) as checkpoint_sql:
            listed = await session_reply_work_item_crud.list_recent_http_for_sessions(
                db,
                uid=UID,
                is_admin=False,
                session_ids=[SESSION_ID],
            )
        assert [work.id for work in listed] == [http_id]
        assert listed[0].execution_state == http_state
        assert checkpoint_sql == []

    async with checkpoint_database() as db:
        result_work = await session_reply_work_item_crud.get(db, result_id)
        assert result_work is not None
        result_sequence_no = result_work.sequence_no

    monkeypatch.setattr(manager_result_module, "AsyncSessionLocal", checkpoint_database, raising=False)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", checkpoint_database)
    with _record_checkpoint_sql(checkpoint_database) as checkpoint_sql:
        result = await SessionReplyResult().wait_for_result(result_id)
    assert result == {
        **result_response,
        "work_id": result_id,
        "work_sequence_no": result_sequence_no,
    }
    assert checkpoint_sql == []


@pytest.mark.asyncio
async def test_checkpoint_message_payloads_are_content_deduplicated_and_session_scoped(checkpoint_database) -> None:
    other_session_id = "checkpoint-message-other-session"
    await _add_session(checkpoint_database, other_session_id)

    first_message = {
        "id": 7001,
        "role": "user",
        "content": "original user message",
        "tool_call_id": None,
        "created_at": 1793775600.0,
    }
    assistant_message = {
        "id": 7002,
        "role": "assistant",
        "content": "assistant message",
        "tool_call_id": "call-large-result",
        "created_at": 1793775601.0,
    }
    large_tool_result = {
        "id": 7003,
        "role": "tool",
        "content": "tool-result-" + ("x" * (2 * 1024 * 1024 + 1)),
        "tool_call_id": "call-large-result",
        "created_at": 1793775602.0,
    }
    assert len(large_tool_result["content"]) > 2 * 1024 * 1024
    for message in (first_message, assistant_message, large_tool_result):
        InternalMessage.model_validate(message)

    messages = [first_message, large_tool_result, first_message, assistant_message]
    turn_messages = [large_tool_result, assistant_message, first_message, large_tool_result]
    frozen_context = deepcopy(messages)
    shared_checkpoint = {
        "messages": deepcopy(messages),
        "turn_messages": deepcopy(turn_messages),
        "current_turn": 4,
        "waiting": {"kind": "tool", "tool_call_id": "call-large-result"},
    }

    first_worker = "checkpoint-message-worker-1"
    second_worker = "checkpoint-message-worker-2"
    other_worker = "checkpoint-message-worker-other"
    first_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-message-dedupe-1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=first_worker,
    )
    second_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-message-dedupe-2",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=second_worker,
    )
    other_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-message-dedupe-other",
        session_id=other_session_id,
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=other_worker,
    )

    async with checkpoint_database() as db:
        first_frozen_ids = await session_reply_work_item_crud.save_frozen_context(db, first_id, frozen_context)
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=first_id,
            worker_id=first_worker,
            values={"execution_state": {"frozen_context_message_ids": first_frozen_ids}},
            checkpoint=shared_checkpoint,
        )

    async with checkpoint_database() as db:
        second_frozen_ids = await session_reply_work_item_crud.save_frozen_context(db, second_id, frozen_context)
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=second_id,
            worker_id=second_worker,
            values={"execution_state": {"frozen_context_message_ids": second_frozen_ids}},
            checkpoint=shared_checkpoint,
        )

    async with checkpoint_database() as db:
        other_frozen_ids = await session_reply_work_item_crud.save_frozen_context(db, other_id, frozen_context)
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=other_id,
            worker_id=other_worker,
            values={"execution_state": {"frozen_context_message_ids": other_frozen_ids}},
            checkpoint=shared_checkpoint,
        )

    first_checkpoint_ids: dict[str, Any]
    second_checkpoint_ids: dict[str, Any]
    first_session_body_ids: set[int]
    async with checkpoint_database() as db:
        first_work = await session_reply_work_item_crud.get(db, first_id)
        second_work = await session_reply_work_item_crud.get(db, second_id)
        other_work = await session_reply_work_item_crud.get(db, other_id)
        assert first_work is not None
        assert second_work is not None
        assert other_work is not None
        assert first_work.execution_state == {"frozen_context_message_ids": first_frozen_ids}
        assert second_work.execution_state == {"frozen_context_message_ids": second_frozen_ids}
        assert other_work.execution_state == {"frozen_context_message_ids": other_frozen_ids}

        first_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, first_id)
        second_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, second_id)
        other_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, other_id)
        assert first_checkpoint == shared_checkpoint
        assert second_checkpoint == shared_checkpoint
        assert other_checkpoint == shared_checkpoint
        assert await session_reply_work_item_crud.get_frozen_context(db, first_work) == frozen_context
        assert await session_reply_work_item_crud.get_frozen_context(db, second_work) == frozen_context
        assert await session_reply_work_item_crud.get_frozen_context(db, other_work) == frozen_context

        first_checkpoint_row = await db.get(SessionReplyWorkCheckpoint, first_id)
        second_checkpoint_row = await db.get(SessionReplyWorkCheckpoint, second_id)
        assert first_checkpoint_row is not None
        assert second_checkpoint_row is not None
        first_checkpoint_ids = deepcopy(first_checkpoint_row.checkpoint)
        second_checkpoint_ids = deepcopy(second_checkpoint_row.checkpoint)
        serialized_checkpoint = json.dumps(first_checkpoint_row.checkpoint, ensure_ascii=False, separators=(",", ":"))
        assert len(serialized_checkpoint) < 8 * 1024
        assert "content" not in serialized_checkpoint
        for key in ("messages", "turn_messages"):
            assert all(isinstance(message_id, int) for message_id in first_checkpoint_row.checkpoint[key])

        first_body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        first_body_rows = list(first_body_result.scalars().all())
        assert len(first_body_rows) == 3
        assert len({json.dumps(row.payload, sort_keys=True, separators=(",", ":")) for row in first_body_rows}) == 3
        for expected_payload in (first_message, assistant_message, large_tool_result):
            assert sum(row.payload == expected_payload for row in first_body_rows) == 1
        first_session_body_ids = {row.id for row in first_body_rows if row.id is not None}
        assert set(first_checkpoint_row.checkpoint["messages"]) | set(first_checkpoint_row.checkpoint["turn_messages"]) == first_session_body_ids

        other_body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == other_session_id).order_by(SessionReplyCheckpointMessage.id))
        other_body_rows = list(other_body_result.scalars().all())
        assert len(other_body_rows) == 3
        assert {row.id for row in other_body_rows if row.id is not None}.isdisjoint(first_session_body_ids)
        for expected_payload in (first_message, assistant_message, large_tool_result):
            assert sum(row.payload == expected_payload for row in other_body_rows) == 1

    changed_first_message = {
        **first_message,
        "content": "changed user message with the same message id",
    }
    changed_messages = [changed_first_message, large_tool_result, changed_first_message, assistant_message]
    changed_turn_messages = [large_tool_result, assistant_message, changed_first_message, large_tool_result]
    changed_frozen_context = deepcopy(changed_messages)
    changed_checkpoint = {
        "messages": deepcopy(changed_messages),
        "turn_messages": deepcopy(changed_turn_messages),
        "current_turn": 5,
        "waiting": {"kind": "tool", "tool_call_id": "call-large-result"},
    }
    assert changed_first_message["id"] == first_message["id"]

    async with checkpoint_database() as db:
        changed_frozen_ids = await session_reply_work_item_crud.save_frozen_context(db, second_id, changed_frozen_context)
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=second_id,
            worker_id=second_worker,
            values={"execution_state": {"frozen_context_message_ids": changed_frozen_ids}},
            checkpoint=changed_checkpoint,
        )

    async with checkpoint_database() as db:
        first_work = await session_reply_work_item_crud.get(db, first_id)
        second_work = await session_reply_work_item_crud.get(db, second_id)
        other_work = await session_reply_work_item_crud.get(db, other_id)
        assert first_work is not None
        assert second_work is not None
        assert other_work is not None
        assert await session_reply_work_item_crud.get_checkpoint(db, first_id) == shared_checkpoint
        assert await session_reply_work_item_crud.get_checkpoint(db, second_id) == changed_checkpoint
        assert await session_reply_work_item_crud.get_checkpoint(db, other_id) == shared_checkpoint
        assert await session_reply_work_item_crud.get_frozen_context(db, first_work) == frozen_context
        assert await session_reply_work_item_crud.get_frozen_context(db, second_work) == changed_frozen_context
        assert await session_reply_work_item_crud.get_frozen_context(db, other_work) == frozen_context
        assert first_work.execution_state == {"frozen_context_message_ids": first_frozen_ids}
        assert second_work.execution_state == {"frozen_context_message_ids": changed_frozen_ids}

        first_checkpoint_row = await db.get(SessionReplyWorkCheckpoint, first_id)
        second_checkpoint_row = await db.get(SessionReplyWorkCheckpoint, second_id)
        assert first_checkpoint_row is not None
        assert second_checkpoint_row is not None
        assert first_checkpoint_row.checkpoint == first_checkpoint_ids
        assert second_checkpoint_row.checkpoint["messages"] != second_checkpoint_ids["messages"]
        assert second_checkpoint_row.checkpoint["turn_messages"] != second_checkpoint_ids["turn_messages"]

        first_body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        first_body_rows = list(first_body_result.scalars().all())
        assert len(first_body_rows) == 4
        assert len({json.dumps(row.payload, sort_keys=True, separators=(",", ":")) for row in first_body_rows}) == 4
        for expected_payload in (first_message, changed_first_message, assistant_message, large_tool_result):
            assert sum(row.payload == expected_payload for row in first_body_rows) == 1
        assert sum(row.payload["id"] == first_message["id"] for row in first_body_rows) == 2

        other_body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == other_session_id).order_by(SessionReplyCheckpointMessage.id))
        other_body_rows = list(other_body_result.scalars().all())
        assert len(other_body_rows) == 3
        assert {row.id for row in other_body_rows if row.id is not None}.isdisjoint({row.id for row in first_body_rows if row.id is not None})
        for expected_payload in (first_message, assistant_message, large_tool_result):
            assert sum(row.payload == expected_payload for row in other_body_rows) == 1


@pytest.mark.asyncio
async def test_checkpoint_message_payloads_support_incremental_metadata_claims_and_rollback(checkpoint_database) -> None:
    worker_id = "checkpoint-message-incremental-worker"
    initial_message = {
        "id": 7101,
        "role": "user",
        "content": "initial message",
        "tool_call_id": None,
        "created_at": 1793775610.0,
    }
    large_tool_result = {
        "id": 7102,
        "role": "tool",
        "content": "large-tool-result-" + ("y" * (2 * 1024 * 1024 + 1)),
        "tool_call_id": "call-incremental",
        "created_at": 1793775611.0,
    }
    assert len(large_tool_result["content"]) > 2 * 1024 * 1024
    for message in (initial_message, large_tool_result):
        InternalMessage.model_validate(message)
    initial_messages = [initial_message, large_tool_result, initial_message]
    initial_turn_messages = [large_tool_result, initial_message, large_tool_result]
    initial_frozen_context = deepcopy(initial_messages)
    initial_checkpoint = {
        "messages": deepcopy(initial_messages),
        "turn_messages": deepcopy(initial_turn_messages),
        "current_turn": 1,
        "waiting": {"kind": "none"},
    }
    work_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-message-incremental",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=worker_id,
    )

    async with checkpoint_database() as db:
        frozen_ids = await session_reply_work_item_crud.save_frozen_context(db, work_id, initial_frozen_context)
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"execution_state": {"frozen_context_message_ids": frozen_ids}},
            checkpoint=initial_checkpoint,
        )

    async with checkpoint_database() as db:
        initial_work = await session_reply_work_item_crud.get(db, work_id)
        assert initial_work is not None
        initial_state = deepcopy(initial_work.execution_state)
        initial_restored_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work_id)
        initial_restored_frozen = await session_reply_work_item_crud.get_frozen_context(db, initial_work)
        assert initial_restored_checkpoint == initial_checkpoint
        assert initial_restored_frozen == initial_frozen_context
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        initial_body_snapshot = [(row.id, deepcopy(row.payload)) for row in body_result.scalars().all()]
        assert len(initial_body_snapshot) == 2
        reference_result = await db.execute(select(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.work_id == work_id).order_by(SessionReplyCheckpointMessageReference.kind, SessionReplyCheckpointMessageReference.message_id))
        initial_reference_snapshot = [(row.kind, row.message_id) for row in reference_result.scalars().all()]
        assert len(initial_reference_snapshot) == 4
        initial_checkpoint_row = await db.get(SessionReplyWorkCheckpoint, work_id)
        assert initial_checkpoint_row is not None
        initial_lightweight_checkpoint = deepcopy(initial_checkpoint_row.checkpoint)

    metadata_state = {"frozen_context_message_ids": list(initial_state["frozen_context_message_ids"])}
    metadata_checkpoint = {
        **deepcopy(initial_checkpoint),
        "current_turn": 2,
        "waiting": {"kind": "external", "work_id": 8801},
    }
    async with checkpoint_database() as db:
        with _record_checkpoint_message_sql(checkpoint_database) as message_sql:
            assert await session_reply_work_item_crud.update_claimed(
                db,
                work_id=work_id,
                worker_id=worker_id,
                values={"execution_state": metadata_state},
                commit=False,
            )
            await session_reply_work_item_crud.save_checkpoint_metadata(
                db,
                work_id,
                {"current_turn": 2, "waiting": {"kind": "external", "work_id": 8801}},
            )
            await db.commit()
        assert message_sql == []

    async with checkpoint_database() as db:
        updated_work = await session_reply_work_item_crud.get(db, work_id)
        assert updated_work is not None
        assert updated_work.execution_state == metadata_state
        assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == metadata_checkpoint
        assert await session_reply_work_item_crud.get_frozen_context(db, updated_work) == initial_restored_frozen
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        assert [(row.id, row.payload) for row in body_result.scalars().all()] == initial_body_snapshot

    async with checkpoint_database() as db:
        with _record_checkpoint_sql(checkpoint_database) as checkpoint_sql:
            assert await session_reply_work_item_crud.update_claimed(
                db,
                work_id=work_id,
                worker_id=worker_id,
                values={"execution_state": metadata_state},
                checkpoint=metadata_checkpoint,
            )
        assert not any(("insert" in statement.lower() or "update" in statement.lower()) and "session_reply_work_checkpoint" in statement.lower() for statement in checkpoint_sql)

    async with checkpoint_database() as db:
        same_snapshot_work = await session_reply_work_item_crud.get(db, work_id)
        assert same_snapshot_work is not None
        assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == metadata_checkpoint
        assert await session_reply_work_item_crud.get_frozen_context(db, same_snapshot_work) == initial_restored_frozen
        checkpoint_row = await db.get(SessionReplyWorkCheckpoint, work_id)
        assert checkpoint_row is not None
        assert checkpoint_row.checkpoint == metadata_checkpoint | {
            "messages": initial_lightweight_checkpoint["messages"],
            "turn_messages": initial_lightweight_checkpoint["turn_messages"],
        }
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        assert [(row.id, row.payload) for row in body_result.scalars().all()] == initial_body_snapshot

    stale_message = {
        **initial_message,
        "content": "stale worker message should not be persisted",
    }
    stale_checkpoint = {
        "messages": [stale_message, large_tool_result, stale_message],
        "turn_messages": [large_tool_result, stale_message, large_tool_result],
        "current_turn": 99,
        "waiting": {"kind": "stale"},
    }
    async with checkpoint_database() as db:
        assert not await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id="checkpoint-message-stale-worker",
            values={"execution_state": {"frozen_context_message_ids": [999999]}},
            checkpoint=stale_checkpoint,
        )

    async with checkpoint_database() as db:
        stale_work = await session_reply_work_item_crud.get(db, work_id)
        assert stale_work is not None
        assert stale_work.execution_state == metadata_state
        assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == metadata_checkpoint
        assert await session_reply_work_item_crud.get_frozen_context(db, stale_work) == initial_restored_frozen
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        assert [(row.id, row.payload) for row in body_result.scalars().all()] == initial_body_snapshot

    async with checkpoint_database() as db:
        baseline_work = await session_reply_work_item_crud.get(db, work_id)
        assert baseline_work is not None
        baseline_state = deepcopy(baseline_work.execution_state)
        baseline_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work_id)
        baseline_frozen = await session_reply_work_item_crud.get_frozen_context(db, baseline_work)
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        baseline_body_snapshot = [(row.id, deepcopy(row.payload)) for row in body_result.scalars().all()]
        reference_result = await db.execute(select(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.work_id == work_id).order_by(SessionReplyCheckpointMessageReference.kind, SessionReplyCheckpointMessageReference.message_id))
        baseline_reference_snapshot = [(row.kind, row.message_id) for row in reference_result.scalars().all()]

    rollback_changed_message = {
        **initial_message,
        "content": "transaction-only changed version",
    }
    rollback_new_message = {
        "id": 7199,
        "role": "assistant",
        "content": "transaction-only new message",
        "tool_call_id": "call-rollback",
        "created_at": 1793775619.0,
    }
    rollback_checkpoint = {
        "messages": [rollback_changed_message, large_tool_result, rollback_new_message, rollback_changed_message],
        "turn_messages": [rollback_new_message, rollback_changed_message, large_tool_result, rollback_new_message],
        "current_turn": 3,
        "waiting": {"kind": "rollback"},
    }
    rollback_frozen_context = [rollback_changed_message, rollback_new_message, large_tool_result]
    async with checkpoint_database() as db:
        transient_frozen_ids = await session_reply_work_item_crud.save_frozen_context(db, work_id, rollback_frozen_context)
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"execution_state": {"frozen_context_message_ids": transient_frozen_ids}},
            checkpoint=rollback_checkpoint,
            commit=False,
        )
        transient_work = await session_reply_work_item_crud.get(db, work_id)
        assert transient_work is not None
        assert transient_work.execution_state == {"frozen_context_message_ids": transient_frozen_ids}
        assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == rollback_checkpoint
        assert await session_reply_work_item_crud.get_frozen_context(db, transient_work) == rollback_frozen_context
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        transient_body_rows = list(body_result.scalars().all())
        assert len(transient_body_rows) == len(baseline_body_snapshot) + 2
        reference_result = await db.execute(select(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.work_id == work_id).order_by(SessionReplyCheckpointMessageReference.kind, SessionReplyCheckpointMessageReference.message_id))
        transient_reference_snapshot = [(row.kind, row.message_id) for row in reference_result.scalars().all()]
        assert transient_reference_snapshot != baseline_reference_snapshot
        await db.rollback()

    async with checkpoint_database() as db:
        rolled_back_work = await session_reply_work_item_crud.get(db, work_id)
        assert rolled_back_work is not None
        assert rolled_back_work.execution_state == baseline_state
        assert await session_reply_work_item_crud.get_checkpoint(db, work_id) == baseline_checkpoint
        assert await session_reply_work_item_crud.get_frozen_context(db, rolled_back_work) == baseline_frozen
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        assert [(row.id, row.payload) for row in body_result.scalars().all()] == baseline_body_snapshot
        reference_result = await db.execute(select(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.work_id == work_id).order_by(SessionReplyCheckpointMessageReference.kind, SessionReplyCheckpointMessageReference.message_id))
        assert [(row.kind, row.message_id) for row in reference_result.scalars().all()] == baseline_reference_snapshot


@pytest.mark.asyncio
async def test_cleanup_keeps_shared_checkpoint_messages_until_all_work_references_are_gone(checkpoint_database) -> None:
    shared_message = InternalMessage(
        role="user",
        content="shared checkpoint message",
        created_at=1793775620.0,
    ).model_dump()
    exclusive_old_message = InternalMessage(
        role="assistant",
        content="exclusive old checkpoint message",
        created_at=1793775621.0,
    ).model_dump()
    frozen_only_message = InternalMessage(
        role="tool",
        content="frozen-only checkpoint message",
        tool_call_id="frozen-only-call",
        created_at=1793775622.0,
    ).model_dump()
    old_time = get_local_time() - timedelta(hours=25)
    checkpoint_a = {
        "messages": [deepcopy(shared_message), deepcopy(exclusive_old_message)],
        "turn_messages": [deepcopy(shared_message), deepcopy(exclusive_old_message)],
        "current_turn": 1,
        "waiting": {"kind": "none"},
    }
    checkpoint_b = {
        "messages": [deepcopy(shared_message)],
        "turn_messages": [deepcopy(shared_message)],
        "current_turn": 2,
        "waiting": {"kind": "external"},
    }
    frozen_context_b = [deepcopy(shared_message), deepcopy(frozen_only_message)]

    work_a_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-cleanup-shared-a",
        status=SessionReplyWorkStatus.SUCCEEDED,
        updated_at=old_time,
        checkpoint=checkpoint_a,
    )
    work_b_id = await _enqueue_work(
        checkpoint_database,
        dedupe_key="checkpoint-cleanup-shared-b",
        status=SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
        updated_at=old_time,
    )

    async with checkpoint_database() as db:
        work_b = await session_reply_work_item_crud.get(db, work_b_id)
        assert work_b is not None
        work_b.execution_state = {"frozen_context_message_ids": []}
        db.add(work_b)
        await db.flush()
        frozen_ids_b = await session_reply_work_item_crud.save_frozen_context(db, work_b_id, frozen_context_b)
        work_b.execution_state = {"frozen_context_message_ids": frozen_ids_b}
        db.add(work_b)
        await session_reply_work_item_crud.save_checkpoint(db, work_b_id, checkpoint_b)
        await db.commit()

    async with checkpoint_database() as db:
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        body_rows = list(body_result.scalars().all())
        assert len(body_rows) == 3
        assert sum(row.payload == shared_message for row in body_rows) == 1
        assert sum(row.payload == exclusive_old_message for row in body_rows) == 1
        assert sum(row.payload == frozen_only_message for row in body_rows) == 1
        assert await session_reply_work_item_crud.get_checkpoint(db, work_a_id) == checkpoint_a
        work_b = await session_reply_work_item_crud.get(db, work_b_id)
        assert work_b is not None
        assert await session_reply_work_item_crud.get_checkpoint(db, work_b_id) == checkpoint_b
        assert await session_reply_work_item_crud.get_frozen_context(db, work_b) == frozen_context_b

    first_batch_size = 2
    async with checkpoint_database() as db:
        first_result = await session_reply_work_item_crud.cleanup_terminal_items(db, batch_size=first_batch_size)
        assert first_result.total <= first_batch_size
        assert first_result.work_items == 1
        assert first_result.checkpoint_messages == 1
        assert first_result.total == first_result.work_items + first_result.checkpoint_messages

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.get(db, work_a_id) is None
        assert await session_reply_work_item_crud.get_checkpoint(db, work_a_id) is None
        work_b = await session_reply_work_item_crud.get(db, work_b_id)
        assert work_b is not None
        assert await session_reply_work_item_crud.get_checkpoint(db, work_b_id) == checkpoint_b
        assert await session_reply_work_item_crud.get_frozen_context(db, work_b) == frozen_context_b
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        body_rows = list(body_result.scalars().all())
        assert len(body_rows) == 2
        assert sum(row.payload == shared_message for row in body_rows) == 1
        assert sum(row.payload == frozen_only_message for row in body_rows) == 1

    async with checkpoint_database() as db:
        await session_reply_work_item_crud.delete_checkpoint(db, work_b_id)
        await db.commit()

    second_batch_size = 2
    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.get_checkpoint(db, work_b_id) is None
        work_b = await session_reply_work_item_crud.get(db, work_b_id)
        assert work_b is not None
        assert await session_reply_work_item_crud.get_frozen_context(db, work_b) == frozen_context_b
        second_result = await session_reply_work_item_crud.cleanup_terminal_items(db, batch_size=second_batch_size)
        assert second_result.total <= second_batch_size
        assert second_result.checkpoint_messages == 0
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID).order_by(SessionReplyCheckpointMessage.id))
        body_rows = list(body_result.scalars().all())
        assert len(body_rows) == 2
        assert sum(row.payload == shared_message for row in body_rows) == 1
        assert sum(row.payload == frozen_only_message for row in body_rows) == 1
        surviving_work_b = await session_reply_work_item_crud.get(db, work_b_id)
        assert surviving_work_b is not None
        assert await session_reply_work_item_crud.get_frozen_context(db, surviving_work_b) == frozen_context_b

    async with checkpoint_database() as db:
        work_b = await session_reply_work_item_crud.get(db, work_b_id)
        assert work_b is not None
        work_b.status = SessionReplyWorkStatus.CANCELLED
        work_b.updated_at = old_time
        db.add(work_b)
        await db.commit()

    final_batch_size = 3
    async with checkpoint_database() as db:
        final_result = await session_reply_work_item_crud.cleanup_terminal_items(db, batch_size=final_batch_size)
        assert final_result.total <= final_batch_size
        assert final_result.work_items == 1
        assert final_result.checkpoint_messages == 2
        assert final_result.total == final_result.work_items + final_result.checkpoint_messages

    async with checkpoint_database() as db:
        assert await session_reply_work_item_crud.get(db, work_b_id) is None
        assert await session_reply_work_item_crud.get_checkpoint(db, work_b_id) is None
        body_result = await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.session_id == SESSION_ID))
        assert list(body_result.scalars().all()) == []
        reference_result = await db.execute(select(SessionReplyCheckpointMessageReference))
        assert list(reference_result.scalars().all()) == []
