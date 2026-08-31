import sys

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.task.background import background_task_crud
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.providers.database import AsyncSessionLocal
from app.providers.database.time import get_database_timestamp
from app.schemas.background_task import BackgroundTaskResult


@pytest.fixture(autouse=True)
async def isolated_background_task_database(tmp_path, monkeypatch):
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'background-task-lease.db'}", connect_args={"timeout": 30})
    async with test_engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(sys.modules[__name__], "AsyncSessionLocal", session_factory)
    try:
        yield
    finally:
        await test_engine.dispose()


async def create_task() -> BackgroundTask:
    async with AsyncSessionLocal() as db:
        return await background_task_crud.create_task(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            tool_call_id="call-1",
            tool_name="execute_shell",
            arguments={"command": "pwd"},
        )


@pytest.mark.asyncio
async def test_expired_running_task_is_requeued_and_can_be_claimed_again():
    task = await create_task()

    async with AsyncSessionLocal() as db:
        claimed = await background_task_crud.try_claim(db, task_id=task.id, worker_id="worker-a")
        assert claimed is not None
        database_now = await get_database_timestamp(db)
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == task.id).values(lock_until=database_now - 1))
        await db.commit()

    async with AsyncSessionLocal() as db:
        reply_task_ids = await background_task_crud.requeue_expired_running(db, profile_id=1, max_attempts_error="lease exhausted")
        recovered = await background_task_crud.get(db, task.id)

    async with AsyncSessionLocal() as db:
        reclaimed = await background_task_crud.try_claim(db, task_id=task.id, worker_id="worker-b")

    assert reply_task_ids == []
    assert recovered is not None
    assert recovered.status == BackgroundTaskStatus.PENDING
    assert recovered.locked_by is None
    assert recovered.lock_until is None
    assert reclaimed is not None
    assert reclaimed.locked_by == "worker-b"
    assert reclaimed.attempt_count == 2


@pytest.mark.asyncio
async def test_expired_task_at_max_attempts_has_structured_failure_result():
    task = await create_task()

    async with AsyncSessionLocal() as db:
        claimed = await background_task_crud.try_claim(db, task_id=task.id, worker_id="worker-a")
        assert claimed is not None
        database_now = await get_database_timestamp(db)
        await db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task.id)
            .values(
                attempt_count=3,
                lock_until=database_now - 1,
            )
        )
        await db.commit()

    async with AsyncSessionLocal() as db:
        reply_task_ids = await background_task_crud.requeue_expired_running(
            db,
            profile_id=1,
            max_attempts_error="lease exhausted",
        )
        failed = await background_task_crud.get(db, task.id)

    assert reply_task_ids == [task.id]
    assert failed is not None
    assert failed.status == BackgroundTaskStatus.FAILED
    assert failed.reply_status == BackgroundTaskReplyStatus.PENDING
    assert failed.error == "lease exhausted"
    result = BackgroundTaskResult.model_validate(failed.result)
    assert result.status == "failed"
    assert result.tool_name == "execute_shell"
    assert result.error == "lease exhausted"


@pytest.mark.asyncio
async def test_task_lease_renewal_and_completion_are_owner_scoped():
    task = await create_task()

    async with AsyncSessionLocal() as db:
        claimed = await background_task_crud.try_claim(db, task_id=task.id, worker_id="worker-a", lease_seconds=60)
        assert claimed is not None
        original_lock_until = claimed.lock_until
        wrong_owner_renewed = await background_task_crud.renew_lease(db, task_id=task.id, worker_id="worker-b", lease_seconds=300)
        owner_renewed = await background_task_crud.renew_lease(db, task_id=task.id, worker_id="worker-a", lease_seconds=300)
        wrong_owner_completed = await background_task_crud.mark_succeeded(
            db,
            task_id=task.id,
            worker_id="worker-b",
            result={"status": "succeeded"},
            auto_reply=True,
        )

    async with AsyncSessionLocal() as db:
        running = await background_task_crud.get(db, task.id)

    assert wrong_owner_renewed is False
    assert owner_renewed is True
    assert wrong_owner_completed is False
    assert running is not None
    assert running.status == BackgroundTaskStatus.RUNNING
    assert running.locked_by == "worker-a"
    assert running.lock_until > original_lock_until


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
async def test_expired_task_lease_cannot_write_terminal_status(terminal_status):
    task = await create_task()

    async with AsyncSessionLocal() as db:
        claimed = await background_task_crud.try_claim(db, task_id=task.id, worker_id="worker-a")
        assert claimed is not None
        database_now = await get_database_timestamp(db)
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == task.id).values(lock_until=database_now - 1))
        await db.commit()

        if terminal_status == "succeeded":
            marked = await background_task_crud.mark_succeeded(
                db,
                task_id=task.id,
                worker_id="worker-a",
                result={"status": "succeeded"},
                auto_reply=True,
            )
        else:
            marked = await background_task_crud.mark_failed(
                db,
                task_id=task.id,
                worker_id="worker-a",
                error="late failure",
                result={"status": "failed"},
                auto_reply=True,
            )

    async with AsyncSessionLocal() as db:
        current = await background_task_crud.get(db, task.id)

    assert marked is False
    assert current is not None
    assert current.status == BackgroundTaskStatus.RUNNING
    assert current.locked_by == "worker-a"
    assert current.result is None
    assert current.finished_at is None


@pytest.mark.asyncio
async def test_reply_claim_is_owner_scoped_and_only_expired_claim_is_recovered():
    task = await create_task()

    async with AsyncSessionLocal() as db:
        claimed = await background_task_crud.try_claim(db, task_id=task.id, worker_id="task-worker")
        assert claimed is not None
        marked = await background_task_crud.mark_succeeded(
            db,
            task_id=task.id,
            worker_id="task-worker",
            result={"status": "succeeded"},
            auto_reply=True,
        )
        assert marked is True

    async with AsyncSessionLocal() as db:
        first_reply_claim = await background_task_crud.try_claim_reply(
            db,
            task_id=task.id,
            worker_id="reply-worker-a",
            lease_seconds=300,
        )
        second_reply_claim = await background_task_crud.try_claim_reply(
            db,
            task_id=task.id,
            worker_id="reply-worker-b",
            lease_seconds=300,
        )
        active_recovered_count = await background_task_crud.recover_expired_replies(db)

    assert first_reply_claim is not None
    assert first_reply_claim.reply_status == BackgroundTaskReplyStatus.RUNNING
    assert first_reply_claim.reply_locked_by == "reply-worker-a"
    assert second_reply_claim is None
    assert active_recovered_count == 0

    async with AsyncSessionLocal() as db:
        database_now = await get_database_timestamp(db)
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == task.id).values(reply_lock_until=database_now - 1))
        await db.commit()
        expired_recovered_count = await background_task_crud.recover_expired_replies(db)
        second_reply_claim = await background_task_crud.try_claim_reply(
            db,
            task_id=task.id,
            worker_id="reply-worker-b",
            lease_seconds=300,
        )
        stale_owner_released = await background_task_crud.release_reply_claim(
            db,
            task_id=task.id,
            worker_id="reply-worker-a",
        )
        stale_owner_completed = await background_task_crud.complete_reply_claim(
            db,
            task_id=task.id,
            worker_id="reply-worker-a",
            status=BackgroundTaskReplyStatus.SUCCEEDED,
        )
        current = await background_task_crud.get(db, task.id)

    assert expired_recovered_count == 1
    assert second_reply_claim is not None
    assert second_reply_claim.reply_locked_by == "reply-worker-b"
    assert stale_owner_released is False
    assert stale_owner_completed is False
    assert current is not None
    assert current.reply_status == BackgroundTaskReplyStatus.RUNNING
    assert current.reply_locked_by == "reply-worker-b"


@pytest.mark.asyncio
async def test_release_claim_only_requeues_matching_running_owner():
    task = await create_task()

    async with AsyncSessionLocal() as db:
        claimed = await background_task_crud.try_claim(db, task_id=task.id, worker_id="worker-a")
        assert claimed is not None
        wrong_owner_released = await background_task_crud.release_claim(db, task_id=task.id, worker_id="worker-b")
        owner_released = await background_task_crud.release_claim(db, task_id=task.id, worker_id="worker-a")

    async with AsyncSessionLocal() as db:
        released = await background_task_crud.get(db, task.id)

    assert wrong_owner_released is False
    assert owner_released is True
    assert released is not None
    assert released.status == BackgroundTaskStatus.PENDING
    assert released.locked_by is None
    assert released.lock_until is None
