import asyncio
import uuid

from app.core.constants import ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN
from app.core.crud.profile import profile_crud
from app.core.crud.session_reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.crud.system_setting import system_setting_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.log import get_logger
from app.core.session_reply_queue.executor import (
    execute_session_reply_work,
    fail_session_reply_work,
    mark_work_audit_execution_unknown,
    retry_delay_seconds,
    work_has_active_audit_execution,
)
from app.core.utils.dispatcher.helpers import format_exception_message
from app.models.profile import ProfileConfig
from app.models.session_reply_work_item import SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

SESSION_REPLY_POLL_INTERVAL_SECONDS = 0.2
SESSION_REPLY_LEASE_SECONDS = 300
SESSION_REPLY_LEASE_RENEW_INTERVAL_SECONDS = 100
SESSION_REPLY_RECOVERY_INTERVAL_SECONDS = 30
SESSION_REPLY_CLEANUP_INTERVAL_SECONDS = 3600
SESSION_REPLY_CLEANUP_BATCH_SIZE = 500
SESSION_REPLY_CLEANUP_MAX_ITEMS_PER_RUN = 5000


class SessionReplyConsumer:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running: dict[int, tuple[asyncio.Task, str]] = {}
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0
        self._next_cleanup_at = 0.0

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._next_cleanup_at = 0.0
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
        tasks = [task for task, _worker_id in self._running.values() if not task.done()]
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
                claims = {work_id: worker_id for work_id, (_task, worker_id) in self._running.items()}
                if claims:
                    async with AsyncSessionLocal() as db:
                        if now - self._last_lease_renewal_at >= SESSION_REPLY_LEASE_RENEW_INTERVAL_SECONDS:
                            active_claims = await session_reply_work_item_crud.renew_active_claims(
                                db,
                                claims=claims,
                                lease_seconds=SESSION_REPLY_LEASE_SECONDS,
                            )
                            self._last_lease_renewal_at = now
                        else:
                            active_claims = await session_reply_work_item_crud.get_active_claims(db, claims)
                    for work_id, worker_id in claims.items():
                        if (work_id, worker_id) not in active_claims:
                            task_entry = self._running.get(work_id)
                            if task_entry is not None and not task_entry[0].done():
                                task_entry[0].cancel()

                if now - self._last_recovery_at >= SESSION_REPLY_RECOVERY_INTERVAL_SECONDS:
                    await self._recover_expired()
                    self._last_recovery_at = now

                await self._cleanup_terminal_items_if_due(now)

                async with AsyncSessionLocal() as db:
                    settings = await system_setting_crud.get_runtime_settings(db)
                    scheduled_profile_ids = await session_reply_work_item_crud.list_ready_scheduled_profile_ids(db)
                    profiles = await profile_crud.get_by_ids(db, scheduled_profile_ids)
                scheduled_profile_limits = {profile.id: ProfileConfig.model_validate(profile.configs).tool.scheduled_task_max_concurrency for profile in profiles if profile.id is not None}
                available_slots = max(0, settings.session_reply_max_concurrency - len(self._running))
                claimed_count = 0
                for _ in range(available_slots):
                    worker_id = uuid.uuid4().hex
                    async with AsyncSessionLocal() as db:
                        work = await session_reply_work_item_crud.claim_next(
                            db,
                            worker_id=worker_id,
                            lease_seconds=SESSION_REPLY_LEASE_SECONDS,
                            scheduled_profile_limits=scheduled_profile_limits,
                        )
                    if work is None or work.id is None:
                        break
                    language = str((work.execution_state or {}).get("language") or "zh")
                    task = asyncio.create_task(self._run_claimed(work.id, worker_id, work.attempt_count, work.max_attempts, language))
                    self._running[work.id] = (task, worker_id)
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

    async def _run_claimed(self, work_id: int, worker_id: str, attempt_count: int, max_attempts: int, language: str = "zh") -> None:
        """执行已领取回复工作并根据活动审计状态决定失败处理方式。"""
        locale_token = set_current_locale(language)
        try:
            await execute_session_reply_work(work_id, worker_id)
        except asyncio.CancelledError:
            await mark_work_audit_execution_unknown(
                work_id,
                worker_id,
                t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN),
            )
            raise
        except Exception as exc:
            error = format_exception_message(exc)
            logger.bind(work_id=work_id, worker_id=worker_id).error("Session reply work failed", exc_info=True)
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.get(db, work_id)
                stream_started = await session_reply_stream_event_crud.has_events(
                    db,
                    work_id=work_id,
                )
            has_active_audit_execution = work is not None and (work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION or work_has_active_audit_execution(work))
            if has_active_audit_execution:
                await mark_work_audit_execution_unknown(work_id, worker_id, error)
                await fail_session_reply_work(work_id, worker_id, error)
            elif isinstance(exc, BaseBusinessException):
                await fail_session_reply_work(
                    work_id,
                    worker_id,
                    error,
                    user_error=exc.render_message(),
                )
            elif attempt_count >= max_attempts or stream_started:
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
            reset_current_locale(locale_token)

    async def _recover_expired(self) -> None:
        """恢复过期租约并终止带活动审计绑定的回复工作。"""
        async with AsyncSessionLocal() as db:
            _recovered_count, terminal_claims = await session_reply_work_item_crud.recover_expired(db)
        for work_id, worker_id, error in terminal_claims:
            await mark_work_audit_execution_unknown(work_id, worker_id, error)
            await fail_session_reply_work(work_id, worker_id, error)

    async def _cleanup_terminal_items_if_due(self, now: float) -> None:
        if now < self._next_cleanup_at:
            return
        await self._cleanup_terminal_items()
        self._next_cleanup_at = now + SESSION_REPLY_CLEANUP_INTERVAL_SECONDS

    async def _cleanup_terminal_items(self) -> None:
        work_item_count = 0
        stream_event_count = 0
        sequence_count = 0
        remaining = SESSION_REPLY_CLEANUP_MAX_ITEMS_PER_RUN

        while remaining > 0:
            batch_size = min(SESSION_REPLY_CLEANUP_BATCH_SIZE, remaining)
            async with AsyncSessionLocal() as db:
                cleanup_result = await session_reply_work_item_crud.cleanup_terminal_items(
                    db,
                    batch_size=batch_size,
                )
            work_item_count += cleanup_result.work_items
            stream_event_count += cleanup_result.stream_events
            sequence_count += cleanup_result.sequences
            remaining -= cleanup_result.total
            if cleanup_result.total < batch_size or remaining <= 0:
                break
            await asyncio.sleep(0)

        if work_item_count + stream_event_count + sequence_count > 0:
            logger.bind(
                work_item_count=work_item_count,
                stream_event_count=stream_event_count,
                sequence_count=sequence_count,
            ).info("Expired session reply data cleaned")


session_reply_consumer = SessionReplyConsumer()
