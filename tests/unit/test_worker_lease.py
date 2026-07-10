import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, update

from app.core.crud.worker_lease import worker_lease_crud
from app.core.utils.time import get_local_time
from app.models.worker_lease import WorkerLease
from app.providers.database import AsyncSessionLocal, engine
from app.workers import lease as lease_runner


@pytest.fixture(autouse=True)
async def clean_worker_lease_table():
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: WorkerLease.__table__.create(
                sync_connection,
                checkfirst=True,
            )
        )
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
        await db.execute(update(WorkerLease).where(WorkerLease.worker_name == "message_platform").values(lease_until=get_local_time() - timedelta(seconds=1)))
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
