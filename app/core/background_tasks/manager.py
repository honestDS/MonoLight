import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_tasks.runner import run_background_task
from app.core.crud.background_task import background_task_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.models.background_task import BackgroundTask
from app.models.profile import Profile, ProfileConfig

logger = get_logger(__name__)


class BackgroundTaskManager:
    def __init__(self) -> None:
        self._running_by_profile: dict[int, set[asyncio.Task]] = defaultdict(set)
        self._lock = asyncio.Lock()

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
        await self.schedule(profile)
        return task

    async def schedule(self, profile: Profile) -> None:
        if not profile.id:
            return

        cfg = ProfileConfig.model_validate(profile.configs or {})
        max_concurrency = cfg.tool.background_task_max_concurrency
        log = logger.bind(profile_id=profile.id, max_concurrency=max_concurrency)

        async with self._lock:
            running = self._running_by_profile[profile.id]
            completed = {task for task in running if task.done()}
            running.difference_update(completed)
            free_slots = max(0, max_concurrency - len(running))
            log = log.bind(running_count=len(running), free_slots=free_slots)
            if free_slots <= 0:
                log.info(t("LOG_BACKGROUND_TASK_SCHEDULE_NO_SLOT"))
                return

        from app.providers.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            pending_tasks = await background_task_crud.list_pending(db, profile_id=profile.id, limit=free_slots)

        for pending_task in pending_tasks:
            task = asyncio.create_task(self._run_and_reschedule(pending_task.id, profile))
            async with self._lock:
                self._running_by_profile[profile.id].add(task)

    async def _run_and_reschedule(self, task_id: int, profile: Profile) -> None:
        try:
            await run_background_task(task_id)
        finally:
            async with self._lock:
                running = self._running_by_profile.get(profile.id or 0)
                if running:
                    running.difference_update({task for task in running if task.done()})
            await self.schedule(profile)


background_task_manager = BackgroundTaskManager()
