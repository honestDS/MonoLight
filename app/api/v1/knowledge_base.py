import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crud.provider import provider_crud
from app.core.exceptions import LLMException
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
    KnowledgeBaseQueryTestItem,
    KnowledgeBaseQueryTestRequest,
    KnowledgeBaseQueryTestResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
)
from app.models.profile import Profile
from app.models.provider import ModelUsage
from app.providers.database import get_db
from app.providers.vector_db import create_collection, delete_collection, delete_collection_items, get_collection, get_or_create_collection
from app.schemas.response import StandardResponse
from app.transformers.openai import OpenAITransformer

router = APIRouter(prefix="/knowledge-base", tags=["KnowledgeBase"])


async def get_profile_embedding_config(db: AsyncSession, profile: Profile) -> tuple[str, str, str, int | None]:
    provider_config = (profile.configs or {}).get("provider", {})
    embedding_provider_id = provider_config.get("embedding_provider_id")
    embedding_model_id = provider_config.get("embedding_model_id")
    embedding_dimensions = provider_config.get("embedding_dimensions")

    if not embedding_provider_id or embedding_provider_id <= 0:
        raise HTTPException(status_code=400, detail="该知识库绑定的配置文件未设置向量模型提供商")
    if not embedding_model_id:
        raise HTTPException(status_code=400, detail="该知识库绑定的配置文件未设置向量模型ID")

    provider = await provider_crud.get(db, embedding_provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="配置文件绑定的向量模型提供商不存在")
    if provider.usage != ModelUsage.EMBEDDING:
        raise HTTPException(status_code=400, detail="配置文件绑定的提供商不是向量模型类型")
    if not provider.base_url:
        raise HTTPException(status_code=400, detail="向量模型提供商未配置 Base URL")
    return provider.api_key, provider.base_url, embedding_model_id, embedding_dimensions


async def is_embedding_profile_available(db: AsyncSession, profile: Profile) -> bool:
    try:
        await get_profile_embedding_config(db, profile)
        return True
    except HTTPException:
        return False


async def embed_chunks(db: AsyncSession, profile: Profile, texts: list[str], batch_size: int) -> list[list[float]]:
    api_key, base_url, model_id, dimensions = await get_profile_embedding_config(db, profile)
    transformer = OpenAITransformer()
    try:
        return await transformer.embed_texts(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=texts,
            batch_size=batch_size,
            dimensions=dimensions,
        )
    except LLMException as e:
        raise HTTPException(status_code=502, detail=f"向量模型调用失败: {e.message}")


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
        raise HTTPException(status_code=404, detail="指定的 Profile 不存在")
    await get_profile_embedding_config(db, profile)

    # 生成一个唯一的 collection_name
    collection_name = f"kb_{uuid.uuid4().hex}"

    # 在 ChromaDB 中创建 collection
    try:
        create_collection(collection_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建向量库集合失败: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"创建知识库失败，已回滚向量库集合: {str(e)}")

    return StandardResponse.success(data=KnowledgeBaseResponse.model_validate(db_kb))


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
    profile_options = [KnowledgeBaseProfileOption.model_validate(p) for p in profiles]
    available_profiles = [KnowledgeBaseProfileOption.model_validate(p) for p in profiles if await is_embedding_profile_available(db, p)]

    data = KnowledgeBaseListResponse(
        items=[KnowledgeBaseResponse.model_validate(kb) for kb in kbs],
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
        raise HTTPException(status_code=404, detail="知识库不存在")

    kb.name = kb_in.name
    kb.description = kb_in.description

    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return StandardResponse.success(data=KnowledgeBaseResponse.model_validate(kb))


@router.post("/query-test", response_model=StandardResponse[KnowledgeBaseQueryTestResponse])
async def query_test_knowledge_base(
    kb_id: int,
    query_in: KnowledgeBaseQueryTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    profile = await db.get(Profile, kb.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="知识库绑定的配置文件不存在")

    query_embedding = (await embed_chunks(db, profile, [query_in.query], 1))[0]

    try:
        collection = get_collection(kb.collection_name)
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=query_in.top_k,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"知识库检索失败: {str(e)}")

    ids = result.get("ids", [[]])[0] if result.get("ids") else []
    documents = result.get("documents", [[]])[0] if result.get("documents") else []
    metadatas = result.get("metadatas", [[]])[0] if result.get("metadatas") else []
    distances = result.get("distances", [[]])[0] if result.get("distances") else []
    items = []
    for index, item_id in enumerate(ids):
        items.append(
            KnowledgeBaseQueryTestItem(
                id=item_id,
                content=documents[index] if index < len(documents) else "",
                metadata=metadatas[index] if index < len(metadatas) and metadatas[index] else {},
                distance=distances[index] if index < len(distances) else None,
            )
        )

    return StandardResponse.success(data=KnowledgeBaseQueryTestResponse(items=items))


@router.post("/delete", response_model=StandardResponse[bool])
async def delete_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
):
    """删除知识库"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

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
        raise HTTPException(status_code=500, detail=f"删除知识库失败，已撤销数据库删除操作: {str(e)}")

    return StandardResponse.success(data=True)


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
        raise HTTPException(status_code=404, detail="知识库不存在")
    if chunk_overlap >= chunk_size:
        raise HTTPException(status_code=400, detail="分块重叠必须小于分块大小")

    profile = await db.get(Profile, kb.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="知识库绑定的配置文件不存在")

    raw_content = await file.read()
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw_content.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="暂仅支持 UTF-8 或 GBK 编码的文本类文档")

    chunks = TextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap).split(content)
    if not chunks:
        raise HTTPException(status_code=400, detail="文档内容为空，无法导入")

    embeddings = await embed_chunks(db, profile, chunks, batch_size)
    document_uuid = uuid.uuid4().hex
    chunk_ids = [f"kb_{kb.id}_doc_{document_uuid}_chunk_{index}" for index in range(len(chunks))]
    metadatas = [
        {
            "knowledge_base_id": kb.id,
            "document_uuid": document_uuid,
            "filename": file.filename or "未命名文档",
            "chunk_index": index,
        }
        for index in range(len(chunks))
    ]

    try:
        collection = get_or_create_collection(kb.collection_name)
        collection.add(ids=chunk_ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入向量库失败: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"保存文档失败，已回滚向量分块: {str(e)}")

    return StandardResponse.success(data=KnowledgeBaseDocumentResponse.model_validate(db_document), message="文档导入成功")


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
        raise HTTPException(status_code=404, detail="知识库不存在")

    skip = (page - 1) * size
    result = await db.execute(
        select(KnowledgeBaseDocument)
        .where(KnowledgeBaseDocument.knowledge_base_id == kb_id)
        .order_by(KnowledgeBaseDocument.created_at.desc())
        .offset(skip)
        .limit(size)
    )
    documents = result.scalars().all()
    total_result = await db.execute(select(func.count()).select_from(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == kb_id))
    total = total_result.scalar() or 0

    return StandardResponse.success(
        data=KnowledgeBaseDocumentListResponse(
            items=[KnowledgeBaseDocumentResponse.model_validate(item) for item in documents],
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
        raise HTTPException(status_code=404, detail="文档不存在")
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
        raise HTTPException(status_code=404, detail="知识库不存在")
    document = await db.get(KnowledgeBaseDocument, document_id)
    if not document or document.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    await db.delete(document)
    try:
        await db.flush()
        delete_collection_items(kb.collection_name, document.chunk_ids or [])
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"删除文档失败，已撤销数据库删除操作: {str(e)}")
    return StandardResponse.success(data=True, message="文档删除成功")
