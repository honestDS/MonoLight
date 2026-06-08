from typing import Any

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crud.provider import provider_crud
from app.core.exceptions import LLMException
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseQueryTestItem, KnowledgeBaseQueryTestResponse
from app.models.profile import Profile
from app.models.provider import ModelUsage
from app.providers.vector_db import get_collection
from app.transformers.openai import OpenAITransformer

KNOWLEDGE_BASE_QUERY_TOP_K = 5


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


async def list_available_knowledge_bases(db: AsyncSession, profile: Profile) -> list[KnowledgeBase]:
    """查询当前激活 Profile 可用的知识库"""
    query = select(KnowledgeBase).where(KnowledgeBase.profile_id == profile.id).order_by(KnowledgeBase.created_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())


def build_knowledge_base_prompt_items(kbs: list[KnowledgeBase]) -> list[dict[str, Any]]:
    """将知识库实体转换为系统提示词清单项，输出 id、name 与 description"""
    return [
        {
            "id": kb.id,
            "name": kb.name,
            "description": kb.description or "",
        }
        for kb in kbs
    ]


def build_knowledge_base_whitelist(kbs: list[KnowledgeBase]) -> list[int]:
    """生成当前 Profile 可用知识库 ID 白名单"""
    return [kb.id for kb in kbs if kb.id is not None]


async def query_knowledge_base(
    db: AsyncSession,
    profile: Profile,
    kb_id: int,
    query: str,
    top_k: int = KNOWLEDGE_BASE_QUERY_TOP_K,
) -> KnowledgeBaseQueryTestResponse:
    """根据知识库 ID 检索知识库"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    if kb.profile_id != profile.id:
        raise HTTPException(status_code=403, detail="无权查询不属于当前配置的知识库")

    query_embedding = (await embed_chunks(db, profile, [query], 1))[0]

    try:
        collection = get_collection(kb.collection_name)
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
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

    return KnowledgeBaseQueryTestResponse(items=items)
