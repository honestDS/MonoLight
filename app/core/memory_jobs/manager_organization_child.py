from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_DEDUPE_CONFLICT,
    ERR_MEMORY_JOB_FIELD_INVALID,
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud, memory_store_crud
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecordIndexStatus,
)
from app.providers.database.time import get_database_time

from .manager_common import (
    MemoryJobValidationError,
    _is_active_mutation_key_integrity_error,
    _is_integer,
    _organization_job_target_identity,
    _OrganizationMergeStale,
)

__all__ = [
    "MemoryJobOrganizationChild",
]


class MemoryJobOrganizationChild:
    async def create_organization_merge_child(
        self,
        db: AsyncSession,
        *,
        parent_job: LongTermMemoryMutationJob,
        item: Any,
        group_index: int,
        snapshot_digest: str,
        active_embedding_revision: int,
        index_revision: int,
        policy_version: int,
        commit: bool = False,
    ) -> LongTermMemoryMutationJob | None:
        from app.core.memory.errors import MemoryConflictError, MemoryValidationError
        from app.core.memory.identifiers import (
            build_memory_active_mutation_key,
            build_memory_organization_active_mutation_key,
        )
        from app.core.memory.organization import (
            build_organization_merge_child_dedupe_key,
            build_organization_merge_child_payload,
            validate_organization_submission_store,
        )

        parent_id = parent_job.id
        if isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id < 1:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="parent_job_id"))
        if not isinstance(parent_job.uid, str) or not parent_job.uid.strip():
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="uid"))
        try:
            parent_operation = LongTermMemoryMutationOperation(parent_job.operation)
            parent_status = LongTermMemoryMutationStatus(parent_job.status)
        except (TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
        if (
            parent_operation != LongTermMemoryMutationOperation.ORGANIZE
            or parent_status != LongTermMemoryMutationStatus.RUNNING
            or parent_job.parent_job_id is not None
            or parent_job.cancel_requested_at is not None
            or parent_job.memory_id is not None
            or parent_job.expected_version is not None
            or parent_job.source_session_id is not None
            or parent_job.source_profile_id is not None
            or parent_job.source_message_id is not None
            or parent_job.active_mutation_key != build_memory_organization_active_mutation_key(parent_job.uid)
        ):
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if not _is_integer(parent_job.max_attempts) or parent_job.max_attempts < 1:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        try:
            payload = build_organization_merge_child_payload(
                item,
                parent_job_id=parent_id,
                group_index=group_index,
                snapshot_digest=snapshot_digest,
                active_embedding_revision=active_embedding_revision,
                index_revision=index_revision,
                policy_version=policy_version,
            )
            primary_memory_id = payload["primary_memory_id"]
            raw_sources = payload["sources"]
            target = payload["target"]
            if not _is_integer(primary_memory_id) or primary_memory_id < 1 or not isinstance(raw_sources, list) or not isinstance(target, dict) or not isinstance(target.get("memory_key"), str) or not target["memory_key"] or not isinstance(target.get("content_hash"), str) or not target["content_hash"]:
                raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            source_by_id: dict[int, dict[str, Any]] = {}
            for source in raw_sources:
                if (
                    not isinstance(source, dict)
                    or set(source) != {"memory_id", "expected_version", "pinned"}
                    or not _is_integer(source.get("memory_id"))
                    or source["memory_id"] < 1
                    or not _is_integer(source.get("expected_version"))
                    or source["expected_version"] < 0
                    or not isinstance(source.get("pinned"), bool)
                    or source["memory_id"] in source_by_id
                ):
                    raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
                source_by_id[source["memory_id"]] = dict(source)
            if primary_memory_id not in source_by_id:
                raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            ordered_sources = [source_by_id[memory_id] for memory_id in sorted(source_by_id)]
            payload = {**payload, "sources": ordered_sources}
            expected_version = source_by_id[primary_memory_id]["expected_version"]
            active_mutation_key = build_memory_active_mutation_key(
                parent_job.uid,
                memory_id=primary_memory_id,
            )
            dedupe_key = build_organization_merge_child_dedupe_key(
                parent_job_id=parent_id,
                group_index=group_index,
                payload=payload,
            )
        except (KeyError, MemoryValidationError, StopIteration, TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc

        source_pairs = tuple((source["memory_id"], source["expected_version"]) for source in payload["sources"])
        source_ids = {memory_id for memory_id, _ in source_pairs}
        child_job: LongTermMemoryMutationJob | None = None
        try:
            async with db.begin_nested():
                store = await memory_store_crud.lock_for_mutation(
                    db,
                    uid=parent_job.uid,
                    commit=False,
                )
                if store is None:
                    raise _OrganizationMergeStale()
                try:
                    validate_organization_submission_store(store)
                except MemoryConflictError as exc:
                    raise _OrganizationMergeStale() from exc
                if store.active_embedding_revision != active_embedding_revision or store.index_revision != index_revision:
                    raise _OrganizationMergeStale()

                records = await memory_record_crud.get_organization_group(
                    db,
                    uid=parent_job.uid,
                    memory_ids=source_ids,
                )
                if [record.id for record in records] != sorted(source_ids):
                    raise _OrganizationMergeStale()

                existing_child = await memory_job_crud.get_by_dedupe_key(
                    db,
                    uid=parent_job.uid,
                    dedupe_key=dedupe_key,
                )
                pending_ids: set[int | None] = set()
                for record, (_, source_version) in zip(records, source_pairs, strict=True):
                    if (
                        record.uid != parent_job.uid
                        or record.id is None
                        or record.is_active is not True
                        or record.deleted_at is not None
                        or record.index_status != LongTermMemoryRecordIndexStatus.READY
                        or record.indexed_version != record.version
                        or not isinstance(record.vector_item_id, str)
                        or not record.vector_item_id
                        or record.suppress_recall is not False
                        or record.version != source_version
                    ):
                        raise _OrganizationMergeStale()
                    pending_ids.add(record.pending_mutation_job_id)

                if existing_child is not None:
                    if (
                        existing_child.uid != parent_job.uid
                        or existing_child.parent_job_id != parent_id
                        or existing_child.operation != LongTermMemoryMutationOperation.ORGANIZE_MERGE
                        or existing_child.dedupe_key != dedupe_key
                        or existing_child.active_mutation_key != active_mutation_key
                        or existing_child.memory_id != primary_memory_id
                        or existing_child.expected_version != expected_version
                        or existing_child.payload != payload
                        or existing_child.source_session_id is not None
                        or existing_child.source_profile_id is not None
                        or existing_child.source_message_id is not None
                        or existing_child.max_attempts != parent_job.max_attempts
                    ):
                        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
                    if existing_child.id is None:
                        raise _OrganizationMergeStale()
                    if pending_ids not in ({None}, {existing_child.id}):
                        raise _OrganizationMergeStale()
                    if pending_ids == {existing_child.id} or existing_child.status in {
                        LongTermMemoryMutationStatus.SUCCEEDED,
                        LongTermMemoryMutationStatus.FAILED,
                        LongTermMemoryMutationStatus.CANCELLED,
                    }:
                        child_job = existing_child
                    else:
                        child_job = existing_child
                        reserved = await memory_record_crud.reserve_organization_group(
                            db,
                            uid=parent_job.uid,
                            source_versions=source_pairs,
                            job_id=existing_child.id,
                            commit=False,
                        )
                        if not reserved:
                            raise _OrganizationMergeStale()
                else:
                    target_key = payload["target"]["memory_key"]
                    target_hash = payload["target"]["content_hash"]
                    key_record = await memory_record_crud.get_by_key(
                        db,
                        uid=parent_job.uid,
                        memory_key=target_key,
                    )
                    hash_record = await memory_record_crud.get_by_content_hash(
                        db,
                        uid=parent_job.uid,
                        content_hash=target_hash,
                    )
                    if (key_record is not None and key_record.id not in source_ids) or (hash_record is not None and hash_record.id not in source_ids):
                        raise _OrganizationMergeStale()

                    unfinished_jobs = await memory_job_crud.list_unfinished_by_uid(
                        db,
                        uid=parent_job.uid,
                    )
                    for unfinished_job in unfinished_jobs:
                        identity = _organization_job_target_identity(unfinished_job)
                        if identity is None:
                            continue
                        if identity[0] == target_key or identity[1] == target_hash:
                            raise _OrganizationMergeStale()

                    available_at = await get_database_time(db)
                    try:
                        child_job, created = await memory_job_crud.create(
                            db,
                            uid=parent_job.uid,
                            parent_job_id=parent_id,
                            operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
                            dedupe_key=dedupe_key,
                            active_mutation_key=active_mutation_key,
                            memory_id=primary_memory_id,
                            expected_version=expected_version,
                            payload=payload,
                            source_session_id=None,
                            source_profile_id=None,
                            source_message_id=None,
                            max_attempts=parent_job.max_attempts,
                            available_at=available_at,
                            commit=False,
                        )
                    except IntegrityError as exc:
                        if _is_active_mutation_key_integrity_error(exc):
                            raise _OrganizationMergeStale() from exc
                        raise

                    if not created and (
                        child_job.uid != parent_job.uid
                        or child_job.parent_job_id != parent_id
                        or child_job.operation != LongTermMemoryMutationOperation.ORGANIZE_MERGE
                        or child_job.dedupe_key != dedupe_key
                        or child_job.active_mutation_key != active_mutation_key
                        or child_job.memory_id != primary_memory_id
                        or child_job.expected_version != expected_version
                        or child_job.payload != payload
                        or child_job.source_session_id is not None
                        or child_job.source_profile_id is not None
                        or child_job.source_message_id is not None
                        or child_job.max_attempts != parent_job.max_attempts
                    ):
                        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
                    if child_job.id is None:
                        raise _OrganizationMergeStale()
                    reserved = await memory_record_crud.reserve_organization_group(
                        db,
                        uid=parent_job.uid,
                        source_versions=source_pairs,
                        job_id=child_job.id,
                        commit=False,
                    )
                    if not reserved:
                        raise _OrganizationMergeStale()
        except _OrganizationMergeStale:
            if commit:
                await db.commit()
            return None
        except IntegrityError as exc:
            if _is_active_mutation_key_integrity_error(exc):
                if commit:
                    await db.commit()
                return None
            raise

        if child_job is None:
            return None
        if commit:
            await db.commit()
            await db.refresh(child_job)
        return child_job
