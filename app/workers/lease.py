import asyncio
import uuid
from collections.abc import Awaitable, Callable

from app.core.crud.worker_lease import worker_lease_crud
from app.core.log import get_logger
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

WORKER_LEASE_SECONDS = 30
WORKER_LEASE_RENEW_INTERVAL_SECONDS = 10
WORKER_LEASE_ACQUIRE_INTERVAL_SECONDS = 5


async def _acquire_worker_lease(worker_name: str, owner_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        return await worker_lease_crud.acquire(
            db,
            worker_name=worker_name,
            owner_id=owner_id,
            lease_seconds=WORKER_LEASE_SECONDS,
        )


async def _renew_worker_lease(worker_name: str, owner_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        return await worker_lease_crud.renew(
            db,
            worker_name=worker_name,
            owner_id=owner_id,
            lease_seconds=WORKER_LEASE_SECONDS,
        )


async def _release_worker_lease(worker_name: str, owner_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await worker_lease_crud.release(
            db,
            worker_name=worker_name,
            owner_id=owner_id,
        )


async def _wait_for_stop(stop_event: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except TimeoutError:
        return False


async def _maintain_worker_lease(
    worker_name: str,
    owner_id: str,
    shutdown_event: asyncio.Event,
    owned_stop_event: asyncio.Event,
) -> None:
    while not shutdown_event.is_set() and not owned_stop_event.is_set():
        if await _wait_for_stop(
            shutdown_event,
            WORKER_LEASE_RENEW_INTERVAL_SECONDS,
        ):
            owned_stop_event.set()
            return

        try:
            renewed = await _renew_worker_lease(worker_name, owner_id)
        except Exception:
            logger.exception(
                "Worker lease renewal failed",
                extra={"worker_name": worker_name},
            )
            owned_stop_event.set()
            return

        if not renewed:
            logger.error(
                "Worker lease lost",
                extra={"worker_name": worker_name},
            )
            owned_stop_event.set()
            return


async def _run_owned_worker(
    worker_name: str,
    owner_id: str,
    shutdown_event: asyncio.Event,
    run_worker: Callable[[asyncio.Event], Awaitable[None]],
) -> bool:
    owned_stop_event = asyncio.Event()
    worker_task = asyncio.create_task(run_worker(owned_stop_event))
    renewal_task = asyncio.create_task(
        _maintain_worker_lease(
            worker_name,
            owner_id,
            shutdown_event,
            owned_stop_event,
        )
    )
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    try:
        done, _pending = await asyncio.wait(
            {worker_task, renewal_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        worker_finished_independently = worker_task in done and renewal_task not in done and shutdown_task not in done
        owned_stop_event.set()
        await worker_task
        return worker_finished_independently
    finally:
        owned_stop_event.set()
        renewal_task.cancel()
        shutdown_task.cancel()
        if not worker_task.done():
            worker_task.cancel()
        await asyncio.gather(
            worker_task,
            renewal_task,
            shutdown_task,
            return_exceptions=True,
        )


async def run_with_worker_lease(
    worker_name: str,
    shutdown_event: asyncio.Event,
    run_worker: Callable[[asyncio.Event], Awaitable[None]],
) -> None:
    while not shutdown_event.is_set():
        owner_id = uuid.uuid4().hex
        try:
            acquired = await _acquire_worker_lease(worker_name, owner_id)
        except Exception:
            logger.exception(
                "Worker lease acquisition failed",
                extra={"worker_name": worker_name},
            )
            acquired = False

        if not acquired:
            await _wait_for_stop(
                shutdown_event,
                WORKER_LEASE_ACQUIRE_INTERVAL_SECONDS,
            )
            continue

        worker_finished_independently = False
        try:
            worker_finished_independently = await _run_owned_worker(
                worker_name,
                owner_id,
                shutdown_event,
                run_worker,
            )
        finally:
            try:
                await asyncio.shield(_release_worker_lease(worker_name, owner_id))
            except Exception:
                logger.exception(
                    "Worker lease release failed",
                    extra={"worker_name": worker_name},
                )

        if worker_finished_independently:
            return
