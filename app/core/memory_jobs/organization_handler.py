from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
    ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED,
    ERR_MEMORY_ORGANIZATION_PLAN_INVALID,
)
from app.core.i18n import t
from app.core.memory.identifiers import build_memory_organization_active_mutation_key
from app.core.memory.organization import (
    MemoryOrganizationContextExceededError,
    MemoryOrganizationExecutionRequest,
    MemoryOrganizationPlanInvalidError,
    build_organization_execution_request,
    call_organization_model,
    is_external_context_length_error,
    validate_organization_model_output,
)
from app.core.memory_jobs.executor import (
    Handler,
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
    MemoryJobRetryableError,
)
from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryMutationOperation


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
    if job.active_mutation_key != expected_active_key or job.memory_id is not None or job.expected_version is not None or job.source_session_id is not None or job.source_profile_id is not None or job.source_message_id is not None:
        raise _deterministic(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))


def _empty_model_result(request: MemoryOrganizationExecutionRequest) -> MemoryJobExecutionResult:
    return MemoryJobExecutionResult(
        result={
            "status": "succeeded",
            "operation": LongTermMemoryMutationOperation.ORGANIZE.value,
            "snapshot_digest": request.snapshot.digest,
            "snapshot_count": request.snapshot.count,
            "model_called": False,
            "finish_reason": "empty_snapshot",
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "budget": request.budget.to_dict(),
            "keep_count": 0,
            "update_count": 0,
            "merge_count": 0,
            "conflict_count": 0,
            "plan_summary": {"items": [], "final_record_count": 0},
            "validation_errors": [],
            "validation_error_count": 0,
            "validation_errors_truncated": False,
        }
    )


def _response_metadata(response: Any) -> tuple[Any, Any]:
    return _json_safe(response.usage), _json_safe(response.finish_reason)


def _organization_plan_invalid_result(
    request: MemoryOrganizationExecutionRequest,
    response: Any,
    error: MemoryOrganizationPlanInvalidError,
) -> dict[str, Any]:
    usage, finish_reason = _response_metadata(response)
    return {
        "status": "organization_plan_invalid",
        "operation": LongTermMemoryMutationOperation.ORGANIZE.value,
        "snapshot_digest": request.snapshot.digest,
        "snapshot_count": request.snapshot.count,
        "model_called": True,
        "finish_reason": finish_reason,
        "usage": usage,
        "budget": request.budget.to_dict(),
        **dict(error.action_counts),
        "plan_summary": error.plan_summary,
        "validation_errors": list(error.validation_errors),
        "validation_error_count": error.validation_error_count,
        "validation_errors_truncated": error.validation_errors_truncated,
    }


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
    if request.snapshot.count == 0:
        await context.checkpoint()
        return _empty_model_result(request)

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
        raise MemoryJobRetryableError(t(ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED)) from exc

    await context.checkpoint()
    try:
        validated_plan = validate_organization_model_output(response.message.content, request.snapshot)
    except MemoryOrganizationPlanInvalidError as exc:
        try:
            result = _organization_plan_invalid_result(request, response, exc)
        except Exception as metadata_exc:
            raise MemoryJobRetryableError(t(ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED)) from metadata_exc
        raise MemoryJobDeterministicError(t(ERR_MEMORY_ORGANIZATION_PLAN_INVALID), result=result) from exc
    try:
        usage, finish_reason = _response_metadata(response)
    except Exception as exc:
        raise MemoryJobRetryableError(t(ERR_MEMORY_ORGANIZATION_MODEL_CALL_FAILED)) from exc
    return MemoryJobExecutionResult(
        result={
            "status": "succeeded",
            "operation": LongTermMemoryMutationOperation.ORGANIZE.value,
            "snapshot_digest": request.snapshot.digest,
            "snapshot_count": request.snapshot.count,
            "model_called": True,
            "finish_reason": finish_reason,
            "usage": usage,
            "budget": request.budget.to_dict(),
            "keep_count": validated_plan.keep_count,
            "update_count": validated_plan.update_count,
            "merge_count": validated_plan.merge_count,
            "conflict_count": validated_plan.conflict_count,
            "plan_summary": validated_plan.plan_summary,
            "validation_errors": [],
            "validation_error_count": 0,
            "validation_errors_truncated": False,
        }
    )


def create_memory_organization_job_handlers() -> Mapping[LongTermMemoryMutationOperation, Handler]:
    return MappingProxyType({LongTermMemoryMutationOperation.ORGANIZE: handle_memory_organization})


__all__ = [
    "create_memory_organization_job_handlers",
    "handle_memory_organization",
]
