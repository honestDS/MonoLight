import asyncio
from collections import defaultdict
from time import monotonic
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.background_tasks.recovery import recover_pending_background_task_replies, recover_pending_background_tasks
from app.core.background_tasks.reply_trigger import trigger_background_task_reply
from app.core.background_tasks.runner import run_background_task
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN
from app.core.crud.audit.audit import audit_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.models.background_task import BackgroundTask
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

BACKGROUND_TASK_POLL_INTERVAL_SECONDS = 0.5
BACKGROUND_TASK_RECOVERY_INTERVAL_SECONDS = 30
BACKGROUND_TASK_PROFILE_FETCH_LIMIT = 100
BACKGROUND_TASK_REPLY_MAX_CONCURRENCY = 4


def _build_submission_context(messages: list[InternalMessage], tool_call_id: str) -> list[dict[str, Any]]:
    context: list[InternalMessage] = []
    for message in messages:
        if message.role == MessageRole.SYSTEM:
            continue
        copied_message = message.model_copy(deep=True)
        if copied_message.role == MessageRole.ASSISTANT and copied_message.tool_calls and any(tool_call.id == tool_call_id for tool_call in copied_message.tool_calls):
            copied_message.tool_calls = [tool_call for tool_call in copied_message.tool_calls if tool_call.id == tool_call_id]
        context.append(copied_message)
    return [message.model_dump(mode="json", exclude_none=True) for message in context]


class BackgroundTaskManager:
    def __init__(self) -> None:
        self._running_by_profile: dict[int, set[asyncio.Task]] = defaultdict(set)
        self._running_replies: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def submit(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile: Profile,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        allowed_knowledge_base_ids: list[int] | None = None,
        source: str = "llm_tool_call",
        messages: list[InternalMessage] | None = None,
        context_summary_boundary_message_id: int | None = None,
    ) -> BackgroundTask:
        audit_record_id = None
        audit_execution_record_id = None
        extra_payload: dict[str, Any] = {
            "allowed_knowledge_base_ids": allowed_knowledge_base_ids or [],
            "source": source,
            "submission_context": _build_submission_context(messages or [], tool_call_id),
        }
        if isinstance(context_summary_boundary_message_id, int) and not isinstance(context_summary_boundary_message_id, bool) and context_summary_boundary_message_id > 0:
            extra_payload["context_summary_user_boundary_message_id"] = context_summary_boundary_message_id
        if hasattr(db, "execute"):
            binding = await audit_crud.get_execution_binding_for_tool_call(db, new_tool_call_id=tool_call_id)
            if binding is not None:
                audit_record, execution = binding
                existing = await background_task_crud.get_by_audit_execution_record_id(db, execution.id)
                if existing is not None:
                    return existing
                if execution.status != "running" or audit_record.status != "executing" or audit_record.execution_claim_token != execution.claim_token:
                    raise RuntimeError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                audit_record_id = audit_record.id
                audit_execution_record_id = execution.id
                extra_payload["audit_binding"] = {
                    "audit_record_id": audit_record.id,
                    "audit_execution_record_id": execution.id,
                    "claim_token": execution.claim_token,
                    "handoff_state": "persisted",
                }
        try:
            task = await background_task_crud.create_task(
                db,
                uid=uid,
                session_id=session_id,
                profile_id=profile.id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                auto_reply=True,
                extra=extra_payload,
                audit_record_id=audit_record_id,
                audit_execution_record_id=audit_execution_record_id,
            )
        except Exception:
            if audit_record_id is not None and audit_execution_record_id is not None:
                await audit_crud.mark_execution_unknown(
                    db,
                    audit_record_id=audit_record_id,
                    execution_record_id=audit_execution_record_id,
                    claim_token=str(extra_payload["audit_binding"]["claim_token"]),
                    error_reason=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN),
                )
                await update_confirmation_message_status(db, audit_record_id=audit_record_id)
            raise
        logger.bind(
            task_id=task.id,
            uid=uid,
            session_id=session_id,
            profile_id=profile.id,
            tool_name=tool_name,
            source=source,
        ).info(t("LOG_BACKGROUND_TASK_QUEUED"))
        return task

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        async with self._lock:
            active_tasks = [task for tasks in self._running_by_profile.values() for task in tasks if not task.done()]
            active_tasks.extend(task for task in self._running_replies if not task.done())
            self._running_by_profile.clear()
            self._running_replies.clear()

        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

    async def dispatch_pending_tasks(self) -> None:
        offset = 0
        while not self._stop_event.is_set():
            async with AsyncSessionLocal() as db:
                profiles = await profile_crud.get_multi_all(
                    db,
                    skip=offset,
                    limit=BACKGROUND_TASK_PROFILE_FETCH_LIMIT,
                )
            for profile in profiles:
                await self.schedule(profile)
            if len(profiles) < BACKGROUND_TASK_PROFILE_FETCH_LIMIT:
                return
            offset += BACKGROUND_TASK_PROFILE_FETCH_LIMIT

    async def schedule(self, profile: Profile) -> None:
        if not profile.id:
            return

        cfg = ProfileConfig.model_validate(profile.configs or {})
        max_concurrency = cfg.tool.background_task_max_concurrency
        log = logger.bind(profile_id=profile.id, max_concurrency=max_concurrency)

        async with self._lock:
            running = self._running_by_profile[profile.id]
            running.difference_update({task for task in running if task.done()})
            free_slots = max(0, max_concurrency - len(running))
            log = log.bind(running_count=len(running), free_slots=free_slots)
            if free_slots <= 0:
                log.info(t("LOG_BACKGROUND_TASK_SCHEDULE_NO_SLOT"))
                return

            async with AsyncSessionLocal() as db:
                pending_tasks = await background_task_crud.list_pending(
                    db,
                    profile_id=profile.id,
                    limit=free_slots,
                )

            for pending_task in pending_tasks:
                if pending_task.id is None:
                    continue
                task = asyncio.create_task(self._run_task(pending_task.id, profile.id))
                running.add(task)

    async def dispatch_pending_replies(self) -> None:
        async with self._lock:
            self._running_replies.difference_update({task for task in self._running_replies if task.done()})
            free_slots = max(0, BACKGROUND_TASK_REPLY_MAX_CONCURRENCY - len(self._running_replies))
            if free_slots <= 0:
                return

            async with AsyncSessionLocal() as db:
                pending_replies = await background_task_crud.list_pending_replies(db, limit=free_slots)

            for pending_reply in pending_replies:
                if pending_reply.id is None:
                    continue
                task = asyncio.create_task(self._run_reply(pending_reply.id))
                self._running_replies.add(task)

    async def _run_loop(self) -> None:
        next_recovery_at = monotonic() + BACKGROUND_TASK_RECOVERY_INTERVAL_SECONDS
        while not self._stop_event.is_set():
            try:
                if monotonic() >= next_recovery_at:
                    await recover_pending_background_tasks()
                    await recover_pending_background_task_replies()
                    next_recovery_at = monotonic() + BACKGROUND_TASK_RECOVERY_INTERVAL_SECONDS
                await self.dispatch_pending_tasks()
                await self.dispatch_pending_replies()
            except Exception:
                logger.bind(component="background_task_manager").exception(t("LOG_BACKGROUND_TASK_SCHEDULE_FAILED"))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=BACKGROUND_TASK_POLL_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

    async def _run_reply(self, task_id: int) -> None:
        try:
            await trigger_background_task_reply(task_id)
        finally:
            current_task = asyncio.current_task()
            async with self._lock:
                if current_task is not None:
                    self._running_replies.discard(current_task)

    async def _run_task(self, task_id: int, profile_id: int) -> None:
        try:
            await run_background_task(task_id)
        finally:
            current_task = asyncio.current_task()
            async with self._lock:
                running = self._running_by_profile.get(profile_id)
                if running and current_task is not None:
                    running.discard(current_task)
                    if not running:
                        self._running_by_profile.pop(profile_id, None)


background_task_manager = BackgroundTaskManager()
