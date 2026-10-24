import asyncio
from collections import defaultdict
from time import monotonic
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.background_tasks.external_work import reconcile_external_work
from app.core.background_tasks.recovery import recover_pending_background_task_replies, recover_pending_background_tasks
from app.core.background_tasks.reply_trigger import trigger_background_task_reply
from app.core.background_tasks.runner import run_background_task
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN, SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_MAX_CONCURRENCY
from app.core.crud.audit.audit import audit_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import SessionReplyWorkStatus
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

BACKGROUND_TASK_POLL_INTERVAL_SECONDS = 0.5
BACKGROUND_TASK_RECOVERY_INTERVAL_SECONDS = 30
BACKGROUND_TASK_PROFILE_FETCH_LIMIT = 100
BACKGROUND_TASK_REPLY_MAX_CONCURRENCY = 4


def _serialize_request_message(message: InternalMessage) -> dict[str, Any]:
    serialized = message.model_dump(mode="json", exclude_none=True)
    return {key: serialized[key] for key in ("id", "role", "content", "attachments") if key in serialized}


def _build_submission_context(messages: list[InternalMessage], tool_call_id: str) -> list[dict[str, Any]]:
    target_message_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == MessageRole.ASSISTANT and messages[index].tool_calls and any(tool_call.id == tool_call_id for tool_call in messages[index].tool_calls)),
        None,
    )
    context: list[InternalMessage] = []
    removed_tool_call_ids: set[str] = set()
    for index, message in enumerate(messages):
        if message.role == MessageRole.SYSTEM:
            continue
        if message.role in {MessageRole.USER, MessageRole.ASSISTANT}:
            removed_tool_call_ids.clear()
        copied_message = message.model_copy(deep=True)
        if index == target_message_index:
            removed_tool_call_ids.update(tool_call.id for tool_call in copied_message.tool_calls if tool_call.id != tool_call_id)
            copied_message.tool_calls = [tool_call for tool_call in copied_message.tool_calls if tool_call.id == tool_call_id]
        if copied_message.role == MessageRole.TOOL and copied_message.tool_call_id in removed_tool_call_ids:
            continue
        context.append(copied_message)
    return [message.model_dump(mode="json", exclude_none=True) for message in context]


class BackgroundTaskManager:
    def __init__(self) -> None:
        self._running_by_profile: dict[int, set[asyncio.Task]] = defaultdict(set)
        self._running_observers: set[asyncio.Task] = set()
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
        submission_messages = messages or []
        purpose = next(
            (message.content for message in reversed(submission_messages) if message.role == MessageRole.ASSISTANT and message.tool_calls and any(tool_call.id == tool_call_id for tool_call in message.tool_calls)),
            None,
        )
        claim_info = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
        if claim_info is None:
            extra_payload: dict[str, Any] = {
                "allowed_knowledge_base_ids": allowed_knowledge_base_ids or [],
                "source": source,
                "submission_context": _build_submission_context(submission_messages, tool_call_id),
                "purpose": purpose,
            }
        else:
            if not isinstance(claim_info, (tuple, list)) or len(claim_info) != 2 or not isinstance(claim_info[0], int) or isinstance(claim_info[0], bool) or claim_info[0] <= 0 or not isinstance(claim_info[1], str) or not claim_info[1]:
                raise RuntimeError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
            origin_work_id, worker_id = claim_info
            origin_work = await session_reply_work_item_crud.get(db, origin_work_id)
            if origin_work is None or origin_work.uid != uid or origin_work.session_id != session_id or origin_work.profile_id != profile.id or origin_work.status != SessionReplyWorkStatus.RUNNING or origin_work.locked_by != worker_id:
                raise RuntimeError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))

            request_context: list[dict[str, Any]] = []
            for message_id in origin_work.input_message_ids or []:
                message = await message_crud.get(db, message_id)
                if message is None or message.uid != uid or message.session_id != session_id or message.role != MessageRole.USER or message.type != MessageType.TEXT:
                    continue
                parsed_messages = parse_db_messages_to_internal([message])
                if parsed_messages and parsed_messages[0].role == MessageRole.USER:
                    request_context.append(_serialize_request_message(parsed_messages[0]))
            if not request_context:
                for message in reversed(submission_messages):
                    if message.role == MessageRole.USER and message.content is not None:
                        request_context.append(_serialize_request_message(message))
                        break
            extra_payload = {
                "allowed_knowledge_base_ids": allowed_knowledge_base_ids or [],
                "source": source,
                "origin_work_id": origin_work.id,
                "origin_work_identity": origin_work.dedupe_key,
                "request_context": request_context,
                "purpose": purpose,
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
                if "request_context" in extra_payload and not extra_payload["request_context"]:
                    source_assistant_message_id = audit_record.source_assistant_message_id
                    if isinstance(source_assistant_message_id, int) and not isinstance(source_assistant_message_id, bool):
                        history = await message_crud.get_history_backward_by_id(
                            db,
                            session_id=session_id,
                            uid=uid,
                            before_id=source_assistant_message_id,
                            limit=200,
                        )
                        for message in history:
                            if message.role != MessageRole.USER or message.type != MessageType.TEXT:
                                continue
                            parsed_messages = parse_db_messages_to_internal([message])
                            if parsed_messages and parsed_messages[0].role == MessageRole.USER:
                                extra_payload["request_context"] = [_serialize_request_message(parsed_messages[0])]
                                break
                extra_payload["audit_binding"] = {
                    "audit_record_id": audit_record.id,
                    "audit_execution_record_id": execution.id,
                    "claim_token": execution.claim_token,
                    "handoff_state": "persisted",
                }
        origin_work = claim_info is not None
        try:
            task = await background_task_crud.create_task(
                db,
                uid=uid,
                session_id=session_id,
                profile_id=profile.id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                auto_reply=not origin_work,
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
            active_tasks.extend(task for task in self._running_observers if not task.done())
            active_tasks.extend(task for task in self._running_replies if not task.done())
            self._running_by_profile.clear()
            self._running_observers.clear()
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
            else:
                async with AsyncSessionLocal() as db:
                    pending_tasks = await background_task_crud.list_pending(
                        db,
                        profile_id=profile.id,
                        limit=free_slots,
                        observer=False,
                    )

                for pending_task in pending_tasks:
                    if pending_task.id is None:
                        continue
                    task = asyncio.create_task(self._run_task(pending_task.id, profile.id))
                    running.add(task)

            self._running_observers.difference_update({task for task in self._running_observers if task.done()})
            observer_slots = max(0, TERMINAL_WATCH_MAX_CONCURRENCY - len(self._running_observers))
            if observer_slots <= 0:
                return

            async with AsyncSessionLocal() as db:
                pending_observers = await background_task_crud.list_pending(
                    db,
                    profile_id=profile.id,
                    limit=observer_slots,
                    observer=True,
                )

            for pending_observer in pending_observers:
                if pending_observer.id is None:
                    continue
                task = asyncio.create_task(self._run_observer(pending_observer.id))
                self._running_observers.add(task)

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
                    await reconcile_external_work()
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

    async def _run_observer(self, task_id: int) -> None:
        try:
            await run_background_task(task_id)
        finally:
            current_task = asyncio.current_task()
            async with self._lock:
                if current_task is not None:
                    self._running_observers.discard(current_task)


background_task_manager = BackgroundTaskManager()
