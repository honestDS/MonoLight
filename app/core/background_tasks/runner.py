import asyncio
import json
import os
import uuid
from time import monotonic
from typing import Any

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN, ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE, ERR_TOOL_NOT_REGISTERED
from app.core.crud.audit import audit_crud
from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.core.dispatch_context import build_background_dispatch_context
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.tools import TOOL_EXECUTOR_MAP
from app.core.utils.background_task_result import build_background_task_failure_result, build_background_task_success_result, serialize_execution_summary
from app.models.audit import AuditExecutionStatus
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
    }


def _build_success_result(task: BackgroundTask, raw_result: Any) -> dict[str, Any]:
    value = raw_result
    if isinstance(raw_result, str):
        try:
            value = json.loads(raw_result)
        except (TypeError, ValueError):
            value = raw_result
    content = _limit_result_content(_to_json_compatible(value))
    return build_background_task_success_result(task.tool_name, content)


def _build_failure_result(task: BackgroundTask, error: str) -> dict[str, Any]:
    return build_background_task_failure_result(task.tool_name, error)


def _safe_exception_message(exc: Exception) -> str:
    if isinstance(exc, BaseBusinessException):
        return str(exc.render_message())
    return t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)


def _tool_result_succeeded(raw_result: Any) -> bool:
    if isinstance(raw_result, dict):
        payload = raw_result
    elif isinstance(raw_result, str):
        try:
            payload = json.loads(raw_result)
        except (TypeError, ValueError):
            return True
    else:
        return True
    if not isinstance(payload, dict):
        return True
    return not (payload.get("error") or payload.get("status") == "failed" or (isinstance(payload.get("exit_code"), int) and payload["exit_code"] != 0))


def _audit_binding(task: BackgroundTask) -> tuple[int, int, str] | None:
    audit_record_id = getattr(task, "audit_record_id", None)
    execution_record_id = getattr(task, "audit_execution_record_id", None)
    extra = task.extra if isinstance(task.extra, dict) else {}
    binding = extra.get("audit_binding") if isinstance(extra.get("audit_binding"), dict) else {}
    audit_record_id = audit_record_id or binding.get("audit_record_id")
    execution_record_id = execution_record_id or binding.get("audit_execution_record_id")
    claim_token = binding.get("claim_token")
    if not isinstance(audit_record_id, int) or not isinstance(execution_record_id, int) or not isinstance(claim_token, str) or not claim_token:
        return None
    return audit_record_id, execution_record_id, claim_token


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
                    error=str(exc),
                    retry_seconds=max(0, remaining),
                ),
                exc_info=True,
            )
            if remaining <= 0:
                return False
            await asyncio.sleep(min(retry_delay, remaining))
            retry_delay = min(retry_delay * 2, BACKGROUND_TASK_LEASE_RETRY_MAX_SECONDS)


async def _release_task_claim(task_id: int, worker_id: str, log: Any, expected_lock_until: int | None = None) -> None:
    try:
        async with AsyncSessionLocal() as db:
            released = await background_task_crud.release_claim(
                db,
                task_id=task_id,
                worker_id=worker_id,
                expected_lock_until=expected_lock_until,
            )
        if released:
            log.info(t("LOG_BACKGROUND_TASK_CLAIM_RELEASED"))
        else:
            log.warning(t("LOG_BACKGROUND_TASK_CLAIM_RELEASE_SKIPPED"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error(
            t("LOG_BACKGROUND_TASK_CLAIM_RELEASE_FAILED", error=str(exc)),
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
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
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

        binding = _audit_binding(task)
        execute_invoked = False
        force_unknown = False
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
                raise RuntimeError(t(ERR_TOOL_NOT_REGISTERED, tool_name=task.tool_name))

            instance = executor_cls(project_root=os.getcwd(), uid=task.uid)
            if hasattr(instance, "set_config"):
                instance.set_config(cfg)
            if hasattr(instance, "set_runtime_context"):
                instance.set_runtime_context(dispatch_context=dispatch_context)

            args = dict(task.arguments or {})
            args.pop("run_in_background", None)
            if binding is not None:
                audit_record_id, execution_record_id, claim_token = binding
                extra = {
                    **(task.extra if isinstance(task.extra, dict) else {}),
                    "execution_started": True,
                    "audit_binding": {
                        **(task.extra.get("audit_binding", {}) if isinstance(task.extra, dict) and isinstance(task.extra.get("audit_binding"), dict) else {}),
                        "audit_record_id": audit_record_id,
                        "audit_execution_record_id": execution_record_id,
                        "claim_token": claim_token,
                        "execute_started": True,
                    },
                }
                if not await background_task_crud.mark_execution_started(db, task_id=task_id, worker_id=worker, extra=extra):
                    force_unknown = True
                    raise RuntimeError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                if not await audit_crud.mark_execution_started(db, execution_record_id=execution_record_id, claim_token=claim_token):
                    force_unknown = True
                    raise RuntimeError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
            else:
                await db.commit()
            execute_invoked = True
            raw_result = await instance.execute(**args)
            await db.refresh(task)
            if task.status == BackgroundTaskStatus.CANCELLED:
                log.info(t("LOG_BACKGROUND_TASK_CANCELLED"))
                return False
            if binding is None:
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

            audit_record_id, execution_record_id, claim_token = binding
            execution_status = AuditExecutionStatus.SUCCEEDED if _tool_result_succeeded(raw_result) else AuditExecutionStatus.FAILED
            audit_result_summary = serialize_execution_summary(
                raw_result,
                max_chars=1000,
            )
            result_summary = audit_result_summary
            execution_finished = await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution_record_id,
                status=execution_status,
                result_summary=audit_result_summary,
                error=None if execution_status == AuditExecutionStatus.SUCCEEDED else audit_result_summary,
                commit=False,
            )
            if not execution_finished:
                await db.rollback()
                await _mark_bound_execution_unknown(db, task, worker, t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                return False
            task_result = _build_success_result(task, raw_result) if execution_status == AuditExecutionStatus.SUCCEEDED else _build_failure_result(task, result_summary)
            marked = (
                await background_task_crud.mark_succeeded(
                    db,
                    task_id=task_id,
                    worker_id=worker,
                    result=task_result,
                    auto_reply=task.auto_reply,
                    commit=False,
                )
                if execution_status == AuditExecutionStatus.SUCCEEDED
                else await background_task_crud.mark_failed(
                    db,
                    task_id=task_id,
                    worker_id=worker,
                    error=result_summary,
                    result=task_result,
                    auto_reply=task.auto_reply,
                    commit=False,
                )
            )
            if not marked:
                await db.rollback()
                await _mark_bound_execution_unknown(db, task, worker, t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                return False
            round_status = await audit_crud.finish_execution_round_if_complete(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
                commit=False,
            )
            await db.commit()
            if round_status is not None:
                await update_confirmation_message_status(db, audit_record_id=audit_record_id)
            log.info(t("LOG_BACKGROUND_TASK_SUCCEEDED") if execution_status == AuditExecutionStatus.SUCCEEDED else t("LOG_BACKGROUND_TASK_FAILED", error=result_summary))
            return True
        except Exception as exc:
            if binding is not None and (execute_invoked or force_unknown):
                await _mark_bound_execution_unknown(db, task, worker, t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                return False
            await db.refresh(task)
            if task.status == BackgroundTaskStatus.CANCELLED:
                log.info(t("LOG_BACKGROUND_TASK_CANCELLED"))
                return False
            error_message = _safe_exception_message(exc)
            log.error(t("LOG_BACKGROUND_TASK_FAILED", error=error_message), exc_info=True)
            if binding is not None:
                audit_record_id, execution_record_id, claim_token = binding
                audit_error_summary = serialize_execution_summary(
                    error_message,
                    max_chars=1000,
                )
                execution_finished = await audit_crud.finish_execution_attempt(
                    db,
                    execution_record_id=execution_record_id,
                    status=AuditExecutionStatus.FAILED,
                    result_summary=audit_error_summary,
                    error=audit_error_summary,
                    commit=False,
                )
                if not execution_finished:
                    await db.rollback()
                    await _mark_bound_execution_unknown(db, task, worker, t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                    return False
                marked = await background_task_crud.mark_failed(
                    db,
                    task_id=task_id,
                    worker_id=worker,
                    error=error_message,
                    result=_build_failure_result(task, error_message),
                    auto_reply=task.auto_reply,
                    commit=False,
                )
                if marked:
                    await audit_crud.finish_execution_round_if_complete(
                        db,
                        audit_record_id=audit_record_id,
                        claim_token=claim_token,
                        commit=False,
                    )
                    await db.commit()
                    await update_confirmation_message_status(db, audit_record_id=audit_record_id)
                    return True
                await db.rollback()
                await _mark_bound_execution_unknown(db, task, worker, t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                return False
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


async def _mark_bound_execution_unknown(task_db, task: BackgroundTask, worker: str, error: str) -> None:
    binding = _audit_binding(task)
    if binding is None:
        return
    audit_record_id, execution_record_id, claim_token = binding
    await task_db.rollback()
    await audit_crud.mark_execution_unknown(
        task_db,
        audit_record_id=audit_record_id,
        execution_record_id=execution_record_id,
        claim_token=claim_token,
        error_reason=error,
    )
    await background_task_crud.mark_execution_unknown(
        task_db,
        task_id=task.id,
        worker_id=worker,
        error=error,
        result=_build_failure_result(task, error),
        auto_reply=task.auto_reply,
    )
    await update_confirmation_message_status(task_db, audit_record_id=audit_record_id)
