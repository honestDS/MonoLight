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
    ERR_KB_FILE_EMPTY,
    ERR_KB_FILE_ENCODING_ERROR,
    ERR_KB_MANAGED_DOCUMENT_IMPORT_FORBIDDEN,
    ERR_KB_NOT_FOUND,
    ERR_KB_VECTOR_WRITE_FAILED,
    ERR_PROFILE_NOT_FOUND,
    ERR_SESSION_NO_PERMISSION,
    MSG_KB_CREATED,
    MSG_KB_DELETED,
    MSG_KB_DOC_CREATED,
    MSG_KB_DOC_DELETED,
    MSG_KB_UNNAMED_DOCUMENT,
    MSG_KB_UPDATED,
)
from app.core.crud.channel import channel_crud
from app.core.crud.knowledge_base import (
    knowledge_base_crud,
    knowledge_base_document_crud,
    knowledge_base_profile_binding_crud,
)
from app.core.crud.profile import profile_crud
from app.core.embedding.common import EmbeddingRuntimeConfig, load_embedding_runtime_config

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
from app.core.knowledge.migration import record_knowledge_base_migration_change
from app.core.security import get_current_user
from app.core.utils.text_splitter import TextSplitter
from app.models.channel import ModelUsage
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCreate,
    KnowledgeBaseDocumentContentResponse,
    KnowledgeBaseDocumentListResponse,
    KnowledgeBaseDocumentResponse,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseListResponse,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseProfileBindingUpdate,
    KnowledgeBaseQueryTestRequest,
    KnowledgeBaseQueryTestResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseType,
    KnowledgeBaseUpdate,
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
    raise HTTPException(status_code=409, detail=ERR_KB_EMBEDDING_CONFIG_CHANGED)


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

    _channel, embedding_model = await load_embedding_model(db, kb_in.embedding_channel_id, kb_in.embedding_model_id)
    embedding_dimensions = embedding_model.get("embedding_dimensions")
    await db.commit()

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
        embedding_dimensions = embedding_model.get("embedding_dimensions")

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
