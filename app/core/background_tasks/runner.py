import asyncio
import json
import os
import uuid
from time import monotonic
from typing import Any

from app.core import constants
from app.core.constants import ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE
from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.core.dispatch_context import build_background_dispatch_context
from app.core.i18n import t
from app.core.log import get_logger
from app.core.tools import TOOL_EXECUTOR_MAP
from app.core.utils.background_task_result import build_background_task_failure_result, build_background_task_success_result
from app.core.utils.dispatcher.helpers import format_exception_message
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.profile import ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

MAX_BACKGROUND_TASK_RESULT_CHARS = 32000
BACKGROUND_TASK_LEASE_SECONDS = 300
BACKGROUND_TASK_LEASE_RENEW_INTERVAL_SECONDS = 100
BACKGROUND_TASK_LEASE_RETRY_MAX_SECONDS = 10
BACKGROUND_TASK_LEASE_SAFETY_MARGIN_SECONDS = 10


def _to_json_compatible(value: Any, active_container_ids: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    active_ids = active_container_ids if active_container_ids is not None else set()
    if isinstance(value, dict):
        value_id = id(value)
        if value_id in active_ids:
            return "<circular reference>"
        active_ids.add(value_id)
        try:
            return {key if isinstance(key, str) else str(key): _to_json_compatible(item, active_ids) for key, item in value.items()}
        finally:
            active_ids.remove(value_id)

    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)
        if value_id in active_ids:
            return "<circular reference>"
        active_ids.add(value_id)
        try:
            return [_to_json_compatible(item, active_ids) for item in value]
        finally:
            active_ids.remove(value_id)

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
    return build_background_task_success_result(task.tool_name, content)


def _build_failure_result(task: BackgroundTask, error: str) -> dict[str, Any]:
    return build_background_task_failure_result(task.tool_name, error)


def _build_profile_mismatch_error(task: BackgroundTask) -> str:
    return t(ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE)


async def _renew_task_lease(task_id: int, worker_id: str, log: Any) -> bool:
    lease_deadline = monotonic() + BACKGROUND_TASK_LEASE_SECONDS
    retry_delay = 1.0
    await asyncio.sleep(BACKGROUND_TASK_LEASE_RENEW_INTERVAL_SECONDS)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                renewed = await background_task_crud.renew_lease(
                    db,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_seconds=BACKGROUND_TASK_LEASE_SECONDS,
                )
            if not renewed:
                log.warning(t("LOG_BACKGROUND_TASK_LEASE_LOST"))
                return False
            lease_deadline = monotonic() + BACKGROUND_TASK_LEASE_SECONDS
            retry_delay = 1.0
            await asyncio.sleep(BACKGROUND_TASK_LEASE_RENEW_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            remaining = lease_deadline - monotonic() - BACKGROUND_TASK_LEASE_SAFETY_MARGIN_SECONDS
            log.error(
                t(
                    "LOG_BACKGROUND_TASK_LEASE_RENEW_FAILED",
                    error=format_exception_message(exc),
                    retry_seconds=max(0, remaining),
                ),
                exc_info=True,
            )
            if remaining <= 0:
                return False
            await asyncio.sleep(min(retry_delay, remaining))
            retry_delay = min(retry_delay * 2, BACKGROUND_TASK_LEASE_RETRY_MAX_SECONDS)


async def _release_task_claim(task_id: int, worker_id: str, log: Any) -> None:
    try:
        async with AsyncSessionLocal() as db:
            released = await background_task_crud.release_claim(
                db,
                task_id=task_id,
                worker_id=worker_id,
            )
        if released:
            log.info(t("LOG_BACKGROUND_TASK_CLAIM_RELEASED"))
        else:
            log.warning(t("LOG_BACKGROUND_TASK_CLAIM_RELEASE_SKIPPED"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error(
            t("LOG_BACKGROUND_TASK_CLAIM_RELEASE_FAILED", error=format_exception_message(exc)),
            exc_info=True,
        )


async def run_background_task(task_id: int, *, worker_id: str | None = None) -> None:
    worker = worker_id or str(uuid.uuid4())
    log = logger.bind(task_id=task_id, worker_id=worker)

    async with AsyncSessionLocal() as db:
        task = await background_task_crud.try_claim(
            db,
            task_id=task_id,
            worker_id=worker,
            lease_seconds=BACKGROUND_TASK_LEASE_SECONDS,
        )
        if not task:
            log.info(t("LOG_BACKGROUND_TASK_CLAIM_SKIPPED"))
            return
        log = log.bind(uid=task.uid, session_id=task.session_id, profile_id=task.profile_id, tool_name=task.tool_name)
        log.info(t("LOG_BACKGROUND_TASK_STARTED"))

    execution_task = asyncio.create_task(_execute_claimed_background_task(task_id, worker, log))
    lease_task = asyncio.create_task(_renew_task_lease(task_id, worker, log))
    try:
        done, _pending = await asyncio.wait(
            {execution_task, lease_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if execution_task in done:
            await execution_task
        else:
            lease_renewed = await lease_task
            if not lease_renewed:
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
                await asyncio.shield(_release_task_claim(task_id, worker, log))
                return
    except asyncio.CancelledError:
        execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)
        await asyncio.shield(_release_task_claim(task_id, worker, log))
        raise
    finally:
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)


async def _execute_claimed_background_task(task_id: int, worker: str, log: Any) -> bool:
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if not task:
            log.warning(t("LOG_BACKGROUND_TASK_MISSING"))
            return False

        try:
            profile = await profile_crud.get(db, task.profile_id)
            if not profile or profile.uid != task.uid:
                error_message = _build_profile_mismatch_error(task)
                log.error(t("LOG_BACKGROUND_TASK_PROFILE_UNAVAILABLE"))
                raise RuntimeError(error_message)
            cfg = ProfileConfig.model_validate(profile.configs or {})
            dispatch_context = build_background_dispatch_context(
                uid=task.uid,
                session_id=task.session_id,
                profile=profile,
                db=db,
                task_id=task.id,
                allowed_knowledge_base_ids=task.extra.get("allowed_knowledge_base_ids") if isinstance(task.extra, dict) else None,
            )

            executor_cls = TOOL_EXECUTOR_MAP.get(task.tool_name)
            if not executor_cls:
                raise RuntimeError(t(constants.ERR_TOOL_NOT_REGISTERED, tool_name=task.tool_name))

            instance = executor_cls(project_root=os.getcwd(), uid=task.uid)
            if hasattr(instance, "set_config"):
                instance.set_config(cfg)
            if hasattr(instance, "set_runtime_context"):
                instance.set_runtime_context(dispatch_context=dispatch_context)

            args = dict(task.arguments or {})
            args.pop("run_in_background", None)
            raw_result = await instance.execute(**args)
            await db.refresh(task)
            if task.status == BackgroundTaskStatus.CANCELLED:
                log.info(t("LOG_BACKGROUND_TASK_CANCELLED"))
                return False
            marked = await background_task_crud.mark_succeeded(
                db,
                task_id=task_id,
                worker_id=worker,
                result=_build_success_result(task, raw_result),
                auto_reply=task.auto_reply,
            )
            if not marked:
                return False
            log.info(t("LOG_BACKGROUND_TASK_SUCCEEDED"))
            return True
        except Exception as exc:
            await db.refresh(task)
            if task.status == BackgroundTaskStatus.CANCELLED:
                log.info(t("LOG_BACKGROUND_TASK_CANCELLED"))
                return False
            error_message = format_exception_message(exc)
            log.error(t("LOG_BACKGROUND_TASK_FAILED", error=error_message), exc_info=True)
            marked = await background_task_crud.mark_failed(
                db,
                task_id=task_id,
                worker_id=worker,
                error=error_message,
                result=_build_failure_result(task, error_message),
                auto_reply=task.auto_reply,
            )
            if not marked:
                return False
            return True
