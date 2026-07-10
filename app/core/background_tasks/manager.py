import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_tasks.runner import run_background_task
from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.models.background_task import BackgroundTask
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

BACKGROUND_TASK_POLL_INTERVAL_SECONDS = 0.5
BACKGROUND_TASK_PROFILE_FETCH_LIMIT = 100


class BackgroundTaskManager:
    def __init__(self) -> None:
        self._running_by_profile: dict[int, set[asyncio.Task]] = defaultdict(set)
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
    ) -> BackgroundTask:
        task = await background_task_crud.create_task(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile.id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            auto_reply=True,
            extra={"allowed_knowledge_base_ids": allowed_knowledge_base_ids or [], "source": source},
        )
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
            self._running_by_profile.clear()

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

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.dispatch_pending_tasks()
            except Exception:
                logger.bind(component="background_task_manager").exception(t("LOG_BACKGROUND_TASK_SCHEDULE_FAILED"))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=BACKGROUND_TASK_POLL_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

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
