import json
import os
import uuid
from typing import Any

from app.core.background_tasks.reply_trigger import trigger_background_task_reply
from app.core.background_tasks.schemas import BackgroundTaskResult
from app.core.constants import ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE
from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.core.crud.scheduled_task import scheduled_task_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.tools import TOOL_EXECUTOR_MAP
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.profile import ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

MAX_BACKGROUND_TASK_RESULT_CHARS = 32000


def _to_json_compatible(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def _limit_result_content(content: Any) -> Any:
    serialized = json.dumps(content, ensure_ascii=False)
    if len(serialized) <= MAX_BACKGROUND_TASK_RESULT_CHARS:
        return content
    return {
        "truncated": True,
        "original_chars": len(serialized),
        "content": serialized[:MAX_BACKGROUND_TASK_RESULT_CHARS],
    }


def _build_success_result(task: BackgroundTask, raw_result: Any) -> dict[str, Any]:
    content = _limit_result_content(_to_json_compatible(raw_result))
    result = BackgroundTaskResult(
        status="succeeded",
        tool_name=task.tool_name,
        summary=f"Background task {task.id} completed successfully.",
        content=content,
    )
    return result.model_dump()


def _build_failure_result(task: BackgroundTask, error: str) -> dict[str, Any]:
    result = BackgroundTaskResult(
        status="failed",
        tool_name=task.tool_name,
        summary=f"Background task {task.id} failed.",
        error=error,
    )
    return result.model_dump()


def _build_profile_mismatch_error(task: BackgroundTask) -> str:
    return t(ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE)


def _format_exception_message(exc: Exception) -> str:
    if isinstance(exc, BaseBusinessException):
        return t(exc.message, default=exc.message, **exc.kwargs)
    return str(exc)


async def run_background_task(task_id: int, *, worker_id: str | None = None) -> None:
    worker = worker_id or str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        task = await background_task_crud.try_claim(db, task_id=task_id, worker_id=worker)
        if not task:
            return

    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if not task:
            return

        try:
            profile = await profile_crud.get(db, task.profile_id)
            if not profile or profile.uid != task.uid:
                error_message = _build_profile_mismatch_error(task)
                disabled_count = await scheduled_task_crud.disable_by_session(db, uid=task.uid, session_id=task.session_id)
                logger.bind(task_id=task_id, uid=task.uid, session_id=task.session_id, profile_id=task.profile_id, disabled_scheduled_tasks=disabled_count).error(
                    t("LOG_BACKGROUND_TASK_PROFILE_UNAVAILABLE", disabled_count=disabled_count)
                )
                raise RuntimeError(error_message)
            cfg = ProfileConfig.model_validate(profile.configs or {})

            executor_cls = TOOL_EXECUTOR_MAP.get(task.tool_name)
            if not executor_cls:
                raise RuntimeError(f"Tool {task.tool_name} not registered")

            instance = executor_cls(project_root=os.getcwd(), uid=task.uid)
            if hasattr(instance, "set_config"):
                instance.set_config(cfg)
            if hasattr(instance, "set_runtime_context"):
                instance.set_runtime_context(
                    db=db,
                    profile=profile,
                    session_id=task.session_id,
                    allowed_knowledge_base_ids=task.extra.get("allowed_knowledge_base_ids") if isinstance(task.extra, dict) else None,
                )

            args = dict(task.arguments or {})
            args.pop("run_in_background", None)
            raw_result = await instance.execute(**args)
            await db.refresh(task)
            if task.status == BackgroundTaskStatus.CANCELLED:
                return
            task = await background_task_crud.mark_succeeded(db, task=task, result=_build_success_result(task, raw_result))
        except Exception as exc:
            await db.refresh(task)
            if task.status == BackgroundTaskStatus.CANCELLED:
                return
            logger.bind(task_id=task_id, uid=getattr(task, "uid", None), session_id=getattr(task, "session_id", None)).error("Background task failed", exc_info=True)
            error_message = _format_exception_message(exc)
            task = await background_task_crud.mark_failed(db, task=task, error=error_message)
            task.result = _build_failure_result(task, error_message)
            db.add(task)
            await db.commit()
            await db.refresh(task)

    await trigger_background_task_reply(task.id)
