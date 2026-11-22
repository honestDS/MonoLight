import asyncio
import time
import uuid
from contextlib import ExitStack

from app.core.constants import (
    ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED,
    ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN,
    ERR_SUBAGENT_CONTEXT_INVALID,
    NOTIFICATION_ERROR_RETRY_SECONDS,
    SESSION_REPLY_CLAIM_BATCH_SIZE,
    SESSION_REPLY_CLEANUP_BATCH_SIZE,
    SESSION_REPLY_CLEANUP_INTERVAL_SECONDS,
    SESSION_REPLY_CLEANUP_MAX_ITEMS_PER_RUN,
    SESSION_REPLY_LEASE_RENEW_INTERVAL_SECONDS,
    SESSION_REPLY_LEASE_SECONDS,
    SESSION_REPLY_RECOVERY_INTERVAL_SECONDS,
    SUBAGENT_CLEANUP_BATCH_SIZE,
    SUBAGENT_CLEANUP_MAX_ITEMS_PER_RUN,
    SUBAGENT_NOTIFICATION_CLEANUP_BATCH_SIZE,
    SUBAGENT_NOTIFICATION_CLEANUP_MAX_SCAN_PER_RUN,
)
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.crud.system.setting import system_setting_crud
from app.core.exceptions import BaseBusinessException, LLMContextLengthException, LLMRequestRejectedException, ParameterException
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.log import agent_log_context, get_logger
from app.core.notifications.inbox import NotificationBatch, NotificationInbox
from app.core.notifications.protocol import Notification, NotificationTopic
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.executor import (
    execute_session_reply_work,
    fail_session_reply_work,
    mark_work_audit_execution_unknown,
    retry_delay_seconds,
    work_has_active_audit_execution,
)
from app.core.utils.dispatcher.helpers import format_exception_message
from app.models.profile import ProfileConfig
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


def _resource_id(notification: Notification, resource_type: str) -> int | None:
    resource = notification.resource
    if resource is None or resource.type != resource_type:
        return None
    try:
        resource_id = int(resource.id)
    except ValueError:
        return None
    if resource_id <= 0:
        return None
    return resource_id


class SessionReplyConsumer:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running: dict[int, tuple[asyncio.Task, str]] = {}
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0
        self._next_cleanup_at = 0.0
        self._notification_cleanup_after_id = 0
        self._inbox = NotificationInbox()

    def start(self, *, inbox: NotificationInbox | None = None) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._inbox = inbox if inbox is not None else NotificationInbox()
        self._inbox.wake()
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0
        self._next_cleanup_at = 0.0
        self._notification_cleanup_after_id = 0
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

    def _discard_completed(self, work_id: int, finished_task: asyncio.Task) -> None:
        task_entry = self._running.get(work_id)
        if task_entry is not None and task_entry[0] is finished_task:
            self._running.pop(work_id, None)
            self._inbox.wake()

    async def _dispatch_pending(
        self,
        *,
        session_ids: set[str] | None = None,
        profile_ids: set[int] | None = None,
    ) -> int:
        if session_ids == set() and profile_ids == set():
            return 0

        async with AsyncSessionLocal() as db:
            settings = await system_setting_crud.get_runtime_settings(db)
            agent_profile_ids = await session_reply_work_item_crud.list_ready_agent_profile_ids(
                db,
                session_ids=session_ids,
                profile_ids=profile_ids,
            )
            profiles = await profile_crud.get_by_ids(db, agent_profile_ids)
        scheduled_profile_limits: dict[int, int] = {}
        subagent_profile_limits: dict[int, int] = {}
        for profile in profiles:
            if profile.id is None:
                continue
            profile_config = ProfileConfig.model_validate(profile.configs)
            scheduled_profile_limits[profile.id] = profile_config.tool.scheduled_task_max_concurrency
            subagent_profile_limits[profile.id] = profile_config.tool.subagent_max_concurrency

        claimed_count = 0
        for independent_only, batch_size in (
            (False, SESSION_REPLY_CLAIM_BATCH_SIZE),
            (True, min(SESSION_REPLY_CLAIM_BATCH_SIZE, settings.agent_work_max_concurrency)),
        ):
            for _ in range(batch_size):
                if self._stop_event.is_set():
                    break
                worker_id = uuid.uuid4().hex
                async with AsyncSessionLocal() as db:
                    work = await session_reply_work_item_crud.claim_next(
                        db,
                        worker_id=worker_id,
                        lease_seconds=SESSION_REPLY_LEASE_SECONDS,
                        scheduled_profile_limits=scheduled_profile_limits,
                        independent_only=independent_only,
                        agent_work_max_concurrency=settings.agent_work_max_concurrency,
                        subagent_profile_limits=subagent_profile_limits,
                        exclude_work_ids=set(self._running),
                        session_ids=session_ids,
                        profile_ids=profile_ids,
                    )
                if work is None or work.id is None:
                    break
                language = str((work.execution_state or {}).get("language") or "zh")
                task = asyncio.create_task(self._run_claimed(work.id, worker_id, work.attempt_count, work.max_attempts, language))
                self._running[work.id] = (task, worker_id)
                task.add_done_callback(lambda finished_task, work_id=work.id: self._discard_completed(work_id, finished_task))
                claimed_count += 1
        return claimed_count

    async def _check_running_claims(self, claims: dict[int, str], *, now: float, renew: bool) -> None:
        if not claims:
            return

        claims_to_cancel: list[tuple[int, str]] = []
        async with AsyncSessionLocal() as db:
            if renew:
                active_claims = await session_reply_work_item_crud.renew_active_claims(
                    db,
                    claims=claims,
                    lease_seconds=SESSION_REPLY_LEASE_SECONDS,
                )
                self._last_lease_renewal_at = now
            else:
                active_claims = await session_reply_work_item_crud.get_active_claims(db, claims)
            for work_id, worker_id in claims.items():
                if (work_id, worker_id) in active_claims:
                    continue
                work = await session_reply_work_item_crud.get(db, work_id)
                if (
                    work is not None
                    and work.locked_by is None
                    and work.status
                    in {
                        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                        SessionReplyWorkStatus.READY_FOR_LLM,
                        SessionReplyWorkStatus.SUCCEEDED,
                    }
                ):
                    continue
                claims_to_cancel.append((work_id, worker_id))
        for work_id, _worker_id in claims_to_cancel:
            task_entry = self._running.get(work_id)
            if task_entry is not None and not task_entry[0].done():
                task_entry[0].cancel()

    async def _dispatch_scope(self, batch: NotificationBatch) -> tuple[set[str] | None, set[int] | None]:
        if batch.full_scan:
            return None, None
        if not batch.notifications:
            return set(), set()

        session_ids: set[str] = set()
        profile_ids: set[int] = set()
        work_ids: set[int] = set()
        for notification in batch.notifications:
            if notification.topic == NotificationTopic.SYSTEM_SETTINGS:
                return None, None
            if notification.topic == NotificationTopic.SESSION_REPLY and notification.event == "capacity":
                return None, None
            session_id = notification.scope.session_id
            if notification.topic == NotificationTopic.SESSION_REPLY and session_id:
                session_ids.add(session_id)
                continue
            work_id = _resource_id(notification, "session_reply_work")
            if notification.topic == NotificationTopic.SESSION_REPLY and work_id is not None:
                work_ids.add(work_id)
                continue
            profile_id = notification.scope.profile_id
            if notification.topic in {NotificationTopic.SESSION_REPLY, NotificationTopic.PROFILE_CONFIG} and profile_id is not None:
                profile_ids.add(profile_id)
                continue
            profile_id = _resource_id(notification, "profile")
            if notification.topic == NotificationTopic.PROFILE_CONFIG and profile_id is not None:
                profile_ids.add(profile_id)
                continue
            return None, None

        if work_ids:
            async with AsyncSessionLocal() as db:
                works = await session_reply_work_item_crud.get_by_ids(db, work_ids)
            for work in works:
                session_ids.add(work.session_id)
        return session_ids, profile_ids

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_available_at = float("inf")
        while not self._stop_event.is_set():
            try:
                now = loop.time()
                batch = self._inbox.take()
                recovery_due = now - self._last_recovery_at >= SESSION_REPLY_RECOVERY_INTERVAL_SECONDS
                full_scan = batch.full_scan or recovery_due or now >= next_available_at
                if full_scan:
                    session_ids, profile_ids = None, None
                else:
                    session_ids, profile_ids = await self._dispatch_scope(batch)
                    full_scan = session_ids is None and profile_ids is None

                claims = {work_id: worker_id for work_id, (_task, worker_id) in self._running.items()}
                renew_due = bool(claims) and now - self._last_lease_renewal_at >= SESSION_REPLY_LEASE_RENEW_INTERVAL_SECONDS
                resource_work_ids: set[int] = set()
                for notification in batch.notifications:
                    if notification.topic != NotificationTopic.SESSION_REPLY:
                        continue
                    work_id = _resource_id(notification, "session_reply_work")
                    if work_id is not None:
                        resource_work_ids.add(work_id)
                scoped_claims: dict[int, str] = {}
                for work_id, worker_id in claims.items():
                    if work_id in resource_work_ids:
                        scoped_claims[work_id] = worker_id
                if renew_due or full_scan:
                    await self._check_running_claims(claims, now=now, renew=renew_due)
                elif claims and resource_work_ids:
                    await self._check_running_claims(
                        scoped_claims,
                        now=now,
                        renew=False,
                    )

                if recovery_due:
                    await self._recover_expired()
                    self._last_recovery_at = now

                await self._cleanup_terminal_items_if_due(now)

                dispatch_triggered = bool(batch.notifications) or full_scan
                claimed_count = 0
                available_deadline = float("inf")
                next_available = None
                if dispatch_triggered:
                    claimed_count = await self._dispatch_pending(session_ids=session_ids, profile_ids=profile_ids)
                    async with AsyncSessionLocal() as db:
                        next_available = await session_reply_work_item_crud.get_next_available_at(
                            db,
                            session_ids=session_ids,
                            profile_ids=profile_ids,
                        )
                if next_available is not None:
                    available_deadline = loop.time() + max(0.0, next_available - time.time())
                if dispatch_triggered and full_scan:
                    next_available_at = available_deadline
                if dispatch_triggered and not full_scan:
                    next_available_at = min(next_available_at, available_deadline)
                if claimed_count >= SESSION_REPLY_CLAIM_BATCH_SIZE:
                    self._inbox.wake()

                deadlines = [
                    self._last_recovery_at + SESSION_REPLY_RECOVERY_INTERVAL_SECONDS,
                    next_available_at,
                    self._next_cleanup_at,
                ]
                if self._running:
                    deadlines.append(self._last_lease_renewal_at + SESSION_REPLY_LEASE_RENEW_INTERVAL_SECONDS)
                await self._inbox.wait(max(0.0, min(deadlines) - loop.time()))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Session reply consumer loop failed")
                self._inbox.wake()
                await asyncio.sleep(NOTIFICATION_ERROR_RETRY_SECONDS)

    async def _run_claimed(self, work_id: int, worker_id: str, attempt_count: int, max_attempts: int, language: str = "zh") -> None:
        """执行已领取回复工作并根据活动审计状态决定失败处理方式。"""
        locale_token = set_current_locale(language)
        with ExitStack() as log_context:
            try:
                async with AsyncSessionLocal() as db:
                    work = await session_reply_work_item_crud.get(db, work_id)
                    if work is None:
                        return

                    subagent_name = None
                    if work.subagent_id is not None:
                        subagent = await subagent_crud.get_scoped(
                            db,
                            subagent_id=work.subagent_id,
                            uid=work.uid,
                            session_id=work.session_id,
                            profile_id=work.profile_id,
                        )
                        if subagent is None:
                            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
                        subagent_name = subagent.name

                log_context.enter_context(
                    agent_log_context(
                        subagent_name=subagent_name,
                        uid=work.uid,
                        session_id=work.session_id,
                        profile_id=work.profile_id,
                        work_id=work_id,
                    )
                )
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
                if isinstance(exc, LLMContextLengthException) and work is not None and work.work_type == SessionReplyWorkType.AGENT_WORK and work.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN:
                    await fail_session_reply_work(
                        work_id,
                        worker_id,
                        error,
                        user_error=t(ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED),
                    )
                elif isinstance(exc, BaseBusinessException) and (not has_active_audit_execution or isinstance(exc, LLMRequestRejectedException)):
                    await fail_session_reply_work(
                        work_id,
                        worker_id,
                        error,
                        user_error=exc.render_message(),
                    )
                elif has_active_audit_execution or attempt_count >= max_attempts or stream_started:
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
        async with AsyncSessionLocal() as db:
            await reconcile_child_work(db)

    async def _cleanup_terminal_items_if_due(self, now: float) -> None:
        if now < self._next_cleanup_at:
            return
        self._next_cleanup_at = now + SESSION_REPLY_CLEANUP_INTERVAL_SECONDS
        await self._cleanup_subagent_data()
        await self._cleanup_terminal_items()
        await self._cleanup_subagent_notifications()

    async def _cleanup_subagent_data(self) -> None:
        cleanup_counts = {
            "subagent_count": 0,
            "subagent_round_count": 0,
        }
        remaining = SUBAGENT_CLEANUP_MAX_ITEMS_PER_RUN

        for metric, cleanup in (
            ("subagent_count", subagent_crud.cleanup_idle),
            ("subagent_round_count", subagent_crud.cleanup_old_rounds),
        ):
            while remaining > 0:
                batch_size = min(SUBAGENT_CLEANUP_BATCH_SIZE, remaining)
                async with AsyncSessionLocal() as db:
                    cleanup_count = await cleanup(db, batch_size=batch_size)
                cleanup_counts[metric] += cleanup_count
                remaining -= cleanup_count
                if cleanup_count < batch_size or remaining <= 0:
                    break
                await asyncio.sleep(0)

        if sum(cleanup_counts.values()) > 0:
            logger.bind(**cleanup_counts).info("Subagent data cleaned")

    async def _cleanup_terminal_items(self) -> None:
        work_item_count = 0
        stream_event_count = 0
        sequence_count = 0
        checkpoint_message_count = 0
        private_message_count = 0
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
            private_message_count += cleanup_result.private_messages
            sequence_count += cleanup_result.sequences
            checkpoint_message_count += cleanup_result.checkpoint_messages
            remaining -= cleanup_result.total
            if cleanup_result.total < batch_size or remaining <= 0:
                break
            await asyncio.sleep(0)

        if work_item_count + stream_event_count + private_message_count + sequence_count + checkpoint_message_count > 0:
            logger.bind(
                work_item_count=work_item_count,
                stream_event_count=stream_event_count,
                private_message_count=private_message_count,
                sequence_count=sequence_count,
                checkpoint_message_count=checkpoint_message_count,
            ).info("Session reply data cleaned")

    async def _cleanup_subagent_notifications(self) -> None:
        deleted_count = 0
        scanned_count = 0
        remaining = SUBAGENT_NOTIFICATION_CLEANUP_MAX_SCAN_PER_RUN

        while remaining > 0:
            batch_size = min(SUBAGENT_NOTIFICATION_CLEANUP_BATCH_SIZE, remaining)
            async with AsyncSessionLocal() as db:
                cleanup_result = await message_crud.cleanup_orphan_subagent_notifications(
                    db,
                    limit=batch_size,
                    after_id=self._notification_cleanup_after_id,
                )
                await db.commit()
            self._notification_cleanup_after_id = cleanup_result.after_id
            deleted_count += cleanup_result.deleted
            scanned_count += cleanup_result.scanned
            remaining = SUBAGENT_NOTIFICATION_CLEANUP_MAX_SCAN_PER_RUN - scanned_count
            if cleanup_result.scanned < batch_size or cleanup_result.after_id == 0 or remaining <= 0:
                break
            await asyncio.sleep(0)

        if deleted_count > 0:
            logger.bind(subagent_notification_count=deleted_count).info("Subagent notifications cleaned")
