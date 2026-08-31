from __future__ import annotations

import hashlib
import json
from typing import Any

from chromadb.errors import NotFoundError as ChromaNotFoundError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_DELETE_CLEANUP_FAILED,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_PAYLOAD_INVALID,
    MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
)
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobDeterministicError,
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobRetryableError,
)
from app.models.knowledge_base import KnowledgeJob, KnowledgeJobOperation, KnowledgeJobStatus
from app.providers.database.time import get_database_time
from app.providers.vector import async_delete_collection_items

_CLEANUP_REASONS = frozenset({"staged", "superseded"})
_CLEANUP_PARENT_OPERATIONS = frozenset(
    {
        KnowledgeJobOperation.MANAGED_CREATE,
        KnowledgeJobOperation.MANAGED_UPDATE,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        KnowledgeJobStatus.SUCCEEDED,
        KnowledgeJobStatus.FAILED,
        KnowledgeJobStatus.CANCELLED,
    }
)


def _invalid_payload() -> KnowledgeJobDeterministicError:
    return KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_PAYLOAD_INVALID))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _validate_vector_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _invalid_payload()
    normalized: list[str] = []
    for item_id in value:
        if not isinstance(item_id, str) or not item_id.strip():
            raise _invalid_payload()
        normalized.append(item_id.strip())
    if len(set(normalized)) != len(normalized):
        raise _invalid_payload()
    return normalized


def _build_payload(
    *,
    source_job_id: int,
    reason: str,
    collection_name: str,
    vector_item_ids: list[str],
) -> dict[str, Any]:
    return {
        "source_job_id": source_job_id,
        "reason": reason,
        "collection_name": collection_name,
        "vector_item_ids": vector_item_ids,
    }


def _request_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _dedupe_key(source_job_id: int, reason: str, payload_hash: str) -> str:
    return f"knowledge-vector-cleanup:{source_job_id}:{reason}:{payload_hash}"


def _validate_cleanup_job(job: KnowledgeJob) -> dict[str, Any]:
    try:
        operation = KnowledgeJobOperation(job.operation)
    except (TypeError, ValueError) as exc:
        raise _invalid_payload() from exc
    if operation != KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP:
        raise _invalid_payload()
    if _positive_int(job.id) is None or _positive_int(job.parent_job_id) is None:
        raise _invalid_payload()
    payload = job.payload
    if not isinstance(payload, dict) or set(payload) != {
        "source_job_id",
        "reason",
        "collection_name",
        "vector_item_ids",
    }:
        raise _invalid_payload()
    source_job_id = _positive_int(payload.get("source_job_id"))
    reason = payload.get("reason")
    collection_name = payload.get("collection_name")
    if source_job_id is None or source_job_id != job.parent_job_id:
        raise _invalid_payload()
    if not isinstance(reason, str) or reason not in _CLEANUP_REASONS:
        raise _invalid_payload()
    if not isinstance(collection_name, str) or not collection_name.strip():
        raise _invalid_payload()
    vector_item_ids = _validate_vector_ids(payload.get("vector_item_ids"))
    return _build_payload(
        source_job_id=source_job_id,
        reason=reason,
        collection_name=collection_name.strip(),
        vector_item_ids=vector_item_ids,
    )


async def create_managed_vector_cleanup_job(
    db: AsyncSession,
    *,
    source_job: KnowledgeJob,
    reason: str,
    collection_name: str,
    vector_item_ids: list[str],
) -> KnowledgeJob:
    source_job_id = _positive_int(source_job.id)
    if source_job_id is None or not isinstance(source_job.uid, str) or not source_job.uid:
        raise _invalid_payload()
    try:
        source_operation = KnowledgeJobOperation(source_job.operation)
    except (TypeError, ValueError) as exc:
        raise _invalid_payload() from exc
    if source_operation not in _CLEANUP_PARENT_OPERATIONS:
        raise _invalid_payload()
    if reason not in _CLEANUP_REASONS:
        raise _invalid_payload()
    if not isinstance(collection_name, str) or not collection_name.strip():
        raise _invalid_payload()
    normalized_ids = _validate_vector_ids(vector_item_ids)
    payload = _build_payload(
        source_job_id=source_job_id,
        reason=reason,
        collection_name=collection_name.strip(),
        vector_item_ids=normalized_ids,
    )
    payload_hash = _request_hash(payload)
    available_at = await get_database_time(db)
    try:
        cleanup_job, _ = await knowledge_job_crud.create(
            db,
            uid=source_job.uid,
            parent_job_id=source_job_id,
            operation=KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP,
            dedupe_key=_dedupe_key(source_job_id, reason, payload_hash),
            request_hash=payload_hash,
            active_change_key=None,
            knowledge_base_id=source_job.knowledge_base_id,
            knowledge_id=source_job.knowledge_id,
            expected_version=source_job.expected_version,
            payload=payload,
            source_session_id=None,
            source_profile_id=None,
            source_message_id=None,
            max_attempts=source_job.max_attempts,
            available_at=available_at,
            commit=False,
        )
    except IntegrityError as exc:
        raise _invalid_payload() from exc
    normalized_payload = _validate_cleanup_job(cleanup_job)
    if cleanup_job.uid != source_job.uid or normalized_payload != payload:
        raise _invalid_payload()
    return cleanup_job


async def execute_managed_vector_cleanup(
    context: KnowledgeJobExecutionContext,
) -> KnowledgeJobExecutionResult:
    cleanup_job = await context.checkpoint()
    payload = _validate_cleanup_job(cleanup_job)
    async with context.session_factory() as db:
        parent = await knowledge_job_crud.get_by_id(
            db,
            uid=cleanup_job.uid,
            job_id=payload["source_job_id"],
        )
        if parent is None:
            raise _invalid_payload()
        if parent.status not in _TERMINAL_STATUSES:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        current_vector_ids: set[str] = set()
        if cleanup_job.knowledge_id is not None:
            item = await managed_knowledge_item_crud.get_by_id(
                db,
                uid=cleanup_job.uid,
                knowledge_base_id=cleanup_job.knowledge_base_id,
                knowledge_id=cleanup_job.knowledge_id,
            )
            if item is not None:
                current_vector_ids = set(item.vector_item_ids or ())

    delete_ids = [item_id for item_id in payload["vector_item_ids"] if item_id not in current_vector_ids]
    if delete_ids:
        try:
            await async_delete_collection_items(
                payload["collection_name"],
                delete_ids,
                batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
            )
        except ChromaNotFoundError:
            pass
        except Exception as exc:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_DELETE_CLEANUP_FAILED)) from exc
    return KnowledgeJobExecutionResult(
        result={
            "source_job_id": payload["source_job_id"],
            "reason": payload["reason"],
            "deleted_count": len(delete_ids),
        },
        finalized=False,
    )


__all__ = [
    "create_managed_vector_cleanup_job",
    "execute_managed_vector_cleanup",
]
