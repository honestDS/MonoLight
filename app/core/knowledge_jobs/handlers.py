from __future__ import annotations

from dataclasses import dataclass

from chromadb.errors import NotFoundError as ChromaNotFoundError

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_CANCELLATION_REQUESTED,
    ERR_KNOWLEDGE_JOB_DELETE_CLEANUP_FAILED,
    ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_PUBLICATION_FAILED,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    ERR_KNOWLEDGE_JOB_VECTOR_WRITE_FAILED,
    MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_OVERLAP,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_SIZE,
)
from app.core.crud.knowledge.base import (
    knowledge_base_collection_owner_crud,
    knowledge_base_crud,
)
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.embedding.common import EmbeddingRuntimeConfig, embed_texts_with_config, load_embedding_runtime_config
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.i18n import t
from app.core.knowledge import organization_run
from app.core.knowledge.errors import (
    KnowledgeOrganizationConfigurationError,
    KnowledgeOrganizationContextExceededError,
    KnowledgeOrganizationExecutionError,
    KnowledgeOrganizationModelFailedError,
    KnowledgeOrganizationNotConvergedError,
)
from app.core.knowledge.managed import normalize_managed_knowledge_key
from app.core.knowledge.migration import record_knowledge_base_migration_change
from app.core.knowledge.organization_lifecycle import coordinate_organization_terminal, get_organization_parent
from app.core.knowledge.organization_publication import (
    load_knowledge_organization_mutation_item,
    publish_knowledge_organization_plan,
)
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationMerge,
    KnowledgeOrganizationUpdate,
)
from app.core.knowledge.results import ManagedKnowledgeMutationStatus
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobDeterministicError,
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobExecutor,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
    SessionFactory,
)
from app.core.knowledge_jobs.maintenance import handle_knowledge_maintenance
from app.core.knowledge_jobs.manager import KnowledgeJobSubmissionResult, knowledge_job_manager
from app.core.knowledge_jobs.migration import (
    handle_embedding_migration,
    handle_migration_target_cleanup,
    handle_old_collection_cleanup,
)
from app.core.knowledge_jobs.vector_cleanup import (
    create_managed_vector_cleanup_job,
    execute_managed_vector_cleanup,
)
from app.core.utils.text_splitter import TextSplitter
from app.models.knowledge_base import (
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeActorType,
    ManagedKnowledgeSourceType,
)
from app.providers.database import AsyncSessionLocal
from app.providers.database.time import get_database_time
from app.providers.vector import (
    async_delete_collection_items,
    async_get_or_create_collection,
    async_upsert_collection_items,
)


@dataclass(frozen=True, slots=True)
class _ManagedPublicationSnapshot:
    uid: str
    job_id: int
    knowledge_base_id: int
    knowledge_id: int
    version: int
    attempt_count: int
    content: str
    collection_name: str
    embedding_channel_id: int
    embedding_model_id: str
    embedding_dimensions: int | None
    embedding_signature: str | None
    embedding_revision: int
    previous_vector_item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ManagedDeleteSnapshot:
    uid: str
    job_id: int
    knowledge_base_id: int
    knowledge_id: int
    version: int
    collection_name: str
    vector_item_ids: tuple[str, ...]
    rolled_back: bool = False


async def _requeue_collection_cleanup_if_container_deleted(
    context: KnowledgeJobExecutionContext,
    snapshot: _ManagedPublicationSnapshot,
) -> None:
    async with context.session_factory() as db:
        knowledge_base = await knowledge_base_crud.get(db, snapshot.knowledge_base_id)
        if knowledge_base is not None:
            return
        await knowledge_base_collection_owner_crud.requeue_orphan(
            db,
            collection_name=snapshot.collection_name,
        )


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


async def _load_container(db, *, uid: str, knowledge_base_id: int):
    knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
    if knowledge_base is None or knowledge_base.uid != uid or knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return knowledge_base


async def _prepare_publication(
    context: KnowledgeJobExecutionContext,
) -> tuple[_ManagedPublicationSnapshot, EmbeddingRuntimeConfig]:
    job = await context.checkpoint()
    job_id = _positive_int(job.id)
    knowledge_id = _positive_int(job.knowledge_id)
    expected_version = _positive_int(job.expected_version)
    attempt_count = _positive_int(job.attempt_count)
    if job_id is None or knowledge_id is None or expected_version is None or attempt_count is None:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    async with context.session_factory() as db:
        item = await managed_knowledge_item_crud.get_by_id(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
        )
        if item is None or item.deleted_at is not None or item.version != expected_version or item.pending_job_id != job_id:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base = await _load_container(db, uid=job.uid, knowledge_base_id=job.knowledge_base_id)
        active_embedding = resolve_active_knowledge_base_embedding(knowledge_base)
        channel_id = active_embedding.channel_id
        model_id = active_embedding.model_id
        dimensions = active_embedding.dimensions
        collection_name = active_embedding.collection_name
        if _positive_int(channel_id) is None or not model_id or not collection_name:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        try:
            embedding_config = await load_embedding_runtime_config(db, channel_id, model_id)
        except Exception as exc:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED)) from exc
        snapshot = _ManagedPublicationSnapshot(
            uid=job.uid,
            job_id=job_id,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
            version=expected_version,
            attempt_count=attempt_count,
            content=item.content,
            collection_name=collection_name,
            embedding_channel_id=channel_id,
            embedding_model_id=model_id,
            embedding_dimensions=dimensions,
            embedding_signature=knowledge_base.active_embedding_signature,
            embedding_revision=knowledge_base.active_embedding_revision,
            previous_vector_item_ids=tuple(item.vector_item_ids or ()),
        )
    return snapshot, embedding_config


def _build_vector_items(snapshot: _ManagedPublicationSnapshot) -> tuple[list[str], list[str], list[dict]]:
    chunks = TextSplitter(
        chunk_size=MANAGED_KNOWLEDGE_VECTOR_CHUNK_SIZE,
        chunk_overlap=MANAGED_KNOWLEDGE_VECTOR_CHUNK_OVERLAP,
    ).split(snapshot.content)
    if not chunks:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    item_ids = [f"managed_{snapshot.knowledge_base_id}_{snapshot.knowledge_id}_v{snapshot.version}_a{snapshot.attempt_count}_chunk_{index}" for index in range(len(chunks))]
    metadatas = [
        {
            "knowledge_type": "managed",
            "knowledge_base_id": snapshot.knowledge_base_id,
            "managed_knowledge_id": snapshot.knowledge_id,
            "managed_knowledge_version": snapshot.version,
            "chunk_index": index,
            "chunk_count": len(chunks),
        }
        for index in range(len(chunks))
    ]
    return item_ids, chunks, metadatas


async def handle_managed_publication(context: KnowledgeJobExecutionContext) -> KnowledgeJobExecutionResult:
    snapshot, embedding_config = await _prepare_publication(context)
    vector_item_ids, chunks, metadatas = _build_vector_items(snapshot)

    await context.checkpoint()
    try:
        await async_get_or_create_collection(snapshot.collection_name)
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_VECTOR_WRITE_FAILED)) from exc

    await context.checkpoint()
    try:
        embeddings = await embed_texts_with_config(
            embedding_config,
            chunks,
            batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
            dimensions=snapshot.embedding_dimensions,
        )
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED)) from exc
    if len(embeddings) != len(chunks) or any(not vector or (snapshot.embedding_dimensions is not None and len(vector) != snapshot.embedding_dimensions) for vector in embeddings):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED))

    await context.checkpoint()
    async with context.session_factory() as db:
        current = await knowledge_job_crud.get_active_claim(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            worker_id=context.worker_id,
        )
        if current is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        await create_managed_vector_cleanup_job(
            db,
            source_job=current,
            reason="staged",
            collection_name=snapshot.collection_name,
            vector_item_ids=vector_item_ids,
        )
        await db.commit()

    await context.checkpoint()
    try:
        await async_upsert_collection_items(
            snapshot.collection_name,
            vector_item_ids,
            chunks,
            embeddings,
            metadatas,
            batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
        )
    except Exception as exc:
        await _requeue_collection_cleanup_if_container_deleted(context, snapshot)
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_VECTOR_WRITE_FAILED)) from exc

    try:
        await context.checkpoint()
    except Exception:
        await _requeue_collection_cleanup_if_container_deleted(context, snapshot)
        raise
    async with context.session_factory() as db:
        current = await knowledge_job_crud.get_active_claim(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            worker_id=context.worker_id,
        )
        if current is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        knowledge_base = await knowledge_base_crud.lock_owned_by_id(
            db,
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
            await db.rollback()
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        active_embedding = resolve_active_knowledge_base_embedding(knowledge_base)
        if (
            active_embedding.channel_id != snapshot.embedding_channel_id
            or active_embedding.model_id != snapshot.embedding_model_id
            or active_embedding.dimensions != snapshot.embedding_dimensions
            or active_embedding.collection_name != snapshot.collection_name
            or knowledge_base.active_embedding_signature != snapshot.embedding_signature
            or knowledge_base.active_embedding_revision != snapshot.embedding_revision
        ):
            await db.rollback()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        published = await managed_knowledge_item_crud.publish_indexed_version(
            db,
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            knowledge_id=snapshot.knowledge_id,
            expected_version=snapshot.version,
            job_id=snapshot.job_id,
            vector_item_ids=vector_item_ids,
            commit=False,
        )
        if published is None:
            await db.rollback()
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        await record_knowledge_base_migration_change(
            db,
            knowledge_base=knowledge_base,
            source_type=KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE,
            source_id=published.id,
            source_version=published.version,
            action=KnowledgeBaseMigrationDeltaAction.UPSERT,
        )
        ready = await knowledge_base_crud.mark_managed_initial_index_ready(
            db,
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            active_collection_name=snapshot.collection_name,
            commit=False,
        )
        if not ready:
            await db.rollback()
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        stale_vector_ids = [item_id for item_id in snapshot.previous_vector_item_ids if item_id not in set(vector_item_ids)]
        if stale_vector_ids:
            await create_managed_vector_cleanup_job(
                db,
                source_job=current,
                reason="superseded",
                collection_name=snapshot.collection_name,
                vector_item_ids=stale_vector_ids,
            )
        succeeded = await knowledge_job_crud.mark_succeeded(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            owner=context.worker_id,
            result={
                "knowledge_id": snapshot.knowledge_id,
                "version": snapshot.version,
                "vector_item_count": len(vector_item_ids),
            },
            commit=False,
        )
        if not succeeded:
            await db.rollback()
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        await coordinate_organization_terminal(
            db,
            snapshot.uid,
            snapshot.job_id,
            error=None,
            commit=False,
        )
        await db.commit()
    return KnowledgeJobExecutionResult(
        result={
            "knowledge_id": snapshot.knowledge_id,
            "version": snapshot.version,
            "vector_item_count": len(vector_item_ids),
        },
        finalized=True,
    )


async def _prepare_delete(context: KnowledgeJobExecutionContext) -> _ManagedDeleteSnapshot:
    job = await context.checkpoint()
    job_id = _positive_int(job.id)
    knowledge_id = _positive_int(job.knowledge_id)
    expected_version = _positive_int(job.expected_version)
    if job_id is None or knowledge_id is None or expected_version is None:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    async with context.session_factory() as db:
        item = await managed_knowledge_item_crud.get_by_id(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
        )
        if item is None or item.deleted_at is None or item.is_recallable or item.version != expected_version or item.pending_job_id != job_id:
            parent = await get_organization_parent(db, child=job)
            if parent is not None and parent.status in {KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED} and (item is None or (item.deleted_at is None and item.pending_job_id is None)):
                return _ManagedDeleteSnapshot(
                    uid=job.uid,
                    job_id=job_id,
                    knowledge_base_id=job.knowledge_base_id,
                    knowledge_id=knowledge_id,
                    version=expected_version,
                    collection_name="",
                    vector_item_ids=(),
                    rolled_back=True,
                )
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base = await _load_container(db, uid=job.uid, knowledge_base_id=job.knowledge_base_id)
        collection_name = resolve_active_knowledge_base_embedding(knowledge_base).collection_name
        if not collection_name:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        return _ManagedDeleteSnapshot(
            uid=job.uid,
            job_id=job_id,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
            version=expected_version,
            collection_name=collection_name,
            vector_item_ids=tuple(item.vector_item_ids or ()),
        )


async def handle_managed_delete_cleanup(context: KnowledgeJobExecutionContext) -> KnowledgeJobExecutionResult:
    snapshot = await _prepare_delete(context)
    if snapshot.rolled_back:
        await context.checkpoint()
        async with context.session_factory() as db:
            succeeded = await knowledge_job_crud.mark_succeeded(
                db,
                uid=snapshot.uid,
                job_id=snapshot.job_id,
                owner=context.worker_id,
                result={
                    "knowledge_id": snapshot.knowledge_id,
                    "version": snapshot.version,
                    "cleanup_skipped": "organization_rolled_back",
                },
                commit=False,
            )
            if not succeeded:
                await db.rollback()
                raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
            await coordinate_organization_terminal(
                db,
                snapshot.uid,
                snapshot.job_id,
                error=None,
                commit=False,
            )
            await db.commit()
        return KnowledgeJobExecutionResult(
            result={
                "knowledge_id": snapshot.knowledge_id,
                "version": snapshot.version,
                "cleanup_skipped": "organization_rolled_back",
            },
            finalized=True,
        )
    if snapshot.vector_item_ids:
        try:
            await async_delete_collection_items(
                snapshot.collection_name,
                list(snapshot.vector_item_ids),
                batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
            )
        except ChromaNotFoundError:
            pass
        except Exception as exc:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_DELETE_CLEANUP_FAILED)) from exc

    await context.checkpoint()
    async with context.session_factory() as db:
        current = await knowledge_job_crud.get_active_claim(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            worker_id=context.worker_id,
        )
        if current is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        deleted = await managed_knowledge_item_crud.hard_delete_tombstoned(
            db,
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            knowledge_id=snapshot.knowledge_id,
            expected_version=snapshot.version,
            job_id=snapshot.job_id,
            commit=False,
        )
        if not deleted:
            await db.rollback()
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        succeeded = await knowledge_job_crud.mark_succeeded(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            owner=context.worker_id,
            result={"knowledge_id": snapshot.knowledge_id, "version": snapshot.version},
            commit=False,
        )
        if not succeeded:
            await db.rollback()
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        await coordinate_organization_terminal(
            db,
            snapshot.uid,
            snapshot.job_id,
            error=None,
            commit=False,
        )
        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_PUBLICATION_FAILED)) from exc
    return KnowledgeJobExecutionResult(
        result={"knowledge_id": snapshot.knowledge_id, "version": snapshot.version},
        finalized=True,
    )


def _organization_job_error(exc: BaseException) -> KnowledgeJobDeterministicError:
    if isinstance(exc, KnowledgeOrganizationExecutionError):
        message = exc.render_message()
        result = exc.data if isinstance(exc.data, dict) else None
    else:
        message = t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)
        result = None
    return KnowledgeJobDeterministicError(message, result=result)


async def handle_knowledge_organization(context: KnowledgeJobExecutionContext) -> KnowledgeJobExecutionResult:
    job = await context.checkpoint()
    if job.id is None or job.operation not in {
        KnowledgeJobOperation.AUTO_ORGANIZE,
        KnowledgeJobOperation.MANUAL_ORGANIZE,
    }:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    try:
        execution = await organization_run._execute_knowledge_organization_run(
            context.session_factory,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            organization_job_id=job.id,
        )
    except (
        KnowledgeOrganizationConfigurationError,
        KnowledgeOrganizationContextExceededError,
        KnowledgeOrganizationModelFailedError,
        KnowledgeOrganizationNotConvergedError,
        KnowledgeOrganizationExecutionError,
    ) as exc:
        raise _organization_job_error(exc) from exc

    await context.checkpoint()
    publication = None
    if execution.snapshot_id is not None and execution.final_stage_id is not None:
        try:
            publication = await publish_knowledge_organization_plan(
                context.session_factory,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
                organization_job_id=job.id,
                owner=context.worker_id,
                snapshot_id=execution.snapshot_id,
                final_stage_id=execution.final_stage_id,
                plan=execution.plan,
            )
        except KnowledgeOrganizationExecutionError as exc:
            raise _organization_job_error(exc) from exc

    await context.checkpoint()
    mutation_job_ids = list(publication.mutation_job_ids) if publication is not None else []
    return KnowledgeJobExecutionResult(
        result={
            "snapshot_id": execution.snapshot_id,
            "final_stage_id": execution.final_stage_id,
            "final_stage_key": execution.final_stage_key,
            "work_key": execution.work_key,
            "model_id": execution.model_id,
            "stage_count": execution.stage_count,
            "keep_count": sum(item.action == "keep" for item in execution.plan.items),
            "update_count": sum(item.action == "update" for item in execution.plan.items),
            "merge_count": sum(item.action == "merge" for item in execution.plan.items),
            "conflict_count": sum(item.action == "conflict" for item in execution.plan.items),
            "mutation_job_ids": mutation_job_ids,
        },
        wait_for_children=bool(mutation_job_ids),
    )


def _organization_reference(job: KnowledgeJob) -> dict[str, int]:
    if job.id is None or job.parent_job_id is None:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return {
        "organization_job_id": job.parent_job_id,
        "organization_mutation_job_id": job.id,
    }


def _validate_organization_mutation_job(job: KnowledgeJob) -> tuple[dict, int]:
    if job.id is None or job.operation != KnowledgeJobOperation.ORGANIZE_MUTATION or job.parent_job_id is None:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    payload = job.payload
    if not isinstance(payload, dict) or set(payload) - {
        "snapshot_id",
        "stage_id",
        "plan_item_index",
        "action",
        "sources",
        "primary_knowledge_id",
    }:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    required = ("snapshot_id", "stage_id", "plan_item_index", "action", "sources")
    if any(key not in payload for key in required):
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    if any(isinstance(payload[key], bool) or not isinstance(payload[key], int) or payload[key] < 1 for key in ("snapshot_id", "stage_id")):
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    if isinstance(payload["plan_item_index"], bool) or not isinstance(payload["plan_item_index"], int) or payload["plan_item_index"] < 0:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    if payload["action"] not in {"update", "merge"} or not isinstance(payload["sources"], list) or not payload["sources"]:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    if payload["action"] == "merge" and "primary_knowledge_id" not in payload:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return payload, job.parent_job_id


async def handle_organization_mutation(context: KnowledgeJobExecutionContext) -> KnowledgeJobExecutionResult:
    job = await context.checkpoint()
    payload, organization_job_id = _validate_organization_mutation_job(job)
    async with context.session_factory() as db:
        parent = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=organization_job_id)
        if (
            parent is None
            or parent.knowledge_base_id != job.knowledge_base_id
            or parent.operation
            not in {
                KnowledgeJobOperation.AUTO_ORGANIZE,
                KnowledgeJobOperation.MANUAL_ORGANIZE,
            }
            or parent.status not in {KnowledgeJobStatus.RUNNING, KnowledgeJobStatus.SUCCEEDED}
            or parent.cancel_requested_at is not None
        ):
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        plan_item = await load_knowledge_organization_mutation_item(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            snapshot_id=payload["snapshot_id"],
            stage_id=payload["stage_id"],
            plan_item_index=payload["plan_item_index"],
        )
        plan_sources = [(source.knowledge_id, source.expected_version) for source in ((plan_item.source,) if isinstance(plan_item, KnowledgeOrganizationUpdate) else plan_item.sources)]
        if isinstance(plan_item, KnowledgeOrganizationUpdate):
            expected_target = plan_sources[0]
        else:
            expected_target = (
                next(
                    (
                        source.knowledge_id,
                        source.expected_version,
                    )
                    for source in plan_item.sources
                    if source.knowledge_id == plan_item.primary_knowledge_id
                )
                if any(source.knowledge_id == plan_item.primary_knowledge_id for source in plan_item.sources)
                else None
            )
            if expected_target is None:
                raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if payload["action"] != plan_item.action or job.knowledge_id != expected_target[0] or job.expected_version != expected_target[1] or (isinstance(plan_item, KnowledgeOrganizationMerge) and payload.get("primary_knowledge_id") != plan_item.primary_knowledge_id):
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        payload_sources = payload["sources"]
        normalized_payload_sources = []
        for source in payload_sources:
            if not isinstance(source, dict) or set(source) != {"knowledge_id", "expected_version"}:
                raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            knowledge_id = source.get("knowledge_id")
            expected_version = source.get("expected_version")
            if isinstance(knowledge_id, bool) or not isinstance(knowledge_id, int) or knowledge_id < 1 or isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
                raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            normalized_payload_sources.append((knowledge_id, expected_version))
        if tuple(normalized_payload_sources) != tuple(plan_sources):
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        parent = await knowledge_job_crud.lock_by_id(db, uid=job.uid, job_id=organization_job_id)
        now = await get_database_time(db)
        if (
            parent is None
            or parent.uid != job.uid
            or parent.knowledge_base_id != job.knowledge_base_id
            or parent.operation
            not in {
                KnowledgeJobOperation.AUTO_ORGANIZE,
                KnowledgeJobOperation.MANUAL_ORGANIZE,
            }
            or parent.status != KnowledgeJobStatus.RUNNING
            or not parent.locked_by
            or parent.lock_until is None
            or parent.lock_until < now
        ):
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if parent.cancel_requested_at is not None:
            raise KnowledgeJobCancelledError(t(ERR_KNOWLEDGE_JOB_CANCELLATION_REQUESTED))

        reference = _organization_reference(job)
        lock_token = f"job:{organization_job_id}"
        child_job_ids: list[int] = []

        async def submit_update(source_id: int, expected_version: int, target) -> None:
            submission = await knowledge_job_manager.submit_update(
                db,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
                knowledge_id=source_id,
                expected_version=expected_version,
                knowledge_key=target.knowledge_key,
                content=target.content,
                source_type=ManagedKnowledgeSourceType.AUTO_ORGANIZE,
                actor=ManagedKnowledgeActorType.SYSTEM,
                dedupe_key=f"knowledge-organization-publication:{job.id}:update:{source_id}",
                source_reference=reference,
                source_profile_id=parent.source_profile_id,
                parent_job_id=organization_job_id,
                organization_lock_token=lock_token,
                commit=False,
            )
            if submission.status in {ManagedKnowledgeMutationStatus.EXISTING_KEY, ManagedKnowledgeMutationStatus.EXISTING_CONTENT}:
                raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            if submission.job is not None and submission.job.id is not None:
                child_job_ids.append(submission.job.id)

        async def submit_delete(source_id: int, expected_version: int) -> KnowledgeJobSubmissionResult:
            submission = await knowledge_job_manager.submit_delete(
                db,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
                knowledge_id=source_id,
                expected_version=expected_version,
                source_type=ManagedKnowledgeSourceType.AUTO_ORGANIZE,
                actor=ManagedKnowledgeActorType.SYSTEM,
                dedupe_key=f"knowledge-organization-publication:{job.id}:delete:{source_id}",
                source_reference=reference,
                source_profile_id=parent.source_profile_id,
                parent_job_id=organization_job_id,
                organization_lock_token=lock_token,
                commit=False,
            )
            if submission.status in {ManagedKnowledgeMutationStatus.EXISTING_KEY, ManagedKnowledgeMutationStatus.EXISTING_CONTENT}:
                raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            if submission.job is not None and submission.job.id is not None:
                child_job_ids.append(submission.job.id)
            return submission

        if isinstance(plan_item, KnowledgeOrganizationUpdate):
            await submit_update(plan_item.source.knowledge_id, plan_item.source.expected_version, plan_item.target)
        elif isinstance(plan_item, KnowledgeOrganizationMerge):
            normalized_target_key = normalize_managed_knowledge_key(plan_item.target.knowledge_key)
            for source in plan_item.sources:
                if source.knowledge_id != plan_item.primary_knowledge_id:
                    submission = await submit_delete(source.knowledge_id, source.expected_version)
                    deleted_item = submission.item
                    delete_job_id = submission.job.id if submission.job is not None else None
                    if deleted_item is not None and delete_job_id is not None and (deleted_item.knowledge_key is None or normalize_managed_knowledge_key(deleted_item.knowledge_key) == normalized_target_key or deleted_item.content == plan_item.target.content):
                        cleared = await managed_knowledge_item_crud.clear_organization_merge_unique_fields(
                            db,
                            uid=job.uid,
                            knowledge_base_id=job.knowledge_base_id,
                            knowledge_id=source.knowledge_id,
                            expected_version=deleted_item.version,
                            pending_job_id=delete_job_id,
                            organization_lock_token=lock_token,
                            commit=False,
                        )
                        if not cleared:
                            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            await submit_update(plan_item.primary_knowledge_id, expected_target[1], plan_item.target)
        else:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        await db.commit()

    return KnowledgeJobExecutionResult(
        result={
            "parent_job_id": organization_job_id,
            "action": plan_item.action,
            "mutation_job_ids": child_job_ids,
        }
    )


def create_default_knowledge_job_executor(
    *,
    session_factory: SessionFactory = AsyncSessionLocal,
) -> KnowledgeJobExecutor:
    return KnowledgeJobExecutor(
        {
            KnowledgeJobOperation.MANAGED_CREATE: handle_managed_publication,
            KnowledgeJobOperation.MANAGED_UPDATE: handle_managed_publication,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP: handle_managed_delete_cleanup,
            KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP: execute_managed_vector_cleanup,
            KnowledgeJobOperation.EMBEDDING_MIGRATION: handle_embedding_migration,
            KnowledgeJobOperation.OLD_COLLECTION_CLEANUP: handle_old_collection_cleanup,
            KnowledgeJobOperation.MIGRATION_TARGET_CLEANUP: handle_migration_target_cleanup,
            KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE: handle_knowledge_maintenance,
            KnowledgeJobOperation.AUTO_ORGANIZE: handle_knowledge_organization,
            KnowledgeJobOperation.MANUAL_ORGANIZE: handle_knowledge_organization,
            KnowledgeJobOperation.ORGANIZE_MUTATION: handle_organization_mutation,
        },
        session_factory=session_factory,
    )


__all__ = [
    "create_default_knowledge_job_executor",
    "handle_embedding_migration",
    "handle_managed_delete_cleanup",
    "handle_managed_publication",
    "handle_old_collection_cleanup",
    "handle_migration_target_cleanup",
    "handle_knowledge_maintenance",
    "handle_knowledge_organization",
    "handle_organization_mutation",
]
