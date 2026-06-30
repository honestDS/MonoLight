import asyncio

from app.core.constants import ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND
from app.core.crud.active_session import active_session_crud
from app.core.crud.profile import profile_crud
from app.core.crud.scheduled_task import scheduled_task_crud
from app.core.crud.session import session_crud
from app.core.crud.user import user_crud
from app.core.exceptions import BaseBusinessException, ServerException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.scheduled_task import ScheduledTask
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

SCHEDULED_TASK_POLL_INTERVAL_SECONDS = 1
SCHEDULED_TASK_FETCH_LIMIT = 100
SCHEDULED_TASK_MAX_CONCURRENT_REPLIES = 4


class ScheduledTaskScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._active_dispatch_tasks: set[asyncio.Task] = set()
        self._active_reply_tasks: set[asyncio.Task] = set()
        self._inflight_task_ids: set[int] = set()
        self._reply_semaphore = asyncio.Semaphore(SCHEDULED_TASK_MAX_CONCURRENT_REPLIES)

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        for task in [*self._active_dispatch_tasks, *self._active_reply_tasks]:
            task.cancel()
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        active_tasks = [*self._active_dispatch_tasks, *self._active_reply_tasks]
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.dispatch_due_tasks()
            except Exception:
                logger.bind(component="scheduled_task_scheduler").exception(t("LOG_SCHEDULED_TASK_DISPATCH_FAILED"))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=SCHEDULED_TASK_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    async def dispatch_due_tasks(self) -> None:
        async with AsyncSessionLocal() as db:
            scheduled_tasks = await scheduled_task_crud.list_due(db, limit=SCHEDULED_TASK_FETCH_LIMIT)
        for scheduled_task in scheduled_tasks:
            if scheduled_task.id is None or scheduled_task.id in self._inflight_task_ids:
                continue
            self._inflight_task_ids.add(scheduled_task.id)
            task = asyncio.create_task(self._dispatch_one(scheduled_task.id))
            self._active_dispatch_tasks.add(task)
            task.add_done_callback(self._active_dispatch_tasks.discard)
            task.add_done_callback(lambda _task, task_id=scheduled_task.id: self._inflight_task_ids.discard(task_id))

    async def _dispatch_one(self, scheduled_task_id: int | None) -> None:
        if scheduled_task_id is None:
            return
        async with AsyncSessionLocal() as db:
            scheduled_task = await scheduled_task_crud.get(db, scheduled_task_id)
            if not scheduled_task:
                return
            await self._dispatch_one_with_db(db, scheduled_task)

    async def _dispatch_one_with_db(self, db, scheduled_task: ScheduledTask) -> None:
        log = logger.bind(
            uid=scheduled_task.uid,
            session_id=scheduled_task.session_id,
            scheduled_task_id=scheduled_task.id,
            scheduled_task_name=scheduled_task.name,
        )
        session = await session_crud.get_by_session_id(db, scheduled_task.session_id)
        if not session or session.uid != scheduled_task.uid:
            log.warning(t("LOG_SCHEDULED_TASK_SESSION_MISSING"))
            await scheduled_task_crud.mark_skipped(db, scheduled_task=scheduled_task)
            return

        # 定时任务按启用时绑定的配置文件执行。
        profile_id = scheduled_task.profile_id
        profile = await profile_crud.get_with_relations(db, profile_id) if profile_id else None
        if not profile or not profile.id or profile.uid != scheduled_task.uid:
            log.bind(profile_id=profile_id).warning(t("LOG_SCHEDULED_TASK_PROFILE_MISSING"))
            await scheduled_task_crud.disable_task(db, scheduled_task=scheduled_task)
            return

        user = await user_crud.get_by_uid(db, scheduled_task.uid)
        username = user.username if user else "Unknown"
        log.info(t("LOG_DISPATCHER_USER_MESSAGE", username=username, message=scheduled_task.message, attachments=str(None)))
        user_message = InternalMessage(role=MessageRole.USER, content=scheduled_task.message)
        saved_message = await save_message(
            db,
            scheduled_task.session_id,
            scheduled_task.uid,
            MessageRole.USER,
            MessageType.SCHEDULED_TASK_TRIGGER,
            user_message,
            profile.id,
            is_processed=True,
        )
        await scheduled_task_crud.mark_dispatched(db, scheduled_task=scheduled_task, message_id=saved_message.id)
        log.bind(message_id=saved_message.id, profile_id=profile.id).info(t("LOG_SCHEDULED_TASK_MESSAGE_QUEUED"))
        reply_task = asyncio.create_task(self._generate_reply(scheduled_task.id, scheduled_task.uid, scheduled_task.session_id, profile.id))
        self._active_reply_tasks.add(reply_task)
        reply_task.add_done_callback(self._active_reply_tasks.discard)

    async def _generate_reply(self, scheduled_task_id: int | None, uid: str, session_id: str, profile_id: int) -> None:
        from app.adapters.chat_ws import ws_chat_adapter
        from app.core.dispatcher import ChatDispatcher

        async with self._reply_semaphore:
            await self._generate_reply_locked(ws_chat_adapter, ChatDispatcher, scheduled_task_id, uid, session_id, profile_id)

    async def _generate_reply_locked(self, ws_chat_adapter, ChatDispatcher, scheduled_task_id: int | None, uid: str, session_id: str, profile_id: int) -> None:
        async with AsyncSessionLocal() as db:
            log = logger.bind(uid=uid, session_id=session_id, scheduled_task_id=scheduled_task_id, profile_id=profile_id)
            await active_session_crud.cleanup_expired_locks(db)
            lock_acquired = await active_session_crud.acquire_lock(db, session_id)
            if not lock_acquired:
                log.info(t("LOG_SCHEDULED_TASK_REPLY_DEFERRED"))
                return

            try:
                log.info(t("LOG_SCHEDULED_TASK_REPLY_STARTED"))
                profile = await profile_crud.get_with_relations(db, profile_id)
                if not profile or profile.uid != uid:
                    disabled_count = await scheduled_task_crud.disable_by_session(db, uid=uid, session_id=session_id)
                    log.bind(disabled_scheduled_tasks=disabled_count).error(t("LOG_BACKGROUND_TASK_PROFILE_UNAVAILABLE", disabled_count=disabled_count))
                    raise ServerException(message=ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)
                ai_msg, turn_messages = await ChatDispatcher._generate_reply_from_history(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile=profile,
                    call_context="scheduled_task_reply",
                    allow_tools=True,
                    restrict_tools_to_background_allowlist=False,
                )
            except Exception as exc:
                error_message = t(exc.message, default=exc.message, **exc.kwargs) if isinstance(exc, BaseBusinessException) else str(exc)
                log.error(t("LOG_SCHEDULED_TASK_REPLY_FAILED", error=error_message), exc_info=True)
                await ws_chat_adapter.send_session_event(
                    uid,
                    session_id,
                    {
                        "type": "proactive_reply_error",
                        "session_id": session_id,
                        "content": f"定时任务回复失败：{error_message}",
                        "task_id": scheduled_task_id,
                    },
                )
            else:
                log.bind(content=ai_msg.content or "[工具调用]").info(t("LOG_SCHEDULED_TASK_REPLY_COMPLETED", content=ai_msg.content or "[工具调用]"))
                await ws_chat_adapter.send_session_event(
                    uid,
                    session_id,
                    {
                        "type": "proactive_reply",
                        "session_id": session_id,
                        "history": [message.model_dump(mode="json") for message in turn_messages],
                        "content": ai_msg.content,
                        "task_id": scheduled_task_id,
                    },
                )
            finally:
                await active_session_crud.release_lock(db, session_id)


scheduled_task_scheduler = ScheduledTaskScheduler()
