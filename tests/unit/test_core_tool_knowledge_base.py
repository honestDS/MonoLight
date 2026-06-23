import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.embedding.knowledge_base import (
    build_knowledge_base_prompt_items,
    build_knowledge_base_whitelist,
    list_available_knowledge_bases,
    query_knowledge_base,
)
from app.core.exceptions import LLMException
from app.core.prompts import SYSTEM_RUNTIME_CONTEXT_POLICY
from app.core.retrieval.schemas import RetrievalHit
from app.core.tools import get_tools_for_profile
from app.core.tools.knowledge_base_query import KnowledgeBaseQueryExecutor
from app.core.utils.dispatcher.inject_system_prompt import inject_system_prompt
from app.models.knowledge_base import KnowledgeBase
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile


class FakeKnowledgeBaseQueryResponse:
    def __init__(self, items):
        self.items = items


@pytest.mark.asyncio
async def test_knowledge_base_core_functions(db_session: AsyncSession):
    # 1. 创建测试 Profile
    profile = Profile(name="Test Profile KB", configs={"provider": {"embedding_provider_id": 1, "embedding_model_id": "text-embedding-3-small", "embedding_dimensions": 1536}})
    db_session.add(profile)
    await db_session.commit()
    await db_session.refresh(profile)

    # 2. 创建测试知识库
    kb1 = KnowledgeBase(name="KB 1", description="Description 1", profile_id=profile.id, collection_name="kb_test_1")
    kb2 = KnowledgeBase(name="KB 2", description="Description 2", profile_id=profile.id, collection_name="kb_test_2")
    kb_other = KnowledgeBase(name="KB Other", description="Other Profile KB", profile_id=99999, collection_name="kb_test_other")
    db_session.add(kb1)
    db_session.add(kb2)
    db_session.add(kb_other)
    await db_session.commit()
    await db_session.refresh(kb1)
    await db_session.refresh(kb2)

    # 测试 list_available_knowledge_bases 只返回当前 profile 绑定的知识库
    kbs = await list_available_knowledge_bases(db_session, profile)
    assert len(kbs) == 2
    assert {kb.name for kb in kbs} == {"KB 1", "KB 2"}

    # 测试 build_knowledge_base_prompt_items 输出 id、name 与 description
    prompt_items = build_knowledge_base_prompt_items(kbs)
    assert len(prompt_items) == 2
    for item in prompt_items:
        assert set(item.keys()) == {"id", "name", "description"}
        assert item["id"] in {kb1.id, kb2.id}
        assert "KB" in item["name"]

    # 测试 build_knowledge_base_whitelist 生成 id 白名单
    whitelist = build_knowledge_base_whitelist(kbs)
    assert set(whitelist) == {kb1.id, kb2.id}

    # 测试 query_knowledge_base 的非法 ID 与非法 Profile 校验
    with pytest.raises(HTTPException) as exc_info:
        await query_knowledge_base(db_session, profile, 99999, "query")
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        await query_knowledge_base(db_session, profile, kb_other.id, "query")
    assert exc_info.value.status_code == 403

    # 清理数据
    await db_session.delete(kb1)
    await db_session.delete(kb2)
    await db_session.delete(kb_other)
    await db_session.delete(profile)
    await db_session.commit()


@pytest.mark.asyncio
async def test_dynamic_tools_and_prompt_injection(db_session: AsyncSession):
    # 1. 创建没有向量库的 Profile
    profile_no_kb = Profile(name="Profile No KB", configs={})
    db_session.add(profile_no_kb)
    await db_session.commit()
    await db_session.refresh(profile_no_kb)

    # 1.1 测试动态工具列表不暴露 query_knowledge_base
    tools, whitelist = await get_tools_for_profile(db_session, profile_no_kb)
    assert "query_knowledge_base" not in [t["function"]["name"] for t in tools]
    assert len(whitelist) == 0

    # 1.2 测试提示词注入不包含 <available_knowledge_bases>
    messages = [InternalMessage(role=MessageRole.USER, content="hello")]
    messages_injected = await inject_system_prompt(db_session, profile_no_kb, messages)
    assert messages_injected[0].role == MessageRole.SYSTEM
    system_msg = messages_injected[0].content
    assert SYSTEM_RUNTIME_CONTEXT_POLICY in system_msg
    assert "<available_knowledge_bases>" not in system_msg

    # 2. 创建有向量库的 Profile
    profile_with_kb = Profile(name="Profile With KB", configs={})
    db_session.add(profile_with_kb)
    await db_session.commit()
    await db_session.refresh(profile_with_kb)

    kb = KnowledgeBase(name="My Special KB", description="Special Description", profile_id=profile_with_kb.id, collection_name="kb_special")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    # 2.1 测试动态工具列表暴露并定制化 query_knowledge_base
    tools, whitelist = await get_tools_for_profile(db_session, profile_with_kb, embedding_profile_available=True)
    assert "query_knowledge_base" in [t["function"]["name"] for t in tools]
    assert whitelist == [kb.id]

    kb_tool = next(t for t in tools if t["function"]["name"] == "query_knowledge_base")
    assert kb_tool["function"]["parameters"]["properties"]["knowledge_base_id"]["enum"] == [str(kb.id)]
    assert "My Special KB" in kb_tool["function"]["parameters"]["properties"]["knowledge_base_id"]["description"]

    # 2.2 测试提示词注入包含 <available_knowledge_bases>
    messages_injected = await inject_system_prompt(db_session, profile_with_kb, messages, embedding_profile_available=True)
    assert messages_injected[0].role == MessageRole.SYSTEM
    system_msg = messages_injected[0].content
    assert SYSTEM_RUNTIME_CONTEXT_POLICY in system_msg
    assert "<available_knowledge_bases>" in system_msg
    assert f'"id": {kb.id}' in system_msg
    assert "My Special KB" in system_msg
    assert "Special Description" in system_msg

    # 清理数据
    await db_session.delete(kb)
    await db_session.delete(profile_no_kb)
    await db_session.delete(profile_with_kb)
    await db_session.commit()


@pytest.mark.asyncio
async def test_knowledge_base_query_executor(db_session: AsyncSession):
    # 模拟环境
    executor = KnowledgeBaseQueryExecutor(project_root=".")

    # 1. 缺少必要参数的测试
    res = await executor.execute()
    data = json.loads(res)
    assert "error" in data
    assert "Missing required arguments" in data["error"]

    # 2. 没经过 runtime 注入（白名单为空）的测试
    res_unauth = await executor.execute(knowledge_base_id=1, query="test")
    data_unauth = json.loads(res_unauth)
    assert "error" in data_unauth
    assert "Unauthorized knowledge_base_id" in data_unauth["error"]

    # 3. 注入白名单但缺少数据库上下文的测试
    executor.set_runtime_context(allowed_knowledge_base_ids=[1])
    res_no_db = await executor.execute(knowledge_base_id=1, query="test")
    data_no_db = json.loads(res_no_db)
    assert "error" in data_no_db
    assert "Database session or Profile configuration is missing" in data_no_db["error"]

    # 4. LLM 可能把 integer 参数序列化为字符串，执行器需要先归一化为 int 再校验白名单
    res_string_id = await executor.execute(knowledge_base_id="1", query="test")
    data_string_id = json.loads(res_string_id)
    assert "error" in data_string_id
    assert "Database session or Profile configuration is missing" in data_string_id["error"]

    # 5. 非法 ID 字符串应返回明确错误
    res_invalid_id = await executor.execute(knowledge_base_id="not-int", query="test")
    data_invalid_id = json.loads(res_invalid_id)
    assert "error" in data_invalid_id
    assert "Invalid knowledge_base_id" in data_invalid_id["error"]


@pytest.mark.asyncio
async def test_knowledge_base_query_executor_returns_only_source_and_content(monkeypatch, db_session: AsyncSession):
    async def fake_query_knowledge_base(**kwargs):
        return FakeKnowledgeBaseQueryResponse(
            [
                SimpleNamespace(
                    content="命中文本片段",
                    metadata_={
                        "filename": "source.md",
                        "dense_rank": 1,
                        "sparse_rank": 1,
                        "fusion_score": 0.1,
                    },
                )
            ]
        )

    monkeypatch.setattr("app.core.tools.knowledge_base_query.query_knowledge_base", fake_query_knowledge_base)

    executor = KnowledgeBaseQueryExecutor(project_root=".")
    executor.set_runtime_context(
        db=db_session,
        profile=Profile(name="test", configs={}),
        allowed_knowledge_base_ids=[1],
    )

    res = await executor.execute(knowledge_base_id="1", query="test")
    data = json.loads(res)

    assert set(data.keys()) == {"items"}
    assert data["items"] == [{"source": "source.md", "content": "命中文本片段"}]


@pytest.mark.asyncio
async def test_query_knowledge_base_falls_back_to_hybrid_when_rerank_fails(monkeypatch, db_session: AsyncSession):
    kb = KnowledgeBase(name="KB Rerank Fallback", description="", profile_id=1, collection_name="kb_rerank_fallback")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    profile = Profile(id=1, name="test", configs={})

    async def fake_embed_chunks(*args, **kwargs):
        return [[0.1, 0.2]]

    async def fake_get_profile_rerank_config(*args, **kwargs):
        if 1 in (kwargs.get("excluded_priorities") or set()):
            return None
        return SimpleNamespace(
            candidate_k=5,
            priority=1,
            provider_id=1,
            provider_name="rerank-provider",
            provider_type="OPENAI",
            model_id="rerank-model",
        )

    async def fake_hybrid_query_collection(*args, **kwargs):
        return [
            RetrievalHit(
                id="chunk-1",
                content="混合检索结果",
                metadata={"filename": "fallback.md"},
                dense_rank=1,
                sparse_rank=1,
                fusion_score=0.1,
            ),
            RetrievalHit(
                id="chunk-2",
                content="第二个候选片段",
                metadata={"filename": "fallback.md"},
                dense_rank=2,
                sparse_rank=2,
                fusion_score=0.05,
            ),
        ]

    async def fake_rerank_retrieval_hits(*args, **kwargs):
        raise LLMException(message="rerank failed")

    monkeypatch.setattr("app.core.embedding.knowledge_base.embed_chunks", fake_embed_chunks)
    monkeypatch.setattr("app.core.embedding.knowledge_base.get_profile_rerank_config", fake_get_profile_rerank_config)
    monkeypatch.setattr("app.core.embedding.knowledge_base.hybrid_query_collection", fake_hybrid_query_collection)
    monkeypatch.setattr("app.core.embedding.knowledge_base.rerank_retrieval_hits", fake_rerank_retrieval_hits)

    response = await query_knowledge_base(db_session, profile, kb.id, "query", top_k=1, expose_rerank_error=False)

    assert response.retrieval_mode == "hybrid"
    assert response.rerank_error is None
    assert response.items[0].content == "混合检索结果"

    await db_session.delete(kb)
    await db_session.commit()


@pytest.mark.asyncio
async def test_query_knowledge_base_exposes_rerank_error_for_query_test(monkeypatch, db_session: AsyncSession):
    kb = KnowledgeBase(name="KB Rerank Error", description="", profile_id=1, collection_name="kb_rerank_error")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    profile = Profile(id=1, name="test", configs={})

    async def fake_embed_chunks(*args, **kwargs):
        return [[0.1, 0.2]]

    async def fake_get_profile_rerank_config(*args, **kwargs):
        if 1 in (kwargs.get("excluded_priorities") or set()):
            return None
        return SimpleNamespace(
            candidate_k=5,
            priority=1,
            provider_id=1,
            provider_name="rerank-provider",
            provider_type="OPENAI",
            model_id="rerank-model",
        )

    async def fake_hybrid_query_collection(*args, **kwargs):
        return [
            RetrievalHit(
                id="chunk-1",
                content="混合检索结果",
                metadata={"filename": "fallback.md"},
                dense_rank=1,
                sparse_rank=1,
                fusion_score=0.1,
            ),
            RetrievalHit(
                id="chunk-2",
                content="第二个候选片段",
                metadata={"filename": "fallback.md"},
                dense_rank=2,
                sparse_rank=2,
                fusion_score=0.05,
            ),
        ]

    async def fake_rerank_retrieval_hits(*args, **kwargs):
        raise LLMException(message="rerank failed")

    monkeypatch.setattr("app.core.embedding.knowledge_base.embed_chunks", fake_embed_chunks)
    monkeypatch.setattr("app.core.embedding.knowledge_base.get_profile_rerank_config", fake_get_profile_rerank_config)
    monkeypatch.setattr("app.core.embedding.knowledge_base.hybrid_query_collection", fake_hybrid_query_collection)
    monkeypatch.setattr("app.core.embedding.knowledge_base.rerank_retrieval_hits", fake_rerank_retrieval_hits)

    with pytest.raises(HTTPException) as exc_info:
        await query_knowledge_base(db_session, profile, kb.id, "query", top_k=1, expose_rerank_error=True)

    assert exc_info.value.status_code == 502

    await db_session.delete(kb)
    await db_session.commit()
