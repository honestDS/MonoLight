from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
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
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
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
