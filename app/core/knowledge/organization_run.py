from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    ERR_KB_NOT_FOUND,
    ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID,
    ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID,
    KNOWLEDGE_ORGANIZATION_JOB_LEASE_RENEW_INTERVAL_SECONDS,
    KNOWLEDGE_ORGANIZATION_JOB_LEASE_SECONDS,
    LOG_KNOWLEDGE_ORGANIZATION_CONFIG_INVALID,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import knowledge_job_crud
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
from app.core.log import get_logger
from app.models.knowledge_base import KnowledgeJobOperation, KnowledgeOrganizationSnapshot, KnowledgeOrganizationStage

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
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            organization_job_id=organization_job_id,
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
    async with session_factory() as db:
        if error is None:
            return await knowledge_job_crud.mark_succeeded(
                db,
                uid=uid,
                job_id=job_id,
                owner=worker_id,
                result={
                    "snapshot_id": result.snapshot_id if result is not None else None,
                    "stage_count": result.stage_count if result is not None else 0,
                    "model_id": result.model_id if result is not None else None,
                },
            )
        return await knowledge_job_crud.mark_failed(
            db,
            uid=uid,
            job_id=job_id,
            owner=worker_id,
            error=f"{type(error).__name__}: {error}",
        )


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


__all__ = [
    "execute_knowledge_organization",
]
