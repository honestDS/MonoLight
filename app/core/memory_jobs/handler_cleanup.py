from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PREPARATION_FAILED,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_VERSION_CONFLICT,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_record_crud,
)
from app.core.i18n import t
from app.core.memory import (
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryValidationError,
    build_memory_record_snapshot,
)
from app.core.memory_jobs.executor import (
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
    MemoryJobLeaseLostError,
)
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemorySource,
)
from app.providers.vector import (
    async_delete_collection_items,
    async_validate_collection,
)

from .handler_contracts import (
    _deterministic,
    _MemoryDeleteCleanupSnapshot,
    _require_non_negative_int,
    _require_positive_int,
    _retryable,
    _validate_claim,
    logger,
)
from .handler_organization_validation import (
    _validate_delete_payload,
    _validate_organization_cleanup_source,
)
from .handler_prepare import _prepare_delete_cleanup
from .handler_replacement_validation import _enum_value

__all__ = []


async def _finalize_delete_cleanup(
    context: MemoryJobExecutionContext,
    snapshot: _MemoryDeleteCleanupSnapshot,
) -> MemoryJobExecutionResult:
    await context.checkpoint()
    async with context.session_factory() as db:
        try:
            claim = _validate_claim(
                context,
                await memory_job_crud.get_active_claim(
                    db,
                    uid=snapshot.uid,
                    job_id=snapshot.job_id,
                    owner=snapshot.owner,
                ),
                LongTermMemoryMutationOperation.DELETE_CLEANUP,
            )
            memory_id = _require_positive_int(claim.memory_id)
            expected_version = _require_non_negative_int(claim.expected_version)
            payload = _validate_delete_payload(claim, expected_version)
            if memory_id != snapshot.memory_id or expected_version != snapshot.expected_version:
                raise _deterministic(ERR_MEMORY_VERSION_CONFLICT)
            if claim.active_mutation_key != snapshot.active_mutation_key:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if payload["record_snapshot"] != snapshot.record_snapshot:
                raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
            merge_source: dict[str, Any] | None = None
            if snapshot.organization_parent_job_id is None:
                if payload["source"] == LongTermMemorySource.AUTO_ORGANIZE.value or claim.parent_job_id is not None:
                    raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
            else:
                if payload["source"] != LongTermMemorySource.AUTO_ORGANIZE.value:
                    raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
                (
                    organization_parent_job_id,
                    organization_merge_job_id,
                    merge_source,
                ) = await _validate_organization_cleanup_source(
                    db,
                    job=claim,
                    payload=payload,
                    memory_id=memory_id,
                    expected_version=expected_version,
                )
                if organization_parent_job_id != snapshot.organization_parent_job_id or organization_merge_job_id != snapshot.organization_merge_job_id:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            record = await memory_record_crud.get_by_id(db, uid=snapshot.uid, memory_id=memory_id)
            if record is None or record.pending_mutation_job_id != snapshot.job_id or record.version != expected_version or record.is_active or record.deleted_at is None:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if getattr(record, "vector_item_id", None) != snapshot.vector_item_id:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if snapshot.organization_parent_job_id is None:
                if build_memory_record_snapshot(record) != snapshot.record_snapshot:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            else:
                if record.memory_key is not None or record.content_hash is not None:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                if record.pinned is not merge_source["pinned"]:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                if any(
                    (getattr(record, field) if field not in {"memory_type", "source"} else _enum_value(getattr(record, field))) != snapshot.record_snapshot[field]
                    for field in (
                        "content",
                        "content_token_count",
                        "memory_type",
                        "source",
                        "source_id",
                        "source_session_id",
                        "source_profile_id",
                        "source_message_id",
                        "source_job_id",
                        "change_evidence",
                        "version",
                    )
                ):
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if not await memory_record_crud.delete_tombstone_after_cleanup(
                db,
                uid=snapshot.uid,
                memory_id=memory_id,
                job_id=snapshot.job_id,
                expected_version=expected_version,
                commit=False,
            ):
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            result = {
                "memory_id": memory_id,
                "version": expected_version,
                "vector_item_id": snapshot.vector_item_id,
                "operation": LongTermMemoryMutationOperation.DELETE_CLEANUP.value,
                "record_snapshot": snapshot.record_snapshot,
            }
            if snapshot.organization_parent_job_id is not None:
                result["organization_parent_job_id"] = snapshot.organization_parent_job_id
                result["organization_merge_job_id"] = snapshot.organization_merge_job_id
            if not await memory_job_crud.mark_succeeded(
                db,
                uid=snapshot.uid,
                job_id=snapshot.job_id,
                owner=snapshot.owner,
                result=result,
                commit=False,
            ):
                if (
                    await memory_job_crud.get_active_claim(
                        db,
                        uid=snapshot.uid,
                        job_id=snapshot.job_id,
                        owner=snapshot.owner,
                    )
                    is None
                ):
                    raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
                raise _retryable(ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED)
            await db.commit()
            return MemoryJobExecutionResult(result=result, finalized=True)
        except Exception:
            await db.rollback()
            raise


async def _handle_delete_cleanup(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    snapshot: _MemoryDeleteCleanupSnapshot | None = None
    phase = "preparation"
    try:
        snapshot = await _prepare_delete_cleanup(context)
        await context.checkpoint()
        if snapshot.vector_item_id:
            phase = "delete_cleanup"
            try:
                validation = await async_validate_collection(snapshot.active_collection_name)
                if getattr(validation, "exists", False):
                    await async_delete_collection_items(
                        snapshot.active_collection_name,
                        [snapshot.vector_item_id],
                        batch_size=1,
                    )
            except MemoryJobExecutionError:
                raise
            except Exception as exc:
                raise _retryable(ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED) from exc
        phase = "publication"
        return await _finalize_delete_cleanup(context, snapshot)
    except MemoryJobExecutionError:
        raise
    except (MemoryValidationError, MemoryConflictError, MemoryNotFoundError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    except Exception as exc:
        message_key = ERR_MEMORY_JOB_PREPARATION_FAILED if phase == "preparation" else ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))
        raise _retryable(message_key) from exc
