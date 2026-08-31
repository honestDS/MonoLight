import asyncio

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import OperationalError

from app.core.crud.worker.lease import worker_lease_crud
from app.models.worker_lease import WorkerLease
from app.providers.database import AsyncSessionLocal, engine
from app.providers.database.time import get_database_timestamp
from app.workers import lease as lease_runner


@pytest.fixture(autouse=True)
async def clean_worker_lease_table():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: WorkerLease.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: WorkerLease.__table__.create(sync_connection))
    async with AsyncSessionLocal() as db:
        await db.execute(delete(WorkerLease))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(WorkerLease))
        await db.commit()


@pytest.mark.asyncio
async def test_worker_lease_is_exclusive_and_owner_scoped():
    async with AsyncSessionLocal() as db:
        first_acquired = await worker_lease_crud.acquire(
            db,
            worker_name="background_task",
            owner_id="worker-a",
            lease_seconds=30,
        )

    async with AsyncSessionLocal() as db:
        second_acquired = await worker_lease_crud.acquire(
            db,
            worker_name="background_task",
            owner_id="worker-b",
            lease_seconds=30,
        )
        wrong_owner_renewed = await worker_lease_crud.renew(
            db,
            worker_name="background_task",
            owner_id="worker-b",
            lease_seconds=30,
        )
        wrong_owner_released = await worker_lease_crud.release(
            db,
            worker_name="background_task",
            owner_id="worker-b",
        )

    async with AsyncSessionLocal() as db:
        owner_renewed = await worker_lease_crud.renew(
            db,
            worker_name="background_task",
            owner_id="worker-a",
            lease_seconds=30,
        )
        owner_released = await worker_lease_crud.release(
            db,
            worker_name="background_task",
            owner_id="worker-a",
        )

    async with AsyncSessionLocal() as db:
        acquired_after_release = await worker_lease_crud.acquire(
            db,
            worker_name="background_task",
            owner_id="worker-b",
            lease_seconds=30,
        )

    assert first_acquired is True
    assert second_acquired is False
    assert wrong_owner_renewed is False
    assert wrong_owner_released is False
    assert owner_renewed is True
    assert owner_released is True
    assert acquired_after_release is True


@pytest.mark.asyncio
async def test_expired_worker_lease_can_be_taken_over():
    async with AsyncSessionLocal() as db:
        assert await worker_lease_crud.acquire(
            db,
            worker_name="message_platform",
            owner_id="worker-a",
            lease_seconds=30,
        )
        database_now = await get_database_timestamp(db)
        await db.execute(update(WorkerLease).where(WorkerLease.worker_name == "message_platform").values(lease_until=database_now - 1))
        await db.commit()

    async with AsyncSessionLocal() as db:
        acquired = await worker_lease_crud.acquire(
            db,
            worker_name="message_platform",
            owner_id="worker-b",
            lease_seconds=30,
        )
        stale_owner_renewed = await worker_lease_crud.renew(
            db,
            worker_name="message_platform",
            owner_id="worker-a",
            lease_seconds=30,
        )
        lease = await worker_lease_crud.get_by_name(db, "message_platform")

    assert acquired is True
    assert stale_owner_renewed is False
    assert lease is not None
    assert lease.owner_id == "worker-b"
    assert isinstance(lease.lease_until, int)
    assert isinstance(lease.updated_at, int)


@pytest.mark.asyncio
async def test_worker_lease_runner_waits_then_starts_after_acquiring(monkeypatch):
    acquire_attempts = 0
    worker_started = False
    shutdown_event = asyncio.Event()

    async def acquire(worker_name, owner_id):
        nonlocal acquire_attempts
        acquire_attempts += 1
        return acquire_attempts == 2

    async def release(worker_name, owner_id):
        return None

    async def run_worker(owned_stop_event):
        nonlocal worker_started
        worker_started = True
        shutdown_event.set()

    monkeypatch.setattr(lease_runner, "WORKER_LEASE_ACQUIRE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(lease_runner, "_acquire_worker_lease", acquire)
    monkeypatch.setattr(lease_runner, "_release_worker_lease", release)

    await lease_runner.run_with_worker_lease(
        "background_task",
        shutdown_event,
        run_worker,
    )

    assert acquire_attempts == 2
    assert worker_started is True


@pytest.mark.asyncio
async def test_worker_lease_renewal_retries_transient_sqlite_lock(monkeypatch):
    renew_attempts = 0
    shutdown_event = asyncio.Event()
    owned_stop_event = asyncio.Event()

    async def renew(worker_name, owner_id):
        nonlocal renew_attempts
        renew_attempts += 1
        if renew_attempts == 1:
            raise OperationalError(
                "UPDATE worker_lease SET lease_until=?",
                {},
                Exception("database is locked"),
            )
        shutdown_event.set()
        return True

    async def wait_for_stop(stop_event, timeout):
        return False

    monkeypatch.setattr(lease_runner, "_renew_worker_lease", renew)
    monkeypatch.setattr(lease_runner, "_wait_for_stop", wait_for_stop)
    monkeypatch.setattr(lease_runner, "monotonic", lambda: 100.0)

    await lease_runner._maintain_worker_lease(
        "session_reply",
        "worker-a",
        shutdown_event,
        owned_stop_event,
    )

    assert renew_attempts == 2
    assert owned_stop_event.is_set() is False


@pytest.mark.asyncio
async def test_worker_lease_runner_releases_and_cancels_worker_on_cancellation(monkeypatch):
    events = []
    worker_started = asyncio.Event()

    async def acquire(worker_name, owner_id):
        events.append(("acquire", owner_id))
        return True

    async def release(worker_name, owner_id):
        events.append(("release", owner_id))

    async def run_worker(owned_stop_event):
        events.append(("worker-start",))
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append(("worker-stop", owned_stop_event.is_set()))

    monkeypatch.setattr(lease_runner, "_acquire_worker_lease", acquire)
    monkeypatch.setattr(lease_runner, "_release_worker_lease", release)

    task = asyncio.create_task(
        lease_runner.run_with_worker_lease(
            "background_task",
            asyncio.Event(),
            run_worker,
        )
    )
    await worker_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert [event[0] for event in events] == [
        "acquire",
        "worker-start",
        "worker-stop",
        "release",
    ]
    assert events[2][1] is True
    assert events[0][1] == events[3][1]


@pytest.mark.asyncio
async def test_worker_lease_runner_stops_and_releases_after_lease_loss(monkeypatch):
    events = []
    stop_event = asyncio.Event()

    async def acquire(worker_name, owner_id):
        events.append(("acquire", worker_name, owner_id))
        return True

    async def renew(worker_name, owner_id):
        events.append(("renew", worker_name, owner_id))
        return False

    async def release(worker_name, owner_id):
        events.append(("release", worker_name, owner_id))

    async def run_worker(owned_stop_event):
        events.append(("worker-start",))
        await owned_stop_event.wait()
        events.append(("worker-stop",))
        stop_event.set()

    monkeypatch.setattr(lease_runner, "WORKER_LEASE_RENEW_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(lease_runner, "_acquire_worker_lease", acquire)
    monkeypatch.setattr(lease_runner, "_renew_worker_lease", renew)
    monkeypatch.setattr(lease_runner, "_release_worker_lease", release)

    await lease_runner.run_with_worker_lease(
        "message_platform",
        stop_event,
        run_worker,
    )

    assert stop_event.is_set()
    assert [event[0] for event in events] == [
        "acquire",
        "worker-start",
        "renew",
        "worker-stop",
        "release",
    ]
    assert events[0][2] == events[2][2] == events[4][2]


@pytest.mark.asyncio
async def test_stop_owned_worker_forces_process_exit_after_cancel_timeout(monkeypatch):
    class ForcedExit(Exception):
        pass

    force_exit_calls = []
    cancellation_received = asyncio.Event()
    keep_running = True

    async def uncooperative_worker():
        nonlocal keep_running
        while keep_running:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_received.set()

    def force_exit(worker_name):
        force_exit_calls.append(worker_name)
        raise ForcedExit

    monkeypatch.setattr(lease_runner, "WORKER_SHUTDOWN_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(lease_runner, "WORKER_CANCEL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(lease_runner, "_force_worker_process_exit", force_exit)

    worker_task = asyncio.create_task(uncooperative_worker())
    try:
        with pytest.raises(ForcedExit):
            await lease_runner._stop_owned_worker(
                "background_task",
                worker_task,
            )

        assert cancellation_received.is_set()
        assert force_exit_calls == ["background_task"]
    finally:
        keep_running = False
        worker_task.cancel()
        await asyncio.wait_for(
            asyncio.gather(worker_task, return_exceptions=True),
            timeout=1,
        )


@pytest.mark.asyncio
async def test_worker_lease_loss_cancels_worker_after_shutdown_timeout(monkeypatch):
    events = []
    worker_started = asyncio.Event()
    worker_cancelled = asyncio.Event()
    acquire_attempts = 0

    async def acquire(worker_name, owner_id):
        nonlocal acquire_attempts
        acquire_attempts += 1
        return True

    async def renew(worker_name, owner_id):
        await worker_started.wait()
        return False

    async def release(worker_name, owner_id):
        events.append(("release", worker_name, owner_id))

    async def run_worker(owned_stop_event):
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append(("worker-stop", owned_stop_event.is_set()))
            worker_cancelled.set()

    monkeypatch.setattr(lease_runner, "WORKER_LEASE_RENEW_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(lease_runner, "WORKER_SHUTDOWN_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(lease_runner, "_acquire_worker_lease", acquire)
    monkeypatch.setattr(lease_runner, "_renew_worker_lease", renew)
    monkeypatch.setattr(lease_runner, "_release_worker_lease", release)

    await lease_runner.run_with_worker_lease(
        "background_task",
        asyncio.Event(),
        run_worker,
    )

    assert worker_cancelled.is_set()
    assert acquire_attempts == 1
    assert [event[0] for event in events] == ["worker-stop", "release"]
    assert events[0][1] is True
