import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import constants

# Re-use the refactored embedding and knowledge base query core functions
from app.core.embedding.knowledge_base import (
    embed_chunks,
    get_profile_embedding_config,
    is_embedding_profile_available,
    query_knowledge_base,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.core.utils.text_splitter import TextSplitter
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCreate,
    KnowledgeBaseDocument,
    KnowledgeBaseDocumentContentResponse,
    KnowledgeBaseDocumentListResponse,
    KnowledgeBaseDocumentResponse,
    KnowledgeBaseListResponse,
    KnowledgeBaseProfileOption,
    KnowledgeBaseQueryTestRequest,
    KnowledgeBaseQueryTestResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
)
from app.models.profile import Profile
from app.providers.database import get_db
from app.providers.vector import create_collection, delete_collection, delete_collection_items, get_or_create_collection
from app.schemas.response import StandardResponse

router = APIRouter(prefix="/knowledge-base", tags=["KnowledgeBase"])


@router.post("/create", response_model=StandardResponse[KnowledgeBaseResponse])
async def create_knowledge_base(
    kb_in: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """创建知识库"""

    # 检查 profile 是否存在
    profile = await db.get(Profile, kb_in.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=constants.ERR_PROFILE_NOT_FOUND)
    await get_profile_embedding_config(db, profile, call_context="knowledge_base_create_embedding_check")

    # 生成一个唯一的 collection_name
    collection_name = f"kb_{uuid.uuid4().hex}"

    # 在 ChromaDB 中创建 collection
    try:
        create_collection(collection_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_COLLECTION_CREATE_FAILED, message=str(e)))

    # 存入关系型数据库
    db_kb = KnowledgeBase(
        name=kb_in.name,
        description=kb_in.description,
        profile_id=kb_in.profile_id,
        collection_name=collection_name,
    )
    db.add(db_kb)
    try:
        await db.commit()
        await db.refresh(db_kb)
    except Exception as e:
        await db.rollback()
        try:
            delete_collection(collection_name)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_CREATE_FAILED_WITH_ROLLBACK, message=str(e)))

    return StandardResponse.success(data=KnowledgeBaseResponse.model_validate(db_kb), message=constants.MSG_KB_CREATED)


@router.get("/list", response_model=StandardResponse[KnowledgeBaseListResponse])
async def list_knowledge_bases(
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """获取知识库列表及可用配置"""
    # 1. 获取知识库列表 (带分页)
    skip = (page - 1) * size
    query = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc()).offset(skip).limit(size)
    result_kb = await db.execute(query)
    kbs = result_kb.scalars().all()

    # 获取总数
    total_result = await db.execute(select(func.count()).select_from(KnowledgeBase))
    total = total_result.scalar() or 0

    result_profiles = await db.execute(select(Profile))
    profiles = result_profiles.scalars().all()
    profile_options = []
    available_profiles = []
    for profile in profiles:
        profile_option = KnowledgeBaseProfileOption.model_validate(profile)
        profile_options.append(profile_option)
        if await is_embedding_profile_available(db, profile):
            available_profiles.append(profile_option)

    knowledge_base_items = []
    for knowledge_base in kbs:
        knowledge_base_items.append(KnowledgeBaseResponse.model_validate(knowledge_base))

    data = KnowledgeBaseListResponse(
        items=knowledge_base_items,
        total=total,
        profiles=profile_options,
        available_profiles=available_profiles,
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
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)

    kb.name = kb_in.name
    kb.description = kb_in.description

    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return StandardResponse.success(data=KnowledgeBaseResponse.model_validate(kb), message=constants.MSG_KB_UPDATED)


@router.post("/query-test", response_model=StandardResponse[KnowledgeBaseQueryTestResponse])
async def query_test_knowledge_base(
    kb_id: int,
    query_in: KnowledgeBaseQueryTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)

    profile = await db.get(Profile, kb.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_PROFILE_NOT_FOUND)

    response_data = await query_knowledge_base(db, profile, kb_id, query_in.query, query_in.top_k, expose_rerank_error=True)
    return StandardResponse.success(data=response_data)


@router.post("/delete", response_model=StandardResponse[bool])
async def delete_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """删除知识库"""

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)

    docs_result = await db.execute(select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == kb_id))
    for document in docs_result.scalars().all():
        await db.delete(document)
    await db.delete(kb)
    try:
        await db.flush()
        delete_collection(kb.collection_name)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_DELETE_FAILED, message=str(e)))

    return StandardResponse.success(data=True, message=constants.MSG_KB_DELETED)


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

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)
    if chunk_overlap >= chunk_size:
        raise HTTPException(status_code=400, detail=constants.ERR_KB_CHUNK_OVERLAP_ERROR)

    profile = await db.get(Profile, kb.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_PROFILE_NOT_FOUND)

    raw_content = await file.read()
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw_content.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail=constants.ERR_KB_FILE_ENCODING_ERROR)

    chunks = TextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap).split(content)
    if not chunks:
        raise HTTPException(status_code=400, detail=constants.ERR_KB_FILE_EMPTY)

    embeddings = await embed_chunks(db, profile, chunks, batch_size, call_context="knowledge_base_document_import_embedding")
    document_uuid = uuid.uuid4().hex
    chunk_ids = []
    metadatas = []
    for chunk_index in range(len(chunks)):
        chunk_ids.append(f"kb_{kb.id}_doc_{document_uuid}_chunk_{chunk_index}")
        metadatas.append(
            {
                "knowledge_base_id": kb.id,
                "document_uuid": document_uuid,
                "filename": file.filename or "未命名文档",
                "chunk_index": chunk_index,
            }
        )

    try:
        collection = get_or_create_collection(kb.collection_name)
        collection.add(ids=chunk_ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)
    except Exception as e:
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_VECTOR_WRITE_FAILED, message=str(e)))

    db_document = KnowledgeBaseDocument(
        knowledge_base_id=kb.id,
        filename=file.filename or "未命名文档",
        content=content,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        batch_size=batch_size,
        chunk_count=len(chunks),
        chunk_ids=chunk_ids,
        metadata_={"document_uuid": document_uuid, "content_type": file.content_type or ""},
    )
    db.add(db_document)
    try:
        await db.commit()
        await db.refresh(db_document)
    except Exception as e:
        await db.rollback()
        try:
            delete_collection_items(kb.collection_name, chunk_ids)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_DOC_SAVE_FAILED, message=str(e)))

    return StandardResponse.success(data=KnowledgeBaseDocumentResponse.model_validate(db_document), message=constants.MSG_KB_DOC_CREATED)


@router.get("/documents/list", response_model=StandardResponse[KnowledgeBaseDocumentListResponse])
async def list_documents(
    kb_id: int,
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)

    skip = (page - 1) * size
    result = await db.execute(select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == kb_id).order_by(KnowledgeBaseDocument.created_at.desc()).offset(skip).limit(size))
    documents = result.scalars().all()
    total_result = await db.execute(select(func.count()).select_from(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == kb_id))
    total = total_result.scalar() or 0

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

    document = await db.get(KnowledgeBaseDocument, document_id)
    if not document or document.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_DOC_NOT_FOUND)
    return StandardResponse.success(data=KnowledgeBaseDocumentContentResponse.model_validate(document))


@router.post("/documents/delete", response_model=StandardResponse[bool])
async def delete_document(
    kb_id: int,
    document_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)
    document = await db.get(KnowledgeBaseDocument, document_id)
    if not document or document.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_DOC_NOT_FOUND)

    await db.delete(document)
    try:
        await db.flush()
        delete_collection_items(kb.collection_name, document.chunk_ids or [])
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_DOC_DELETE_FAILED, message=str(e)))
    return StandardResponse.success(data=True, message=constants.MSG_KB_DOC_DELETED)
