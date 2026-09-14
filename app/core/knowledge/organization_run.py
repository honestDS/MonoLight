from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterable
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    ERR_KB_NOT_FOUND,
    ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
    ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID,
    ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID,
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED,
    KNOWLEDGE_ORGANIZATION_AUTO_TRIGGER_ITEMS,
    KNOWLEDGE_ORGANIZATION_JOB_LEASE_RENEW_INTERVAL_SECONDS,
    KNOWLEDGE_ORGANIZATION_JOB_LEASE_SECONDS,
    LOG_KNOWLEDGE_ORGANIZATION_CONFIG_INVALID,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.crud.knowledge.organization import knowledge_organization_stage_crud
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.exceptions import ResourceNotFoundException
from app.core.i18n import t
from app.core.knowledge import organization_analysis, organization_plan, organization_reduction, organization_runtime, organization_scope, organization_types
from app.core.knowledge.errors import (
    KnowledgeOrganizationConfigurationError,
    KnowledgeOrganizationContextExceededError,
    KnowledgeOrganizationExecutionError,
    KnowledgeOrganizationModelFailedError,
    KnowledgeOrganizationNotConvergedError,
)
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.organization_lifecycle import (
    coordinate_organization_cancel_request,
    coordinate_organization_terminal,
)
from app.core.log import get_logger
from app.models.knowledge_base import (
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationStage,
)
from app.providers.database.time import get_database_time

logger = get_logger(__name__)


def _contains_exception(exc: BaseException, exception_type: type[BaseException]) -> bool:
    if isinstance(exc, exception_type):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_exception(nested, exception_type) for nested in exc.exceptions)
    cause = exc.__cause__
    if cause is not None and _contains_exception(cause, exception_type):
        return True
    context = exc.__context__
    return context is not None and not exc.__suppress_context__ and _contains_exception(context, exception_type)


def _work_key(snapshot: KnowledgeOrganizationSnapshot, *, organization_job_id: int) -> str:
    if isinstance(organization_job_id, bool) or not isinstance(organization_job_id, int) or organization_job_id < 1:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))
    payload = {
        "snapshot_key": snapshot.snapshot_key,
        "organization_job_id": organization_job_id,
        "scope": "knowledge_organization",
        "version": 4,
    }
    return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()


async def _resolve_knowledge_organization_model(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    knowledge_base_id: int,
    model_candidates: tuple[organization_runtime.KnowledgeOrganizationModelConfig, ...] | None,
) -> organization_runtime.KnowledgeOrganizationModelConfig:
    if model_candidates is not None:
        resolved = tuple(model_candidates)
    else:
        async with session_factory() as db:
            try:
                resolved = await organization_runtime.load_knowledge_organization_model_candidates(db, uid=uid)
            except Exception as exc:
                logger.bind(uid=uid, knowledge_base_id=knowledge_base_id).warning(t(LOG_KNOWLEDGE_ORGANIZATION_CONFIG_INVALID, error=str(exc)))
                raise KnowledgeOrganizationConfigurationError(cause=f"{type(exc).__name__}: {exc}") from exc
    if not resolved:
        raise KnowledgeOrganizationConfigurationError()
    return resolved[0]


async def _prepare_knowledge_organization_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    knowledge_base_id: int,
    organization_job_id: int,
) -> tuple[KnowledgeOrganizationSnapshot, str]:
    async with session_factory() as db:
        organization_job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=organization_job_id)
        payload = organization_job.payload if organization_job is not None else {}
        if not isinstance(payload, dict):
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))

        knowledge_ids = payload.get("knowledge_ids")
        if knowledge_ids is not None and (not isinstance(knowledge_ids, list) or any(isinstance(knowledge_id, bool) or not isinstance(knowledge_id, int) or knowledge_id < 1 for knowledge_id in knowledge_ids)):
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))

        snapshot_nonce = payload.get("snapshot_nonce")
        if snapshot_nonce is not None and (not isinstance(snapshot_nonce, str) or not snapshot_nonce.strip()):
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))

        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            organization_job_id=organization_job_id,
            knowledge_ids=knowledge_ids,
            snapshot_nonce=snapshot_nonce,
        )
        knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
        if knowledge_base is None or knowledge_base.uid != uid:
            raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
        await db.refresh(knowledge_base)
        if knowledge_base.active_embedding_revision != snapshot.active_embedding_revision or knowledge_base.index_revision != snapshot.index_revision:
            raise KnowledgeOrganizationExecutionError(
                message=ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID,
                code=409,
                status="organization_snapshot_stale",
                retryable=True,
            )
        owner = organization_job.locked_by if organization_job is not None else None
        if not owner:
            raise KnowledgeOrganizationExecutionError(
                message=ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
                code=409,
                status="organization_lease_lost",
                retryable=True,
            )
        payload = {
            **payload,
            "snapshot_id": snapshot.id,
            "snapshot_key": snapshot.snapshot_key,
            "snapshot_item_count": snapshot.item_count,
        }
        updated_job = await knowledge_job_crud.update_running_payload(
            db,
            uid=uid,
            job_id=organization_job_id,
            owner=owner,
            payload=payload,
        )
        if updated_job is None:
            raise KnowledgeOrganizationExecutionError(
                message=ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
                code=409,
                status="organization_lease_lost",
                retryable=True,
            )
        return snapshot, resolve_active_knowledge_base_embedding(knowledge_base).collection_name


async def _execute_knowledge_organization_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    knowledge_base_id: int,
    organization_job_id: int,
    model_candidates: tuple[organization_runtime.KnowledgeOrganizationModelConfig, ...] | None = None,
    initial_model: organization_runtime.KnowledgeOrganizationModelConfig | None = None,
    snapshot: KnowledgeOrganizationSnapshot | None = None,
    collection_name: str | None = None,
    model_caller: Callable[..., Any] | None = None,
    analysis_caller: Callable[..., Any] | None = None,
    semantic_neighbor_loader: Callable[..., Any] | None = None,
) -> organization_types.KnowledgeOrganizationExecutionResult:
    model = initial_model or await _resolve_knowledge_organization_model(
        session_factory,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        model_candidates=model_candidates,
    )
    call_model = model_caller or organization_plan._default_model_caller
    call_analysis = analysis_caller or organization_analysis._default_analysis_caller
    load_neighbors = semantic_neighbor_loader or (lambda scope, collection: organization_runtime.load_vector_semantic_neighbors(scope, collection_name=collection))

    if snapshot is None or collection_name is None:
        snapshot, collection_name = await _prepare_knowledge_organization_snapshot(
            session_factory,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            organization_job_id=organization_job_id,
        )
    if snapshot.item_count == 0:
        return organization_types.KnowledgeOrganizationExecutionResult(
            plan=organization_types.KnowledgeOrganizationPlan(items=()),
            model_id=None,
            stage_count=0,
            snapshot_id=snapshot.id,
            final_stage_id=None,
            final_stage_key=None,
            work_key=None,
        )

    work_key = _work_key(snapshot, organization_job_id=organization_job_id)
    lower_stage: KnowledgeOrganizationStage | None = None
    stage_count = 0
    layer_index = 0
    while True:
        if model_candidates is None and layer_index > 0:
            model = await _resolve_knowledge_organization_model(
                session_factory,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
                model_candidates=None,
            )
        single_scope: tuple[organization_types.KnowledgeOrganizationScopeItem, ...] | None = None
        if lower_stage is None:
            single_scope = await organization_scope._try_load_single_request_scope(
                session_factory,
                snapshot=snapshot,
                model=model,
            )

        try:
            selected_result, added_analysis_stages = await organization_plan._execute_plan_stage_for_model(
                session_factory,
                snapshot=snapshot,
                work_key=work_key,
                stage_index=layer_index,
                lower_stage=lower_stage,
                model=model,
                collection_name=collection_name,
                model_caller=call_model,
                analysis_caller=call_analysis,
                semantic_neighbor_loader=load_neighbors,
                single_scope=single_scope,
                organization_job_id=organization_job_id,
            )
            stage_count += 1 + added_analysis_stages
        except Exception as exc:
            if _contains_exception(exc, KnowledgeOrganizationContextExceededError):
                raise KnowledgeOrganizationContextExceededError() from exc
            if _contains_exception(exc, KnowledgeOrganizationNotConvergedError):
                raise KnowledgeOrganizationNotConvergedError() from exc
            if isinstance(exc, KnowledgeOrganizationModelFailedError):
                raise
            raise KnowledgeOrganizationModelFailedError(cause=f"{type(exc).__name__}: {exc}") from exc

        completed_stage = selected_result.stage
        if completed_stage.expected_fragment_count == 1:
            return organization_types.KnowledgeOrganizationExecutionResult(
                plan=await organization_reduction._combine_stage_plan(session_factory, stage=completed_stage),
                model_id=model.model_id,
                stage_count=stage_count,
                snapshot_id=snapshot.id,
                final_stage_id=completed_stage.id,
                final_stage_key=completed_stage.stage_key,
                work_key=work_key,
            )
        lower_stage = completed_stage
        layer_index += 1


async def _create_direct_organization_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    knowledge_base_id: int,
) -> tuple[int, str]:
    run_nonce = uuid4().hex
    worker_id = uuid4().hex
    request_payload = {
        "knowledge_base_id": knowledge_base_id,
        "source": "direct_stage14",
        "run_nonce": run_nonce,
    }
    request_hash = hashlib.sha256(canonical_json_dumps(request_payload).encode("utf-8")).hexdigest()
    async with session_factory() as db:
        knowledge_base = await knowledge_base_crud.lock_owned_by_id(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base is None:
            raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
        try:
            job = await knowledge_job_crud.create_claimed(
                db,
                uid=uid,
                owner=worker_id,
                lease_seconds=KNOWLEDGE_ORGANIZATION_JOB_LEASE_SECONDS,
                operation=KnowledgeJobOperation.MANUAL_ORGANIZE,
                dedupe_key=f"manual-organize:{knowledge_base_id}:{run_nonce}",
                request_hash=request_hash,
                active_change_key=f"kb-organization:{knowledge_base_id}",
                knowledge_base_id=knowledge_base_id,
                payload=request_payload,
                max_attempts=1,
            )
        except IntegrityError as exc:
            await db.rollback()
            raise KnowledgeOrganizationExecutionError(
                message=ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
                code=409,
                status="organization_target_busy",
                retryable=True,
            ) from exc
        if job.id is None:
            raise KnowledgeOrganizationExecutionError()
        return job.id, worker_id


async def _renew_direct_organization_job_lease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    worker_id: str,
    done: asyncio.Event,
) -> None:
    while not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), timeout=KNOWLEDGE_ORGANIZATION_JOB_LEASE_RENEW_INTERVAL_SECONDS)
            return
        except TimeoutError:
            pass
        async with session_factory() as db:
            renewed = await knowledge_job_crud.renew_lease(
                db,
                uid=uid,
                job_id=job_id,
                owner=worker_id,
                lease_seconds=KNOWLEDGE_ORGANIZATION_JOB_LEASE_SECONDS,
            )
        if not renewed:
            raise KnowledgeOrganizationExecutionError(
                message=ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
                code=409,
                status="organization_lease_lost",
                retryable=True,
            )


async def _mark_direct_organization_job_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    worker_id: str,
    result: organization_types.KnowledgeOrganizationExecutionResult | None,
    error: BaseException | None,
) -> bool:
    error_message = None if error is None else f"{type(error).__name__}: {error}"
    async with session_factory() as db:
        if error is None:
            updated = await knowledge_job_crud.mark_succeeded(
                db,
                uid=uid,
                job_id=job_id,
                owner=worker_id,
                result={
                    "snapshot_id": result.snapshot_id if result is not None else None,
                    "stage_count": result.stage_count if result is not None else 0,
                    "model_id": result.model_id if result is not None else None,
                },
                commit=False,
            )
        else:
            updated = await knowledge_job_crud.mark_failed(
                db,
                uid=uid,
                job_id=job_id,
                owner=worker_id,
                error=error_message,
                commit=False,
            )
        if updated:
            await coordinate_organization_terminal(db, uid=uid, job_id=job_id, error=error_message)
            await db.commit()
        return updated


async def execute_knowledge_organization(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    knowledge_base_id: int,
    model_candidates: tuple[organization_runtime.KnowledgeOrganizationModelConfig, ...] | None = None,
    model_caller: Callable[..., Any] | None = None,
    analysis_caller: Callable[..., Any] | None = None,
    semantic_neighbor_loader: Callable[..., Any] | None = None,
) -> organization_types.KnowledgeOrganizationExecutionResult:
    initial_model = await _resolve_knowledge_organization_model(
        session_factory,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        model_candidates=model_candidates,
    )
    organization_job_id, worker_id = await _create_direct_organization_job(
        session_factory,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    try:
        snapshot, collection_name = await _prepare_knowledge_organization_snapshot(
            session_factory,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            organization_job_id=organization_job_id,
        )
    except BaseException as exc:
        await _mark_direct_organization_job_terminal(
            session_factory,
            uid=uid,
            job_id=organization_job_id,
            worker_id=worker_id,
            result=None,
            error=exc,
        )
        raise

    done = asyncio.Event()
    run_task = asyncio.create_task(
        _execute_knowledge_organization_run(
            session_factory,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            organization_job_id=organization_job_id,
            model_candidates=model_candidates,
            initial_model=initial_model,
            snapshot=snapshot,
            collection_name=collection_name,
            model_caller=model_caller,
            analysis_caller=analysis_caller,
            semantic_neighbor_loader=semantic_neighbor_loader,
        )
    )
    lease_task = asyncio.create_task(
        _renew_direct_organization_job_lease(
            session_factory,
            uid=uid,
            job_id=organization_job_id,
            worker_id=worker_id,
            done=done,
        )
    )
    try:
        completed, _pending = await asyncio.wait({run_task, lease_task}, return_when=asyncio.FIRST_COMPLETED)
        if lease_task in completed:
            lease_error = lease_task.exception()
            if lease_error is not None:
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
                raise lease_error
        result = await run_task
        done.set()
        await lease_task
        if not await _mark_direct_organization_job_terminal(
            session_factory,
            uid=uid,
            job_id=organization_job_id,
            worker_id=worker_id,
            result=result,
            error=None,
        ):
            raise KnowledgeOrganizationExecutionError()
        return result
    except BaseException as exc:
        done.set()
        if not lease_task.done():
            lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
        await _mark_direct_organization_job_terminal(
            session_factory,
            uid=uid,
            job_id=organization_job_id,
            worker_id=worker_id,
            result=None,
            error=exc,
        )
        raise


def _normalize_organization_scope_ids(value: Iterable[int] | None) -> list[int] | None:
    if value is None:
        return None
    normalized: set[int] = set()
    for knowledge_id in value:
        if isinstance(knowledge_id, bool) or not isinstance(knowledge_id, int) or knowledge_id < 1:
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))
        normalized.add(knowledge_id)
    if not normalized:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))
    return sorted(normalized)


def _organization_job_payload(
    *,
    knowledge_base_id: int,
    knowledge_ids: list[int] | None,
    source: str,
    snapshot_nonce: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "knowledge_base_id": knowledge_base_id,
        "source": source,
        "snapshot_nonce": snapshot_nonce,
    }
    if knowledge_ids is not None:
        payload["knowledge_ids"] = knowledge_ids
    return payload


def _published_organization_mutation_count(children: Iterable[KnowledgeJob]) -> int:
    child_items = list(children)
    children_by_id = {child.id: child for child in child_items if child.id is not None}
    succeeded = 0
    for mutation in child_items:
        if mutation.operation != KnowledgeJobOperation.ORGANIZE_MUTATION or mutation.status != KnowledgeJobStatus.SUCCEEDED or not isinstance(mutation.result, dict):
            continue
        publication_ids = mutation.result.get("mutation_job_ids")
        if not isinstance(publication_ids, list) or not publication_ids or any(isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1 for job_id in publication_ids):
            continue
        publication_jobs = [children_by_id.get(job_id) for job_id in publication_ids]
        if all(
            publication_job is not None
            and publication_job.operation
            in {
                KnowledgeJobOperation.MANAGED_CREATE,
                KnowledgeJobOperation.MANAGED_UPDATE,
                KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
            }
            and publication_job.status == KnowledgeJobStatus.SUCCEEDED
            for publication_job in publication_jobs
        ):
            succeeded += 1
    return succeeded


async def _organization_job_view(
    db: AsyncSession,
    job: KnowledgeJob,
    children: Iterable[KnowledgeJob] = (),
) -> dict[str, Any]:
    child_items = list(children)
    status = job.status.value if isinstance(job.status, KnowledgeJobStatus) else str(job.status)
    operation = job.operation.value if isinstance(job.operation, KnowledgeJobOperation) else str(job.operation)
    payload = job.payload if isinstance(job.payload, dict) else {}
    result = job.result if isinstance(job.result, dict) else None
    snapshot_id = None
    for source in (result, payload):
        candidate = source.get("snapshot_id") if source is not None else None
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            snapshot_id = candidate
            break
    if snapshot_id is None:
        organization_progress = {
            "stage_count": 0,
            "completed_stage_count": 0,
            "running_stage_count": 0,
            "failed_stage_count": 0,
            "invalidated_stage_count": 0,
            "expected_fragment_count": 0,
            "succeeded_fragment_count": 0,
            "completed_fragment_count": 0,
            "invalidated_fragment_count": 0,
        }
    else:
        organization_progress = await knowledge_organization_stage_crud.get_snapshot_progress(
            db,
            snapshot_id=snapshot_id,
        )
    terminal = {KnowledgeJobStatus.SUCCEEDED, KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED}
    return {
        "id": job.id,
        "job_id": job.id,
        "operation": operation,
        "status": status,
        "knowledge_base_id": job.knowledge_base_id,
        "parent_job_id": job.parent_job_id,
        "payload": payload,
        "result": result,
        "snapshot_id": snapshot_id,
        "organization_progress": organization_progress,
        "error": job.error,
        "source_profile_id": job.source_profile_id,
        "available_at": job.available_at,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "cancel_requested_at": job.cancel_requested_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "child_job_ids": [child.id for child in child_items if child.id is not None],
        "child_job_count": len(child_items),
        "child_terminal_count": sum(child.status in terminal for child in child_items),
        "child_failed_count": sum(child.status in {KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED} for child in child_items),
        "publication_success_count": _published_organization_mutation_count(child_items),
    }


async def submit_knowledge_organization(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    knowledge_ids: Iterable[int] | None = None,
    source: str = "manual",
    dedupe_key: str | None = None,
    commit: bool = True,
) -> KnowledgeJob:
    normalized_ids = _normalize_organization_scope_ids(knowledge_ids)
    if source not in {"manual", "auto"}:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))
    knowledge_base = await knowledge_base_crud.lock_owned_by_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    if knowledge_base is None:
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        raise KnowledgeOrganizationExecutionError(
            message=ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED,
            code=409,
            status="organization_target_invalid",
            retryable=False,
        )

    if normalized_ids is not None:
        candidates = await managed_knowledge_item_crud.list_organization_candidates(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        candidate_ids = {candidate.item.id for candidate in candidates if candidate.item.id is not None}
        if not set(normalized_ids).issubset(candidate_ids):
            raise KnowledgeOrganizationExecutionError(
                message=ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID,
                code=409,
                status="organization_scope_invalid",
                retryable=False,
            )

    normalized_dedupe_key = dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None
    snapshot_nonce = hashlib.sha256(normalized_dedupe_key.encode("utf-8")).hexdigest() if normalized_dedupe_key is not None else uuid4().hex
    payload = _organization_job_payload(
        knowledge_base_id=knowledge_base_id,
        knowledge_ids=normalized_ids,
        source=source,
        snapshot_nonce=snapshot_nonce,
    )
    request_hash = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    final_dedupe_key = normalized_dedupe_key or f"knowledge-organization:{knowledge_base_id}:{payload['snapshot_nonce']}"
    existing_by_dedupe = await knowledge_job_crud.get_by_dedupe_key(
        db,
        uid=uid,
        dedupe_key=final_dedupe_key,
    )
    if existing_by_dedupe is not None:
        if existing_by_dedupe.request_hash != request_hash or existing_by_dedupe.knowledge_base_id != knowledge_base_id:
            raise KnowledgeOrganizationExecutionError(
                message=ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT,
                code=409,
                status="organization_dedupe_conflict",
                retryable=False,
            )
        if commit:
            await db.commit()
            await db.refresh(existing_by_dedupe)
        else:
            await db.flush()
        return existing_by_dedupe

    active_job = await knowledge_job_crud.get_by_active_change_key(
        db,
        uid=uid,
        active_change_key=f"kb-organization:{knowledge_base_id}",
    )
    if active_job is not None and active_job.status not in {
        KnowledgeJobStatus.SUCCEEDED,
        KnowledgeJobStatus.FAILED,
        KnowledgeJobStatus.CANCELLED,
    }:
        raise KnowledgeOrganizationExecutionError(
            message=ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
            code=409,
            status="organization_target_busy",
            retryable=True,
        )
    try:
        job, created = await knowledge_job_crud.create(
            db,
            uid=uid,
            operation=KnowledgeJobOperation.AUTO_ORGANIZE if source == "auto" else KnowledgeJobOperation.MANUAL_ORGANIZE,
            dedupe_key=final_dedupe_key,
            request_hash=request_hash,
            active_change_key=f"kb-organization:{knowledge_base_id}",
            status=KnowledgeJobStatus.PENDING,
            knowledge_base_id=knowledge_base_id,
            payload=payload,
            source_profile_id=knowledge_base.managed_profile_id,
            max_attempts=3,
            available_at=await get_database_time(db),
            commit=False,
        )
    except IntegrityError as exc:
        if commit and db.in_transaction():
            await db.rollback()
        raise KnowledgeOrganizationExecutionError(
            message=ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
            code=409,
            status="organization_target_busy",
            retryable=True,
        ) from exc

    if not created and (job.request_hash != request_hash or job.knowledge_base_id != knowledge_base_id):
        raise KnowledgeOrganizationExecutionError(
            message=ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT,
            code=409,
            status="organization_dedupe_conflict",
            retryable=False,
        )
    if commit:
        await db.commit()
        await db.refresh(job)
    else:
        await db.flush()
    return job


async def submit_auto_knowledge_organization(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    commit: bool = True,
) -> KnowledgeJob | None:
    knowledge_base = await knowledge_base_crud.lock_owned_by_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    if knowledge_base is None or knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        return None

    now = await get_database_time(db)

    candidate_count = await managed_knowledge_item_crud.count_organization_candidates(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    if candidate_count < KNOWLEDGE_ORGANIZATION_AUTO_TRIGGER_ITEMS:
        return None
    if knowledge_base.organization_last_run_at is not None:
        changed_count = await managed_knowledge_item_crud.count_organization_candidates(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            updated_after=knowledge_base.organization_last_run_at,
        )
        if changed_count < KNOWLEDGE_ORGANIZATION_AUTO_TRIGGER_ITEMS:
            return None

    try:
        job = await submit_knowledge_organization(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            source="auto",
            commit=False,
        )
    except KnowledgeOrganizationExecutionError as exc:
        if isinstance(exc.data, dict) and exc.data.get("status") == "organization_target_busy":
            if commit and db.in_transaction():
                await db.rollback()
            return None
        raise

    knowledge_base = await knowledge_base_crud.lock_owned_by_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    if knowledge_base is None or job.id is None:
        raise KnowledgeOrganizationExecutionError(
            message=ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
            code=409,
            status="organization_target_invalid",
            retryable=True,
        )
    knowledge_base.organization_last_job_id = job.id
    knowledge_base.organization_last_run_at = now
    knowledge_base.organization_error = None
    if commit:
        await db.commit()
        await db.refresh(job)
    else:
        await db.flush()
    return job


async def get_knowledge_organization_job(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
) -> dict[str, Any]:
    job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    if job is None or job.operation not in {
        KnowledgeJobOperation.AUTO_ORGANIZE,
        KnowledgeJobOperation.MANUAL_ORGANIZE,
    }:
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
    children = await knowledge_job_crud.list_children(db, uid=uid, parent_job_id=job_id)
    return await _organization_job_view(db, job, children)


async def list_knowledge_organization_jobs(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    skip: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    jobs, total = await knowledge_job_crud.list_page_for_knowledge_base(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        skip=skip,
        limit=limit,
        operations=(KnowledgeJobOperation.AUTO_ORGANIZE, KnowledgeJobOperation.MANUAL_ORGANIZE),
        statuses=None,
    )
    views = []
    for job in jobs:
        children = await knowledge_job_crud.list_children(db, uid=uid, parent_job_id=job.id)
        views.append(await _organization_job_view(db, job, children))
    return {"items": views, "total": total, "skip": max(skip, 0), "limit": min(max(limit, 0), 100)}


async def cancel_knowledge_organization(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
    commit: bool = True,
) -> dict[str, Any]:
    job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    if job is None or job.operation not in {
        KnowledgeJobOperation.AUTO_ORGANIZE,
        KnowledgeJobOperation.MANUAL_ORGANIZE,
    }:
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
    cancellation = await knowledge_job_crud.request_cancel(db, uid=uid, job_id=job_id, commit=False)
    if cancellation.changed:
        await coordinate_organization_cancel_request(db, uid=uid, job_id=job_id)
        await coordinate_organization_terminal(db, uid=uid, job_id=job_id, error=None)
    if commit:
        await db.commit()
    else:
        await db.flush()
    current = cancellation.job or await knowledge_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    children = await knowledge_job_crud.list_children(db, uid=uid, parent_job_id=job_id)
    return {
        "accepted": cancellation.accepted,
        "changed": cancellation.changed,
        "job": await _organization_job_view(db, current, children) if current is not None else None,
    }


async def retry_knowledge_organization(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
    commit: bool = True,
) -> dict[str, Any]:
    old_job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    if (
        old_job is None
        or old_job.operation
        not in {
            KnowledgeJobOperation.AUTO_ORGANIZE,
            KnowledgeJobOperation.MANUAL_ORGANIZE,
        }
        or old_job.status
        not in {
            KnowledgeJobStatus.FAILED,
            KnowledgeJobStatus.CANCELLED,
        }
    ):
        raise KnowledgeOrganizationExecutionError(
            message=ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
            code=409,
            status="organization_retry_invalid",
            retryable=False,
        )
    payload = old_job.payload if isinstance(old_job.payload, dict) else {}
    old_scope = payload.get("knowledge_ids")
    source = "auto" if old_job.operation == KnowledgeJobOperation.AUTO_ORGANIZE else "manual"
    retried = await submit_knowledge_organization(
        db,
        uid=uid,
        knowledge_base_id=old_job.knowledge_base_id,
        knowledge_ids=old_scope,
        source=source,
        dedupe_key=f"knowledge-organization-retry:{job_id}:{uuid4().hex}",
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(retried)
    else:
        await db.flush()
    return {
        "accepted": True,
        "retry_scope": "new_snapshot",
        "job": await _organization_job_view(db, retried),
    }


__all__ = [
    "cancel_knowledge_organization",
    "execute_knowledge_organization",
    "get_knowledge_organization_job",
    "list_knowledge_organization_jobs",
    "retry_knowledge_organization",
    "submit_auto_knowledge_organization",
    "submit_knowledge_organization",
]
