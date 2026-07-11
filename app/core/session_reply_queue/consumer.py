import asyncio
import uuid

from app.core.crud.session_reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.crud.system_setting import system_setting_crud
from app.core.log import get_logger
from app.core.session_reply_queue.executor import execute_session_reply_work, fail_session_reply_work, retry_delay_seconds
from app.core.utils.dispatcher.helpers import format_exception_message
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

SESSION_REPLY_POLL_INTERVAL_SECONDS = 0.2
SESSION_REPLY_LEASE_SECONDS = 300
SESSION_REPLY_LEASE_RENEW_INTERVAL_SECONDS = 100
SESSION_REPLY_RECOVERY_INTERVAL_SECONDS = 30


class SessionReplyConsumer:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}
        self._last_recovery_at = 0.0

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
        tasks = [task for task in self._running.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            try:
                now = loop.time()
                if now - self._last_recovery_at >= SESSION_REPLY_RECOVERY_INTERVAL_SECONDS:
                    async with AsyncSessionLocal() as db:
                        await session_reply_work_item_crud.recover_expired(db)
                    self._last_recovery_at = now

                async with AsyncSessionLocal() as db:
                    settings = await system_setting_crud.get_runtime_settings(db)
                available_slots = max(0, settings.session_reply_max_concurrency - len(self._running))
                claimed_count = 0
                for _ in range(available_slots):
                    worker_id = uuid.uuid4().hex
                    async with AsyncSessionLocal() as db:
                        work = await session_reply_work_item_crud.claim_next(
                            db,
                            worker_id=worker_id,
                            lease_seconds=SESSION_REPLY_LEASE_SECONDS,
                        )
                    if work is None or work.id is None:
                        break
                    task = asyncio.create_task(self._run_claimed(work.id, worker_id, work.attempt_count, work.max_attempts))
                    self._running[work.id] = task
                    task.add_done_callback(lambda _task, work_id=work.id: self._running.pop(work_id, None))
                    claimed_count += 1

                if claimed_count == 0:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=SESSION_REPLY_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Session reply consumer loop failed")
                await asyncio.sleep(SESSION_REPLY_POLL_INTERVAL_SECONDS)

    async def _run_claimed(self, work_id: int, worker_id: str, attempt_count: int, max_attempts: int) -> None:
        execution_task = asyncio.create_task(execute_session_reply_work(work_id, worker_id))
        renewal_task = asyncio.create_task(self._renew_lease(work_id, worker_id))
        try:
            done, _pending = await asyncio.wait(
                {execution_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done and not renewal_task.result():
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
                return
            await execution_task
        except asyncio.CancelledError:
            execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)
            raise
        except Exception as exc:
            error = format_exception_message(exc)
            logger.bind(work_id=work_id, worker_id=worker_id).error("Session reply work failed", exc_info=True)
            async with AsyncSessionLocal() as db:
                stream_started = await session_reply_stream_event_crud.has_events(
                    db,
                    work_id=work_id,
                )
            if attempt_count >= max_attempts or stream_started:
                await fail_session_reply_work(work_id, worker_id, error)
            else:
                async with AsyncSessionLocal() as db:
                    await session_reply_work_item_crud.release_for_retry(
                        db,
                        work_id=work_id,
                        worker_id=worker_id,
                        error=error,
                        delay_seconds=retry_delay_seconds(attempt_count),
                    )
        finally:
            renewal_task.cancel()
            await asyncio.gather(renewal_task, return_exceptions=True)

    async def _renew_lease(self, work_id: int, worker_id: str) -> bool:
        while True:
            await asyncio.sleep(SESSION_REPLY_LEASE_RENEW_INTERVAL_SECONDS)
            async with AsyncSessionLocal() as db:
                renewed = await session_reply_work_item_crud.renew_lease(
                    db,
                    work_id=work_id,
                    worker_id=worker_id,
                    lease_seconds=SESSION_REPLY_LEASE_SECONDS,
                )
            if not renewed:
                return False


session_reply_consumer = SessionReplyConsumer()
