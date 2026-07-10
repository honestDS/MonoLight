import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import constants
from app.core.crud.profile import profile_crud

# Re-use the refactored embedding and knowledge base query core functions
from app.core.embedding.knowledge_base import (
    embed_chunks_with_knowledge_base_config,
    query_knowledge_base,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.core.utils.text_splitter import TextSplitter
from app.models.channel import ModelChannel, ModelUsage
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCreate,
    KnowledgeBaseDocument,
    KnowledgeBaseDocumentContentResponse,
    KnowledgeBaseDocumentListResponse,
    KnowledgeBaseDocumentResponse,
    KnowledgeBaseListResponse,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseProfileBindingUpdate,
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


async def load_owned_knowledge_base(db: AsyncSession, kb_id: int, current_user: Any) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)
    if kb.uid != getattr(current_user, "uid", None) and not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=403, detail=constants.ERR_SESSION_NO_PERMISSION)
    return kb


async def get_knowledge_base_profile_ids(db: AsyncSession, kb_id: int) -> list[int]:
    result = await db.execute(select(KnowledgeBaseProfileBinding.profile_id).where(KnowledgeBaseProfileBinding.knowledge_base_id == kb_id))
    return list(result.scalars().all())


async def replace_knowledge_base_bindings(db: AsyncSession, kb_id: int, profile_ids: list[int]) -> None:
    result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == kb_id))
    for binding in result.scalars().all():
        await db.delete(binding)

    for profile_id in dict.fromkeys(profile_ids):
        db.add(KnowledgeBaseProfileBinding(knowledge_base_id=kb_id, profile_id=profile_id))


async def build_knowledge_base_response(db: AsyncSession, kb: KnowledgeBase) -> KnowledgeBaseResponse:
    response = KnowledgeBaseResponse.model_validate(kb)
    if kb.id is not None:
        response.profile_ids = await get_knowledge_base_profile_ids(db, kb.id)
    return response


async def load_embedding_model(db: AsyncSession, channel_id: int, model_id: str) -> tuple[ModelChannel, dict[str, Any]]:
    channel = await db.get(ModelChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail=constants.ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND)
    if not channel.is_active:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_CHANNEL_DISABLED)
    if not channel.base_url:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_CHANNEL_NO_URL)

    for item in channel.model_ids or []:
        if item.get("model_id") == model_id and item.get("usage") == ModelUsage.EMBEDDING and item.get("is_enabled", True):
            return channel, item
    raise HTTPException(status_code=404, detail=constants.ERR_PROFILE_NO_EMBEDDING_MODEL)


async def list_embedding_model_options(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(ModelChannel).where(ModelChannel.is_active))
    options = []
    for channel in result.scalars().all():
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


async def load_user_knowledge_base(db: AsyncSession, kb_id: int, current_user: Any) -> tuple[KnowledgeBase, Profile]:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)
    if kb.uid != getattr(current_user, "uid", None) and not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)

    result = await db.execute(select(Profile).join(KnowledgeBaseProfileBinding, KnowledgeBaseProfileBinding.profile_id == Profile.id).where(KnowledgeBaseProfileBinding.knowledge_base_id == kb_id).where(Profile.uid == getattr(current_user, "uid", None)))
    profile = result.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)

    return kb, profile


@router.post("/create", response_model=StandardResponse[KnowledgeBaseResponse])
async def create_knowledge_base(
    kb_in: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """创建知识库"""

    _channel, embedding_model = await load_embedding_model(db, kb_in.embedding_channel_id, kb_in.embedding_model_id)

    # 生成一个唯一的 collection_name
    collection_name = f"kb_{uuid.uuid4().hex}"

    # 在 ChromaDB 中创建 collection
    try:
        create_collection(collection_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_COLLECTION_CREATE_FAILED, message=str(e)))

    # 存入关系型数据库
    db_kb = KnowledgeBase(
        uid=current_user.uid,
        name=kb_in.name,
        description=kb_in.description,
        embedding_channel_id=kb_in.embedding_channel_id,
        embedding_model_id=kb_in.embedding_model_id,
        embedding_dimensions=embedding_model.get("embedding_dimensions"),
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

    return StandardResponse.success(data=await build_knowledge_base_response(db, db_kb), message=constants.MSG_KB_CREATED)


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
    list_query = select(KnowledgeBase)
    count_query = select(func.count()).select_from(KnowledgeBase)
    if not getattr(current_user, "is_superuser", False):
        list_query = list_query.where(KnowledgeBase.uid == current_user.uid)
        count_query = count_query.where(KnowledgeBase.uid == current_user.uid)

    result_kb = await db.execute(list_query.order_by(KnowledgeBase.created_at.desc()).offset(skip).limit(size))
    kbs = result_kb.scalars().all()
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

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

    kb.name = kb_in.name
    kb.description = kb_in.description

    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return StandardResponse.success(data=await build_knowledge_base_response(db, kb), message=constants.MSG_KB_UPDATED)


@router.get("/profile-bindings", response_model=StandardResponse[list[int]])
async def get_profile_knowledge_base_bindings(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=constants.ERR_PROFILE_NOT_FOUND)
    if profile.uid != getattr(current_user, "uid", None) and not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=403, detail=constants.ERR_SESSION_NO_PERMISSION)

    result = await db.execute(select(KnowledgeBaseProfileBinding.knowledge_base_id).join(KnowledgeBase, KnowledgeBase.id == KnowledgeBaseProfileBinding.knowledge_base_id).where(KnowledgeBaseProfileBinding.profile_id == profile_id).where(KnowledgeBase.uid == profile.uid))
    return StandardResponse.success(data=list(result.scalars().all()))


@router.post("/profile-bindings", response_model=StandardResponse[list[int]])
async def update_profile_knowledge_base_bindings(
    profile_id: int,
    binding_in: KnowledgeBaseProfileBindingUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=constants.ERR_PROFILE_NOT_FOUND)
    if profile.uid != getattr(current_user, "uid", None) and not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=403, detail=constants.ERR_SESSION_NO_PERMISSION)

    normalized_kb_ids = list(dict.fromkeys(binding_in.knowledge_base_ids))
    if normalized_kb_ids:
        result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id.in_(normalized_kb_ids)).where(KnowledgeBase.uid == profile.uid))
        knowledge_bases = list(result.scalars().all())
        if len(knowledge_bases) != len(normalized_kb_ids):
            raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND)

    existing_result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.profile_id == profile_id))
    for binding in existing_result.scalars().all():
        kb = await db.get(KnowledgeBase, binding.knowledge_base_id)
        if kb and kb.uid == profile.uid:
            await db.delete(binding)

    for kb_id in normalized_kb_ids:
        db.add(KnowledgeBaseProfileBinding(knowledge_base_id=kb_id, profile_id=profile_id))
    await db.commit()
    return StandardResponse.success(data=normalized_kb_ids)


@router.post("/query-test", response_model=StandardResponse[KnowledgeBaseQueryTestResponse])
async def query_test_knowledge_base(
    kb_id: int,
    query_in: KnowledgeBaseQueryTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):

    kb = await load_owned_knowledge_base(db, kb_id, current_user)
    profile = await profile_crud.get_active(db, uid=kb.uid)
    if not profile:
        raise HTTPException(status_code=404, detail=constants.ERR_PROFILE_NOT_FOUND)

    response_data = await query_knowledge_base(db, profile, kb_id, query_in.query, query_in.top_k, expose_rerank_error=True, require_binding=False)
    return StandardResponse.success(data=response_data)


@router.post("/delete", response_model=StandardResponse[bool])
async def delete_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """删除知识库"""

    kb = await load_owned_knowledge_base(db, kb_id, current_user)

    docs_result = await db.execute(select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == kb_id))
    for document in docs_result.scalars().all():
        await db.delete(document)
    binding_result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == kb_id))
    for binding in binding_result.scalars().all():
        await db.delete(binding)
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

    kb = await load_owned_knowledge_base(db, kb_id, current_user)
    if chunk_overlap >= chunk_size:
        raise HTTPException(status_code=400, detail=constants.ERR_KB_CHUNK_OVERLAP_ERROR)

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

    embeddings = await embed_chunks_with_knowledge_base_config(db, kb, chunks, batch_size)
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

    await load_owned_knowledge_base(db, kb_id, current_user)

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

    await load_owned_knowledge_base(db, kb_id, current_user)
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

    kb = await load_owned_knowledge_base(db, kb_id, current_user)
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
