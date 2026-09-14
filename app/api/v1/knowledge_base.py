import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.constants import (
    ERR_KB_CHUNK_OVERLAP_ERROR,
    ERR_KB_COLLECTION_CREATE_FAILED,
    ERR_KB_CREATE_FAILED_WITH_ROLLBACK,
    ERR_KB_DELETE_FAILED,
    ERR_KB_DOC_DELETE_FAILED,
    ERR_KB_DOC_NOT_FOUND,
    ERR_KB_DOC_SAVE_FAILED,
    ERR_KB_EMBEDDING_CONFIG_CHANGED,
    ERR_KB_EMBEDDING_PROBE_FAILED,
    ERR_KB_FILE_EMPTY,
    ERR_KB_FILE_ENCODING_ERROR,
    ERR_KB_MANAGED_DOCUMENT_IMPORT_FORBIDDEN,
    ERR_KB_NOT_FOUND,
    ERR_KB_VECTOR_WRITE_FAILED,
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED,
    ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND,
    ERR_PROFILE_NOT_FOUND,
    ERR_SESSION_NO_PERMISSION,
    MSG_GENERIC_SUCCESS,
    MSG_KB_CREATED,
    MSG_KB_DELETED,
    MSG_KB_DOC_CREATED,
    MSG_KB_DOC_DELETED,
    MSG_KB_EMBEDDING_MIGRATION_SUBMITTED,
    MSG_KB_UNNAMED_DOCUMENT,
    MSG_KB_UPDATED,
)
from app.core.crud.channel.channel import channel_crud
from app.core.crud.knowledge.base import (
    knowledge_base_crud,
    knowledge_base_document_crud,
    knowledge_base_profile_binding_crud,
)
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.crud.profile.profile import profile_crud
from app.core.embedding.common import EmbeddingRuntimeConfig, build_embedding_signature, detect_embedding_dimensions, load_embedding_runtime_config

# Re-use the refactored embedding and knowledge base query core functions
from app.core.embedding.knowledge_base import (
    embed_chunks_with_knowledge_base_config,
    query_knowledge_base,
)
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.knowledge.bindings import (
    get_user_knowledge_base_ids_for_profile,
    replace_user_knowledge_base_bindings,
)
from app.core.knowledge.deletion import delete_owned_knowledge_base
from app.core.knowledge.embedding_migration import submit_user_knowledge_base_embedding_migration
from app.core.knowledge.errors import ManagedKnowledgeConflictError, ManagedKnowledgeNotFoundError
from app.core.knowledge.managed import build_managed_knowledge_snapshot, managed_knowledge_service
from app.core.knowledge.migration import record_knowledge_base_migration_change
from app.core.knowledge.organization_run import (
    cancel_knowledge_organization,
    get_knowledge_organization_job,
    list_knowledge_organization_jobs,
    retry_knowledge_organization,
    submit_knowledge_organization,
)
from app.core.knowledge_jobs.manager import knowledge_job_manager
from app.core.security import get_current_user
from app.core.utils.text_splitter import TextSplitter
from app.models.channel import ModelUsage
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCreate,
    KnowledgeBaseDocumentContentResponse,
    KnowledgeBaseDocumentListResponse,
    KnowledgeBaseDocumentResponse,
    KnowledgeBaseEmbeddingMigrationRequest,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseListResponse,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseProfileBindingUpdate,
    KnowledgeBaseQueryTestRequest,
    KnowledgeBaseQueryTestResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseType,
    KnowledgeBaseUpdate,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationRequest,
    ManagedKnowledgeActorType,
    ManagedKnowledgeCreateRequest,
    ManagedKnowledgeDeleteRequest,
    ManagedKnowledgeItem,
    ManagedKnowledgeItemResponse,
    ManagedKnowledgeItemSummaryResponse,
    ManagedKnowledgeListResponse,
    ManagedKnowledgeMutationResponse,
    ManagedKnowledgeRetryRequest,
    ManagedKnowledgeRevisionResponse,
    ManagedKnowledgeSourceType,
    ManagedKnowledgeUpdateRequest,
)
from app.providers.database import get_db
from app.providers.vector import (
    async_create_collection,
    async_delete_collection,
    async_delete_collection_items,
    async_get_or_create_collection,
    async_upsert_collection_items,
)
from app.schemas.response import StandardResponse

router = APIRouter(prefix="/knowledge-base", tags=["KnowledgeBase"])


async def load_owned_knowledge_base(db: AsyncSession, kb_id: int, current_user: Any) -> KnowledgeBase:
    kb = await knowledge_base_crud.get(db, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=ERR_KB_NOT_FOUND)
    if kb.uid != getattr(current_user, "uid", None) and not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=403, detail=ERR_SESSION_NO_PERMISSION)
    return kb


async def load_owned_managed_knowledge_base(db: AsyncSession, kb_id: int, current_user: Any) -> KnowledgeBase:
    knowledge_base = await load_owned_knowledge_base(db, kb_id, current_user)
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED)
    return knowledge_base


def build_managed_knowledge_item_response(item, publication_job=None) -> ManagedKnowledgeItemResponse:
    payload = build_managed_knowledge_snapshot(item)
    payload["id"] = payload["knowledge_id"]
    content = payload.get("content") or ""
    payload["content_preview"] = content[:300]
    if publication_job is not None:
        payload["publication_job_id"] = publication_job.id
        payload["publication_job_status"] = getattr(publication_job.status, "value", publication_job.status)
        payload["publication_job_error"] = publication_job.error
    return ManagedKnowledgeItemResponse.model_validate(payload)


def build_managed_knowledge_item_summary(item, publication_job=None) -> ManagedKnowledgeItemSummaryResponse:
    response = build_managed_knowledge_item_response(item, publication_job)
    return ManagedKnowledgeItemSummaryResponse.model_validate(response.model_dump())


def build_managed_knowledge_mutation_response(result) -> ManagedKnowledgeMutationResponse:
    status = getattr(result.status, "value", str(result.status))
    item = build_managed_knowledge_item_response(result.item, result.job) if result.item is not None else None
    job_id = getattr(result.job, "id", None) if result.job is not None else None
    return ManagedKnowledgeMutationResponse(status=status, item=item, job_id=job_id)


def managed_user_dedupe_key(prefix: str, requested: str | None) -> str:
    return requested or f"{prefix}:{uuid.uuid4().hex}"


async def load_managed_knowledge_publication_jobs(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    items: list[ManagedKnowledgeItem],
) -> dict[int, KnowledgeJob]:
    targets = [(item.id, item.version) for item in items if item.id is not None]
    latest_by_target = await knowledge_job_crud.latest_publication_jobs_for_targets(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=knowledge_base.id,
        targets=targets,
    )
    unresolved_source_job_ids = [item.source_job_id for item in items if item.id is not None and (item.id, item.version) not in latest_by_target and item.source_job_id is not None]
    source_jobs = await knowledge_job_crud.get_by_ids(
        db,
        uid=knowledge_base.uid,
        job_ids=unresolved_source_job_ids,
    )
    source_jobs_by_id = {job.id: job for job in source_jobs if job.id is not None}
    resolved: dict[int, KnowledgeJob] = {}
    for item in items:
        if item.id is None:
            continue
        publication_job = latest_by_target.get((item.id, item.version))
        if publication_job is None and item.source_job_id is not None:
            candidate = source_jobs_by_id.get(item.source_job_id)
            if candidate is not None and candidate.knowledge_base_id == knowledge_base.id and candidate.operation == KnowledgeJobOperation.MANAGED_CREATE and candidate.knowledge_id is None and candidate.status in {KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED}:
                publication_job = candidate
        if publication_job is not None:
            resolved[item.id] = publication_job
    return resolved


async def get_knowledge_base_profile_ids(db: AsyncSession, kb_id: int, uid: str) -> list[int]:
    return await knowledge_base_profile_binding_crud.list_profile_ids_by_knowledge_base(
        db,
        uid=uid,
        knowledge_base_id=kb_id,
    )


async def build_knowledge_base_response(db: AsyncSession, kb: KnowledgeBase) -> KnowledgeBaseResponse:
    response = KnowledgeBaseResponse.model_validate(kb)
    if kb.id is not None:
        response.profile_ids = await get_knowledge_base_profile_ids(db, kb.id, kb.uid)
    if response.migration_status in {KnowledgeBaseMigrationStatus.FAILED, KnowledgeBaseMigrationStatus.CANCELLED} and response.migration_job_id is not None and response.target_embedding_channel_id is None:
        job = await knowledge_job_crud.get_by_id(db, uid=kb.uid, job_id=response.migration_job_id)
        if job is not None and job.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION and isinstance(job.payload, dict):
            target = job.payload.get("target")
            if isinstance(target, dict):
                channel_id = target.get("channel_id")
                model_id = target.get("model_id")
                dimensions = target.get("dimensions")
                signature = target.get("signature")
                revision = target.get("revision")
                if isinstance(channel_id, int) and not isinstance(channel_id, bool) and channel_id > 0:
                    response.target_embedding_channel_id = channel_id
                if isinstance(model_id, str) and model_id:
                    response.target_embedding_model_id = model_id
                if isinstance(dimensions, int) and not isinstance(dimensions, bool) and dimensions > 0:
                    response.target_embedding_dimensions = dimensions
                if isinstance(signature, str) and signature:
                    response.target_embedding_signature = signature
                if isinstance(revision, int) and not isinstance(revision, bool) and revision > 0:
                    response.target_embedding_revision = revision
    return response


async def load_embedding_model(
    db: AsyncSession,
    channel_id: int,
    model_id: str,
    lock_for_reference_write: bool = False,
) -> tuple[EmbeddingRuntimeConfig, dict[str, Any]]:
    config = await load_embedding_runtime_config(
        db,
        channel_id,
        model_id,
        channel_not_found_status_code=404,
        model_not_found_status_code=404,
        lock_for_reference_write=lock_for_reference_write,
    )
    return config, {"model_id": config.model_id, "embedding_dimensions": config.declared_dimensions}


async def embed_document_with_stable_knowledge_base_config(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    chunks: list[str],
    batch_size: int,
) -> tuple[KnowledgeBase, list[list[float]]]:
    candidate = knowledge_base
    for _attempt in range(2):
        expected_embedding = resolve_active_knowledge_base_embedding(candidate)
        embeddings = await embed_chunks_with_knowledge_base_config(
            db,
            candidate,
            chunks,
            batch_size,
            release_connection=True,
        )
        locked = await knowledge_base_crud.lock_owned_by_id(
            db,
            uid=knowledge_base.uid,
            knowledge_base_id=knowledge_base.id,
        )
        if locked is None:
            raise HTTPException(status_code=404, detail=ERR_KB_NOT_FOUND)
        if resolve_active_knowledge_base_embedding(locked) == expected_embedding:
            return locked, embeddings
        await db.rollback()
        candidate = await knowledge_base_crud.get(db, knowledge_base.id)
        if candidate is None:
            raise HTTPException(status_code=404, detail=ERR_KB_NOT_FOUND)
    raise HTTPException(status_code=409, detail=t(ERR_KB_EMBEDDING_CONFIG_CHANGED))


async def list_embedding_model_options(db: AsyncSession) -> list[dict[str, Any]]:
    options = []
    for channel in await channel_crud.list_active(db):
        if not channel.base_url:
            continue
        for item in channel.model_ids or []:
            if item.get("usage") == ModelUsage.EMBEDDING and item.get("is_enabled", True):
                options.append(
                    {
                        "channel_id": channel.id,
                        "channel_name": channel.name,
                        "model_id": item.get("model_id"),
                        "embedding_dimensions": item.get("embedding_dimensions"),
                    }
                )
    return options


@router.post("/create", response_model=StandardResponse[KnowledgeBaseResponse])
async def create_knowledge_base(
    kb_in: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """创建知识库"""

    embedding_config, embedding_model = await load_embedding_model(db, kb_in.embedding_channel_id, kb_in.embedding_model_id)
    embedding_dimensions = embedding_model.get("embedding_dimensions")
    await db.commit()
    if embedding_dimensions is None:
        try:
            embedding_dimensions = await detect_embedding_dimensions(embedding_config)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=t(ERR_KB_EMBEDDING_PROBE_FAILED)) from exc

    # 生成一个唯一的 collection_name
    collection_name = f"kb_{uuid.uuid4().hex}"

    # 在 ChromaDB 中创建 collection
    try:
        await async_create_collection(collection_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=t(ERR_KB_COLLECTION_CREATE_FAILED, message=str(e)))

    try:
        _channel, embedding_model = await load_embedding_model(
            db,
            kb_in.embedding_channel_id,
            kb_in.embedding_model_id,
            lock_for_reference_write=True,
        )
        locked_dimensions = embedding_model.get("embedding_dimensions")
        if locked_dimensions is not None and locked_dimensions != embedding_dimensions:
            raise HTTPException(status_code=409, detail=t(ERR_KB_EMBEDDING_CONFIG_CHANGED))
        if locked_dimensions is not None:
            embedding_dimensions = locked_dimensions
        active_embedding_signature = (
            build_embedding_signature(
                kb_in.embedding_channel_id,
                kb_in.embedding_model_id,
                embedding_dimensions,
            )
            if isinstance(embedding_dimensions, int) and not isinstance(embedding_dimensions, bool) and embedding_dimensions > 0
            else None
        )

        db_kb = await knowledge_base_crud.create(
            db,
            obj_in={
                "uid": current_user.uid,
                "name": kb_in.name,
                "description": kb_in.description,
                "embedding_channel_id": kb_in.embedding_channel_id,
                "embedding_model_id": kb_in.embedding_model_id,
                "embedding_dimensions": embedding_dimensions,
                "collection_name": collection_name,
                "knowledge_base_type": KnowledgeBaseType.USER,
                "managed_profile_id": None,
                "active_embedding_channel_id": kb_in.embedding_channel_id,
                "active_embedding_model_id": kb_in.embedding_model_id,
                "active_embedding_dimensions": embedding_dimensions,
                "active_embedding_signature": active_embedding_signature,
                "active_embedding_revision": 1,
                "active_collection_name": collection_name,
                "index_revision": 1,
                "index_status": KnowledgeBaseIndexStatus.READY,
            },
            commit=False,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        try:
            await async_delete_collection(collection_name)
        except Exception:
            pass
        raise
    except Exception as e:
        await db.rollback()
        try:
            await async_delete_collection(collection_name)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=t(ERR_KB_CREATE_FAILED_WITH_ROLLBACK, message=str(e)))

    return StandardResponse.success(data=await build_knowledge_base_response(db, db_kb), message=MSG_KB_CREATED)


@router.get("/list", response_model=StandardResponse[KnowledgeBaseListResponse])
async def list_knowledge_bases(
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """获取知识库列表及可用配置"""
    skip = (page - 1) * size
    kbs, total = await knowledge_base_crud.list_page(
        db,
        uid=None if getattr(current_user, "is_superuser", False) else current_user.uid,
        skip=skip,
        limit=size,
    )

    knowledge_base_items = []
    for knowledge_base in kbs:
        knowledge_base_items.append(await build_knowledge_base_response(db, knowledge_base))

    data = KnowledgeBaseListResponse(
        items=knowledge_base_items,
        total=total,
        embedding_models=await list_embedding_model_options(db),
    )

    return StandardResponse.success(data=data)


@router.post("/embedding-migration", response_model=StandardResponse[KnowledgeBaseResponse])
async def submit_knowledge_base_embedding_migration(
    kb_id: int,
    migration_in: KnowledgeBaseEmbeddingMigrationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    kb = await load_owned_knowledge_base(db, kb_id, current_user)
    await submit_user_knowledge_base_embedding_migration(
        db,
        uid=kb.uid,
        knowledge_base_id=kb_id,
        target_channel_id=migration_in.embedding_channel_id,
        target_model_id=migration_in.embedding_model_id,
    )
    refreshed = await knowledge_base_crud.get(db, kb_id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail=ERR_KB_NOT_FOUND)
    return StandardResponse.success(
        data=await build_knowledge_base_response(db, refreshed),
        message=MSG_KB_EMBEDDING_MIGRATION_SUBMITTED,
    )


@router.post("/organization", response_model=StandardResponse[dict[str, Any]])
async def submit_knowledge_base_organization(
    kb_id: int,
    organization_in: KnowledgeOrganizationRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    job = await submit_knowledge_organization(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        knowledge_ids=organization_in.knowledge_ids if organization_in is not None else None,
        dedupe_key=organization_in.dedupe_key if organization_in is not None else None,
    )
    return StandardResponse.success(
        data=await get_knowledge_organization_job(db, uid=knowledge_base.uid, job_id=job.id),
        message=MSG_GENERIC_SUCCESS,
    )


@router.get("/organization/jobs", response_model=StandardResponse[dict[str, Any]])
async def list_knowledge_base_organization_jobs(
    kb_id: int,
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    result = await list_knowledge_organization_jobs(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        skip=max(page - 1, 0) * min(max(size, 1), 100),
        limit=min(max(size, 1), 100),
    )
    return StandardResponse.success(data=result, message=MSG_GENERIC_SUCCESS)


@router.get("/organization/{job_id}", response_model=StandardResponse[dict[str, Any]])
async def get_knowledge_base_organization_job(
    kb_id: int,
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    result = await get_knowledge_organization_job(db, uid=knowledge_base.uid, job_id=job_id)
    if result.get("knowledge_base_id") != kb_id:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND)
    return StandardResponse.success(data=result, message=MSG_GENERIC_SUCCESS)


@router.post("/organization/{job_id}/cancel", response_model=StandardResponse[dict[str, Any]])
async def cancel_knowledge_base_organization_job(
    kb_id: int,
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    old_job = await get_knowledge_organization_job(db, uid=knowledge_base.uid, job_id=job_id)
    if old_job.get("knowledge_base_id") != knowledge_base.id:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND)
    result = await cancel_knowledge_organization(db, uid=knowledge_base.uid, job_id=job_id)
    return StandardResponse.success(data=result, message=MSG_GENERIC_SUCCESS)


@router.post("/organization/{job_id}/retry", response_model=StandardResponse[dict[str, Any]])
async def retry_knowledge_base_organization_job(
    kb_id: int,
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    old_job = await get_knowledge_organization_job(db, uid=knowledge_base.uid, job_id=job_id)
    if old_job.get("knowledge_base_id") != kb_id:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND)
    result = await retry_knowledge_organization(db, uid=knowledge_base.uid, job_id=job_id)
    return StandardResponse.success(data=result, message=MSG_GENERIC_SUCCESS)


@router.post("/update", response_model=StandardResponse[KnowledgeBaseResponse])
async def update_knowledge_base(
    kb_id: int,
    kb_in: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """修改知识库"""
    kb = await load_owned_knowledge_base(db, kb_id, current_user)

    kb = await knowledge_base_crud.update(
        db,
        db_obj=kb,
        obj_in={"name": kb_in.name, "description": kb_in.description},
    )
    return StandardResponse.success(data=await build_knowledge_base_response(db, kb), message=MSG_KB_UPDATED)


@router.get("/managed-items/list", response_model=StandardResponse[ManagedKnowledgeListResponse])
async def list_managed_knowledge_items(
    kb_id: int,
    page: int = 1,
    size: int = 20,
    query: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    page_size = min(max(size, 1), 100)
    skip = max(page - 1, 0) * page_size
    items, total = await managed_knowledge_item_crud.list_page(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        skip=skip,
        limit=page_size,
        query=query,
    )
    publication_jobs = await load_managed_knowledge_publication_jobs(
        db,
        knowledge_base=knowledge_base,
        items=items,
    )
    return StandardResponse.success(
        data=ManagedKnowledgeListResponse(
            items=[build_managed_knowledge_item_summary(item, publication_jobs.get(item.id)) for item in items],
            total=total,
        )
    )


@router.get("/managed-items/get", response_model=StandardResponse[ManagedKnowledgeItemResponse])
async def get_managed_knowledge_item(
    kb_id: int,
    knowledge_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    item = await managed_knowledge_item_crud.get_by_id(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        knowledge_id=knowledge_id,
    )
    if item is None or item.deleted_at is not None:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND)
    publication_jobs = await load_managed_knowledge_publication_jobs(
        db,
        knowledge_base=knowledge_base,
        items=[item],
    )
    return StandardResponse.success(data=build_managed_knowledge_item_response(item, publication_jobs.get(item.id)))


@router.post("/managed-items/create", response_model=StandardResponse[ManagedKnowledgeMutationResponse])
async def create_managed_knowledge_item(
    kb_id: int,
    item_in: ManagedKnowledgeCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    result = await knowledge_job_manager.submit_create(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        knowledge_key=item_in.knowledge_key,
        content=item_in.content,
        source_type=ManagedKnowledgeSourceType.USER_API,
        actor=ManagedKnowledgeActorType.USER,
        dedupe_key=managed_user_dedupe_key("managed-user-create", item_in.dedupe_key),
        llm_maintainable=item_in.llm_maintainable,
        source_profile_id=knowledge_base.managed_profile_id,
    )
    return StandardResponse.success(data=build_managed_knowledge_mutation_response(result))


@router.post("/managed-items/update", response_model=StandardResponse[ManagedKnowledgeMutationResponse])
async def update_managed_knowledge_item(
    kb_id: int,
    knowledge_id: int,
    item_in: ManagedKnowledgeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    result = await knowledge_job_manager.submit_update(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        knowledge_id=knowledge_id,
        expected_version=item_in.expected_version,
        knowledge_key=item_in.knowledge_key,
        content=item_in.content,
        source_type=ManagedKnowledgeSourceType.USER_API,
        actor=ManagedKnowledgeActorType.USER,
        dedupe_key=managed_user_dedupe_key("managed-user-update", item_in.dedupe_key),
        llm_maintainable=item_in.llm_maintainable,
        source_profile_id=knowledge_base.managed_profile_id,
    )
    return StandardResponse.success(data=build_managed_knowledge_mutation_response(result))


@router.post("/managed-items/delete", response_model=StandardResponse[ManagedKnowledgeMutationResponse])
async def delete_managed_knowledge_item(
    kb_id: int,
    knowledge_id: int,
    item_in: ManagedKnowledgeDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    result = await knowledge_job_manager.submit_delete(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        knowledge_id=knowledge_id,
        expected_version=item_in.expected_version,
        source_type=ManagedKnowledgeSourceType.USER_API,
        actor=ManagedKnowledgeActorType.USER,
        dedupe_key=managed_user_dedupe_key("managed-user-delete", item_in.dedupe_key),
        source_profile_id=knowledge_base.managed_profile_id,
    )
    return StandardResponse.success(data=build_managed_knowledge_mutation_response(result))


@router.post("/managed-items/retry", response_model=StandardResponse[ManagedKnowledgeMutationResponse])
async def retry_managed_knowledge_item(
    kb_id: int,
    knowledge_id: int,
    item_in: ManagedKnowledgeRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    result = await knowledge_job_manager.retry_failed_publication(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        knowledge_id=knowledge_id,
        expected_version=item_in.expected_version,
        failed_job_id=item_in.failed_job_id,
        dedupe_key=managed_user_dedupe_key("managed-user-retry", item_in.dedupe_key),
        source_profile_id=knowledge_base.managed_profile_id,
    )
    return StandardResponse.success(data=build_managed_knowledge_mutation_response(result))


@router.get("/managed-items/history", response_model=StandardResponse[list[ManagedKnowledgeRevisionResponse]])
async def get_managed_knowledge_history(
    kb_id: int,
    knowledge_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    knowledge_base = await load_owned_managed_knowledge_base(db, kb_id, current_user)
    history = await managed_knowledge_service.list_history(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=kb_id,
        knowledge_id=knowledge_id,
        limit=100,
    )
    return StandardResponse.success(data=[ManagedKnowledgeRevisionResponse.model_validate(revision) for revision in history])


@router.get("/profile-bindings", response_model=StandardResponse[list[int]])
async def get_profile_knowledge_base_bindings(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    profile = await profile_crud.get(db, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=ERR_PROFILE_NOT_FOUND)
    if profile.uid != getattr(current_user, "uid", None) and not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=403, detail=ERR_SESSION_NO_PERMISSION)

    return StandardResponse.success(
        data=await get_user_knowledge_base_ids_for_profile(
            db,
            uid=profile.uid,
            profile_id=profile_id,
        )
    )


@router.post("/profile-bindings", response_model=StandardResponse[list[int]])
async def update_profile_knowledge_base_bindings(
    profile_id: int,
    binding_in: KnowledgeBaseProfileBindingUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    snapshot = await profile_crud.get(db, profile_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=ERR_PROFILE_NOT_FOUND)
    profile = await profile_crud.lock_for_runtime_use(
        db,
        profile_id=profile_id,
        uid=snapshot.uid,
    )
    if not profile:
        raise HTTPException(status_code=404, detail=ERR_PROFILE_NOT_FOUND)
    if profile.uid != getattr(current_user, "uid", None) and not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=403, detail=ERR_SESSION_NO_PERMISSION)

    normalized_kb_ids = await replace_user_knowledge_base_bindings(
        db,
        uid=profile.uid,
        profile_id=profile_id,
        knowledge_base_ids=binding_in.knowledge_base_ids,
    )
    await db.commit()
    return StandardResponse.success(data=normalized_kb_ids or [])


@router.post("/query-test", response_model=StandardResponse[KnowledgeBaseQueryTestResponse])
async def query_test_knowledge_base(
    kb_id: int,
    query_in: KnowledgeBaseQueryTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    kb = await load_owned_knowledge_base(db, kb_id, current_user)
    profile = await profile_crud.get_default(db, uid=kb.uid)
    if not profile:
        raise HTTPException(status_code=404, detail=ERR_PROFILE_NOT_FOUND)

    response_data = await query_knowledge_base(db, profile, kb_id, query_in.query, query_in.top_k, expose_rerank_error=True, require_binding=False)
    return StandardResponse.success(data=response_data)


@router.post("/delete", response_model=StandardResponse[bool])
async def delete_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """删除知识库"""
    try:
        await delete_owned_knowledge_base(
            db,
            knowledge_base_id=kb_id,
            requester_uid=getattr(current_user, "uid", ""),
            is_superuser=bool(getattr(current_user, "is_superuser", False)),
        )
    except BaseBusinessException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=t(ERR_KB_DELETE_FAILED, message=str(e)))

    return StandardResponse.success(data=True, message=MSG_KB_DELETED)


@router.post("/documents/import", response_model=StandardResponse[KnowledgeBaseDocumentResponse])
async def import_document(
    kb_id: int,
    file: UploadFile = File(...),
    chunk_size: int = Form(1000, ge=100, le=20000),
    chunk_overlap: int = Form(100, ge=0, le=5000),
    batch_size: int = Form(16, ge=1, le=256),
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    kb = await load_owned_knowledge_base(db, kb_id, current_user)
    if kb.knowledge_base_type != KnowledgeBaseType.USER:
        raise HTTPException(status_code=409, detail=t(ERR_KB_MANAGED_DOCUMENT_IMPORT_FORBIDDEN))
    if chunk_overlap >= chunk_size:
        raise HTTPException(status_code=400, detail=ERR_KB_CHUNK_OVERLAP_ERROR)

    raw_content = await file.read()
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw_content.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail=ERR_KB_FILE_ENCODING_ERROR)

    chunks = TextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap).split(content)
    if not chunks:
        raise HTTPException(status_code=400, detail=ERR_KB_FILE_EMPTY)

    locked_kb, embeddings = await embed_document_with_stable_knowledge_base_config(
        db,
        knowledge_base=kb,
        chunks=chunks,
        batch_size=batch_size,
    )
    active_embedding = resolve_active_knowledge_base_embedding(locked_kb)
    document_uuid = uuid.uuid4().hex
    document_filename = file.filename or t(MSG_KB_UNNAMED_DOCUMENT)
    chunk_ids = []
    metadatas = []
    for chunk_index in range(len(chunks)):
        chunk_ids.append(f"kb_{kb.id}_doc_{document_uuid}_chunk_{chunk_index}")
        metadatas.append(
            {
                "knowledge_base_id": kb.id,
                "document_uuid": document_uuid,
                "filename": document_filename,
                "chunk_index": chunk_index,
            }
        )

    try:
        await async_get_or_create_collection(active_embedding.collection_name)
        await async_upsert_collection_items(
            active_embedding.collection_name,
            chunk_ids,
            chunks,
            embeddings,
            metadatas,
            batch_size=batch_size,
        )
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=t(ERR_KB_VECTOR_WRITE_FAILED, message=str(e)))

    try:
        db_document = await knowledge_base_document_crud.create(
            db,
            values={
                "knowledge_base_id": locked_kb.id,
                "filename": document_filename,
                "content": content,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "batch_size": batch_size,
                "chunk_count": len(chunks),
                "chunk_ids": chunk_ids,
                "metadata_": {
                    "document_uuid": document_uuid,
                    "content_type": file.content_type or "",
                },
            },
            commit=False,
        )
        await record_knowledge_base_migration_change(
            db,
            knowledge_base=locked_kb,
            source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
            source_id=db_document.id,
            action=KnowledgeBaseMigrationDeltaAction.UPSERT,
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        try:
            await async_delete_collection_items(active_embedding.collection_name, chunk_ids)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=t(ERR_KB_DOC_SAVE_FAILED, message=str(e)))

    return StandardResponse.success(data=KnowledgeBaseDocumentResponse.model_validate(db_document), message=MSG_KB_DOC_CREATED)


@router.get("/documents/list", response_model=StandardResponse[KnowledgeBaseDocumentListResponse])
async def list_documents(
    kb_id: int,
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    await load_owned_knowledge_base(db, kb_id, current_user)

    skip = (page - 1) * size
    documents, total = await knowledge_base_document_crud.list_page(
        db,
        knowledge_base_id=kb_id,
        skip=skip,
        limit=size,
    )

    document_items = []
    for document in documents:
        document_items.append(KnowledgeBaseDocumentResponse.model_validate(document))

    return StandardResponse.success(
        data=KnowledgeBaseDocumentListResponse(
            items=document_items,
            total=total,
        )
    )


@router.get("/documents/get", response_model=StandardResponse[KnowledgeBaseDocumentContentResponse])
async def get_document_content(
    kb_id: int,
    document_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    await load_owned_knowledge_base(db, kb_id, current_user)
    document = await knowledge_base_document_crud.get_by_knowledge_base(
        db,
        knowledge_base_id=kb_id,
        document_id=document_id,
    )
    if not document:
        raise HTTPException(status_code=404, detail=ERR_KB_DOC_NOT_FOUND)
    return StandardResponse.success(data=KnowledgeBaseDocumentContentResponse.model_validate(document))


@router.post("/documents/delete", response_model=StandardResponse[bool])
async def delete_document(
    kb_id: int,
    document_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    kb = await load_owned_knowledge_base(db, kb_id, current_user)
    locked_kb = await knowledge_base_crud.lock_owned_by_id(
        db,
        uid=kb.uid,
        knowledge_base_id=kb_id,
    )
    if locked_kb is None:
        raise HTTPException(status_code=404, detail=ERR_KB_NOT_FOUND)
    document = await knowledge_base_document_crud.get_by_knowledge_base(
        db,
        knowledge_base_id=kb_id,
        document_id=document_id,
    )
    if not document:
        raise HTTPException(status_code=404, detail=ERR_KB_DOC_NOT_FOUND)

    active_embedding = resolve_active_knowledge_base_embedding(locked_kb)
    try:
        await knowledge_base_document_crud.delete(
            db,
            document=document,
            commit=False,
        )
        await record_knowledge_base_migration_change(
            db,
            knowledge_base=locked_kb,
            source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
            source_id=document.id,
            action=KnowledgeBaseMigrationDeltaAction.DELETE,
        )
        await async_delete_collection_items(
            active_embedding.collection_name,
            document.chunk_ids or [],
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=t(ERR_KB_DOC_DELETE_FAILED, message=str(e)))
    return StandardResponse.success(data=True, message=MSG_KB_DOC_DELETED)
