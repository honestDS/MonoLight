from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PUBLICATION_FAILED,
    ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
    ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED,
    ERR_MEMORY_ORGANIZATION_PLAN_INVALID,
    LOG_MEMORY_ORGANIZATION_MODEL_FALLBACK,
    LOG_MEMORY_ORGANIZATION_MODEL_RETRY,
)
from app.core.crud.channel.channel import channel_crud
from app.core.crud.memory.job import memory_job_crud
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory.identifiers import build_memory_organization_active_mutation_key
from app.core.memory.organization import (
    MemoryOrganizationContextExceededError,
    MemoryOrganizationExecutionRequest,
    MemoryOrganizationPlanInvalidError,
    MemoryOrganizationValidatedPlan,
    build_organization_execution_request,
    call_organization_model,
    is_external_context_length_error,
    validate_organization_model_output,
)
from app.core.memory_jobs.executor import (
    Handler,
    MemoryJobCancelledError,
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
    MemoryJobLeaseLostError,
    MemoryJobRetryableError,
)
from app.core.memory_jobs.manager import memory_job_manager
from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryMutationOperation

logger = get_logger(__name__)


def _deterministic(message: str, *, result: dict[str, Any] | None = None) -> MemoryJobDeterministicError:
    return MemoryJobDeterministicError(message, result=result)


def _organization_context_exceeded(
    request: MemoryOrganizationExecutionRequest,
    *,
    external_context_error: bool = False,
) -> MemoryJobDeterministicError:
    budget = request.budget
    result = {
        "status": "organization_context_exceeded",
        "required_tokens": budget.required_input_tokens,
        "available_tokens": budget.available_input_tokens,
        "external_context_error": external_context_error,
        "operation": LongTermMemoryMutationOperation.ORGANIZE.value,
        "snapshot_digest": request.snapshot.digest,
        "snapshot_count": request.snapshot.count,
        "model_called": external_context_error,
        "budget": budget.to_dict(),
    }
    return _deterministic(
        t(
            ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
            required_tokens=budget.required_input_tokens,
            available_tokens=budget.available_input_tokens,
        ),
        result=result,
    )


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        value = {str(key): _json_safe(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        value = [_json_safe(item) for item in value]
    elif hasattr(value, "value") and not isinstance(value, (str, bytes)):
        value = value.value
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _validate_organization_job(job: LongTermMemoryMutationJob) -> None:
    try:
        operation = LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError) as exc:
        raise _deterministic(t(ERR_MEMORY_JOB_OPERATION_INVALID)) from exc
    if operation != LongTermMemoryMutationOperation.ORGANIZE:
        raise _deterministic(t(ERR_MEMORY_JOB_OPERATION_INVALID))
    try:
        expected_active_key = build_memory_organization_active_mutation_key(job.uid)
    except Exception as exc:
        raise _deterministic(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
    if job.active_mutation_key != expected_active_key or job.parent_job_id is not None or job.memory_id is not None or job.expected_version is not None or job.source_session_id is not None or job.source_profile_id is not None or job.source_message_id is not None:
        raise _deterministic(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))


def _organization_success_result(
    request: MemoryOrganizationExecutionRequest,
    *,
    model_called: bool,
    finish_reason: Any,
    usage: Any,
    plan: MemoryOrganizationValidatedPlan,
) -> dict[str, Any]:
    return {
        "status": "succeeded",
        "operation": LongTermMemoryMutationOperation.ORGANIZE.value,
        "snapshot_digest": request.snapshot.digest,
        "snapshot_count": request.snapshot.count,
        "model_called": model_called,
        "finish_reason": finish_reason,
        "usage": usage,
        "budget": request.budget.to_dict(),
        "keep_count": plan.keep_count,
        "update_count": plan.update_count,
        "merge_count": plan.merge_count,
        "conflict_count": plan.conflict_count,
        "plan_summary": plan.plan_summary,
        "validation_errors": [],
        "validation_error_count": 0,
        "validation_errors_truncated": False,
    }


def _response_metadata(response: Any) -> tuple[dict[str, Any], str | None]:
    usage = _json_safe(response.usage)
    finish_reason = _json_safe(response.finish_reason)
    if not isinstance(usage, dict) or (finish_reason is not None and not isinstance(finish_reason, str)):
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    return usage, finish_reason


def _log_organization_model_call_failure(
    request: MemoryOrganizationExecutionRequest,
    claimed_job: LongTermMemoryMutationJob,
    exc: Exception,
) -> None:
    organization_model = request.organization_model
    log_message = LOG_MEMORY_ORGANIZATION_MODEL_RETRY if claimed_job.attempt_count < claimed_job.max_attempts else LOG_MEMORY_ORGANIZATION_MODEL_FALLBACK
    error = exc.render_message() if isinstance(exc, LLMException) else t(ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED)
    logger.bind(
        uid=claimed_job.uid,
        job_id=claimed_job.id,
        operation=claimed_job.operation,
        channel_id=organization_model.channel_id,
        channel_name=f"{organization_model.channel_name} / {organization_model.model_id}",
        model_id=organization_model.model_id,
        model_name=organization_model.model_id,
        attempt_count=claimed_job.attempt_count,
        max_attempts=claimed_job.max_attempts,
        exception_type=type(exc).__name__,
    ).warning(
        t(log_message, error=error),
    )


def _organization_plan_invalid_result(
    request: MemoryOrganizationExecutionRequest,
    error: MemoryOrganizationPlanInvalidError,
    *,
    model_called: bool,
    usage: dict[str, Any],
    finish_reason: str | None,
) -> dict[str, Any]:
    return {
        "status": "organization_plan_invalid",
        "operation": LongTermMemoryMutationOperation.ORGANIZE.value,
        "snapshot_digest": request.snapshot.digest,
        "snapshot_count": request.snapshot.count,
        "model_called": model_called,
        "finish_reason": finish_reason,
        "usage": usage,
        "budget": request.budget.to_dict(),
        **dict(error.action_counts),
        "plan_summary": error.plan_summary,
        "validation_errors": list(error.validation_errors),
        "validation_error_count": error.validation_error_count,
        "validation_errors_truncated": error.validation_errors_truncated,
    }


def _organization_group_result(
    item: Any,
    *,
    group_index: int,
    status: str,
    child_job_id: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "group_index": group_index,
        "action": item.action,
        "source_memory_ids": [source.memory_id for source in item.sources],
        "status": status,
    }
    if item.action in {"update", "merge"}:
        result["primary_memory_id"] = item.primary_memory_id or item.sources[0].memory_id
    if child_job_id is not None:
        result["child_job_id"] = child_job_id
    return result


async def _submit_organization_plan(
    context: MemoryJobExecutionContext,
    request: MemoryOrganizationExecutionRequest,
    validated_plan: MemoryOrganizationValidatedPlan,
    result: dict[str, Any],
) -> MemoryJobExecutionResult:
    await context.checkpoint()
    async with context.session_factory() as db:
        try:
            parent_job_id = context.job.id
            if parent_job_id is None:
                raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
            parent_job = await memory_job_crud.get_active_claim(
                db,
                uid=context.job.uid,
                job_id=parent_job_id,
                owner=context.worker_id,
            )
            if parent_job is None:
                raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
            if parent_job.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            _validate_organization_job(parent_job)
            channel = await channel_crud.lock_for_mutation(
                db,
                channel_id=request.organization_model.channel_id,
                commit=False,
            )
            if channel is None:
                raise MemoryJobRetryableError(t(ERR_MEMORY_JOB_PUBLICATION_FAILED))

            child_job_ids: list[int] = []
            group_results: list[dict[str, Any]] = []
            stale_count = 0
            skipped_count = 0
            for group_index, item in enumerate(validated_plan.items):
                parent_job = await memory_job_crud.get_active_claim(
                    db,
                    uid=context.job.uid,
                    job_id=parent_job_id,
                    owner=context.worker_id,
                )
                if parent_job is None:
                    raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
                if parent_job.cancel_requested_at is not None:
                    raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
                if item.action == "keep":
                    skipped_count += 1
                    group_results.append(_organization_group_result(item, group_index=group_index, status="skipped"))
                    continue
                if item.action == "conflict":
                    group_results.append(_organization_group_result(item, group_index=group_index, status="conflict"))
                    continue

                child_job = await memory_job_manager.create_organization_merge_child(
                    db,
                    parent_job=parent_job,
                    item=item,
                    group_index=group_index,
                    snapshot_digest=request.snapshot.digest,
                    active_embedding_revision=request.snapshot.active_embedding_revision,
                    index_revision=request.snapshot.index_revision,
                    policy_version=request.snapshot.policy_version,
                    commit=False,
                )
                if child_job is None:
                    stale_count += 1
                    group_results.append(_organization_group_result(item, group_index=group_index, status="stale"))
                    continue
                if child_job.id is None:
                    raise MemoryJobRetryableError(t(ERR_MEMORY_JOB_PUBLICATION_FAILED))
                child_job_ids.append(child_job.id)
                group_results.append(
                    _organization_group_result(
                        item,
                        group_index=group_index,
                        status="submitted",
                        child_job_id=child_job.id,
                    )
                )

            final_result = {
                **result,
                "completion_scope": "plan_submitted",
                "child_job_ids": child_job_ids,
                "stale_count": stale_count,
                "skipped_count": skipped_count,
                "group_results": group_results,
            }
            marked = await memory_job_crud.mark_succeeded(
                db,
                uid=parent_job.uid,
                job_id=parent_job.id,
                owner=context.worker_id,
                result=final_result,
                commit=False,
            )
            if not marked:
                current = await memory_job_crud.get_active_claim(
                    db,
                    uid=parent_job.uid,
                    job_id=parent_job.id,
                    owner=context.worker_id,
                )
                if current is None:
                    raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
                if current.cancel_requested_at is not None:
                    raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
                raise MemoryJobRetryableError(t(ERR_MEMORY_JOB_PUBLICATION_FAILED))
            from app.core.channel_model_protection import finalize_pending_channel_model_deletions_for_organization_job

            await finalize_pending_channel_model_deletions_for_organization_job(db, job=parent_job)
            await db.commit()
            return MemoryJobExecutionResult(result=final_result, finalized=True)
        except Exception:
            await db.rollback()
            raise


async def _persist_organization_plan_checkpoint(
    context: MemoryJobExecutionContext,
    claimed_job: LongTermMemoryMutationJob,
    *,
    model_output: str,
    usage: dict[str, Any],
    finish_reason: str | None,
) -> None:
    if claimed_job.id is None:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    if not isinstance(claimed_job.payload, dict):
        raise _deterministic(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    payload = dict(claimed_job.payload)
    payload["plan_checkpoint"] = {
        "model_output": model_output,
        "usage": usage,
        "finish_reason": finish_reason,
    }
    async with context.session_factory() as db:
        try:
            updated = await memory_job_crud.update_running_payload(
                db,
                uid=claimed_job.uid,
                job_id=claimed_job.id,
                owner=context.worker_id,
                payload=payload,
                commit=True,
            )
            if updated:
                return
            current = await memory_job_crud.get_active_claim(
                db,
                uid=claimed_job.uid,
                job_id=claimed_job.id,
                owner=context.worker_id,
            )
            if current is None:
                raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
            if current.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            raise MemoryJobRetryableError(t(ERR_MEMORY_JOB_PUBLICATION_FAILED))
        except (MemoryJobCancelledError, MemoryJobLeaseLostError, MemoryJobRetryableError, MemoryJobDeterministicError):
            await db.rollback()
            raise
        except Exception as exc:
            await db.rollback()
            raise MemoryJobRetryableError(t(ERR_MEMORY_JOB_PUBLICATION_FAILED)) from exc


async def handle_memory_organization(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    claimed_job = await context.checkpoint()
    _validate_organization_job(claimed_job)
    try:
        request = build_organization_execution_request(claimed_job.payload)
    except MemoryJobExecutionError:
        raise
    except Exception as exc:
        raise _deterministic(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc

    if request.budget.exceeds_hard_window:
        raise _organization_context_exceeded(request)
    if request.plan_checkpoint is not None:
        try:
            validated_plan = validate_organization_model_output(
                request.plan_checkpoint.model_output,
                request.snapshot,
            )
        except MemoryOrganizationPlanInvalidError as exc:
            result = _organization_plan_invalid_result(
                request,
                exc,
                model_called=True,
                usage=request.plan_checkpoint.usage,
                finish_reason=request.plan_checkpoint.finish_reason,
            )
            raise MemoryJobDeterministicError(t(ERR_MEMORY_ORGANIZATION_PLAN_INVALID), result=result) from exc
        return await _submit_organization_plan(
            context,
            request,
            validated_plan,
            _organization_success_result(
                request,
                model_called=True,
                finish_reason=request.plan_checkpoint.finish_reason,
                usage=request.plan_checkpoint.usage,
                plan=validated_plan,
            ),
        )
    if request.snapshot.count == 0:
        empty_plan = MemoryOrganizationValidatedPlan(items=(), final_record_count=0)
        return await _submit_organization_plan(
            context,
            request,
            empty_plan,
            _organization_success_result(
                request,
                model_called=False,
                finish_reason="empty_snapshot",
                usage={
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                plan=empty_plan,
            ),
        )

    await context.checkpoint()
    try:
        response = await call_organization_model(request)
    except MemoryOrganizationContextExceededError as exc:
        raise _organization_context_exceeded(request) from exc
    except MemoryJobExecutionError:
        raise
    except Exception as exc:
        if is_external_context_length_error(exc):
            raise _organization_context_exceeded(request, external_context_error=True) from exc
        _log_organization_model_call_failure(request, claimed_job, exc)
        raise MemoryJobRetryableError(t(ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED)) from exc

    await context.checkpoint()
    try:
        validated_plan = validate_organization_model_output(response.message.content, request.snapshot)
    except MemoryOrganizationPlanInvalidError as exc:
        try:
            usage, finish_reason = _response_metadata(response)
            result = _organization_plan_invalid_result(
                request,
                exc,
                model_called=True,
                usage=usage,
                finish_reason=finish_reason,
            )
        except Exception as metadata_exc:
            raise MemoryJobRetryableError(t(ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED)) from metadata_exc
        raise MemoryJobDeterministicError(t(ERR_MEMORY_ORGANIZATION_PLAN_INVALID), result=result) from exc
    try:
        usage, finish_reason = _response_metadata(response)
    except Exception as exc:
        raise MemoryJobRetryableError(t(ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED)) from exc
    await _persist_organization_plan_checkpoint(
        context,
        claimed_job,
        model_output=response.message.content,
        usage=usage,
        finish_reason=finish_reason,
    )
    return await _submit_organization_plan(
        context,
        request,
        validated_plan,
        _organization_success_result(
            request,
            model_called=True,
            finish_reason=finish_reason,
            usage=usage,
            plan=validated_plan,
        ),
    )


def create_memory_organization_job_handlers() -> Mapping[LongTermMemoryMutationOperation, Handler]:
    return MappingProxyType({LongTermMemoryMutationOperation.ORGANIZE: handle_memory_organization})


__all__ = [
    "create_memory_organization_job_handlers",
    "handle_memory_organization",
]
