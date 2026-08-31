from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.embedding.knowledge_base import build_knowledge_base_prompt_items
from app.core.tools import knowledge_base_query as knowledge_base_query_module
from app.core.tools.knowledge_base_query import KnowledgeBaseQueryExecutor
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseType
from app.models.profile import Profile


def _profile() -> Profile:
    return Profile(id=9, uid="user-1", name="stage6-profile", configs={})


def _executor() -> KnowledgeBaseQueryExecutor:
    executor = KnowledgeBaseQueryExecutor(project_root=".", uid="user-1")
    executor.set_runtime_context(
        db=SimpleNamespace(),
        profile=_profile(),
        session_id="session-1",
        allowed_knowledge_base_ids=[10],
    )
    return executor


@pytest.mark.asyncio
async def test_managed_query_result_exposes_only_trusted_managed_identity(monkeypatch):
    async def fake_query_knowledge_base(**_kwargs):
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    content="Managed knowledge body",
                    metadata_={
                        "knowledge_type": "managed",
                        "managed_knowledge_id": 31,
                        "managed_knowledge_version": 4,
                        "managed_knowledge_llm_maintainable": True,
                    },
                )
            ]
        )

    monkeypatch.setattr(
        knowledge_base_query_module,
        "query_knowledge_base",
        fake_query_knowledge_base,
    )

    payload = json.loads(
        await _executor().execute(
            knowledge_base_id=10,
            query="managed topic",
        )
    )

    assert payload == {
        "items": [
            {
                "source": "未知来源",
                "content": "Managed knowledge body",
                "knowledge_type": "managed",
                "knowledge_id": 31,
                "knowledge_expected_version": 4,
                "llm_maintainable": True,
            }
        ]
    }


@pytest.mark.asyncio
async def test_user_knowledge_base_result_never_exposes_managed_write_identity(monkeypatch):
    async def fake_query_knowledge_base(**_kwargs):
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    content="User document body",
                    metadata_={
                        "filename": "manual.md",
                        "managed_knowledge_id": 31,
                        "managed_knowledge_version": 4,
                    },
                )
            ]
        )

    monkeypatch.setattr(
        knowledge_base_query_module,
        "query_knowledge_base",
        fake_query_knowledge_base,
    )

    payload = json.loads(
        await _executor().execute(
            knowledge_base_id=10,
            query="manual topic",
        )
    )

    assert payload == {
        "items": [
            {
                "source": "manual.md",
                "content": "User document body",
            }
        ]
    }


def test_knowledge_base_prompt_items_identify_managed_and_user_stores():
    user_knowledge_base = KnowledgeBase(
        id=1,
        uid="user-1",
        name="User KB",
        embedding_channel_id=1,
        embedding_model_id="embedding-model",
        collection_name="user-kb",
        knowledge_base_type=KnowledgeBaseType.USER,
    )
    managed_knowledge_base = KnowledgeBase(
        id=2,
        uid="user-1",
        name="Managed KB",
        embedding_channel_id=1,
        embedding_model_id="embedding-model",
        collection_name="managed-kb",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=9,
    )

    items = build_knowledge_base_prompt_items([user_knowledge_base, managed_knowledge_base])

    assert items[0]["kind"] == "user_knowledge_base"
    assert items[1]["kind"] == "managed_knowledge"


def test_knowledge_base_query_log_serializers_do_not_leak_query_or_content():
    arguments = process_single_tool_module._serialize_knowledge_base_query_log_arguments(
        {
            "knowledge_base_id": 10,
            "query": "private semantic query",
        }
    )
    result = process_single_tool_module._serialize_knowledge_base_query_log_result(
        json.dumps(
            {
                "items": [
                    {
                        "source": "managed",
                        "content": "private managed knowledge body",
                        "knowledge_type": "managed",
                        "knowledge_id": 31,
                        "knowledge_expected_version": 4,
                    },
                    {
                        "source": "manual.md",
                        "content": "private user document body",
                    },
                ]
            },
            ensure_ascii=False,
        )
    )

    assert "private semantic query" not in arguments
    assert json.loads(arguments) == {
        "knowledge_base_id": 10,
        "query_length": len("private semantic query"),
    }
    assert "private managed knowledge body" not in result
    assert "private user document body" not in result
    assert json.loads(result) == {
        "items": [
            {
                "source": "managed",
                "knowledge_type": "managed",
                "knowledge_id": 31,
                "knowledge_expected_version": 4,
            },
            {"source": "manual.md"},
        ]
    }


def test_knowledge_base_query_log_serializer_redacts_error_detail() -> None:
    sensitive_error = "query failed with private managed knowledge body"

    result = process_single_tool_module._serialize_knowledge_base_query_log_result(json.dumps({"error": sensitive_error}, ensure_ascii=False))

    assert sensitive_error not in result
    assert json.loads(result) == {"error": True}


def test_structured_truncation_removes_managed_write_identity() -> None:
    result = json.dumps(
        {
            "items": [
                {
                    "source": "managed",
                    "content": "managed-body-" * 2000,
                    "knowledge_type": "managed",
                    "knowledge_id": 31,
                    "knowledge_expected_version": 4,
                    "llm_maintainable": True,
                }
            ]
        },
        ensure_ascii=False,
    )

    truncated, stats = process_single_tool_module._truncate_knowledge_base_query_result_for_budget(
        result,
        context_window_k=1,
        budget_tokens=80,
    )
    payload = json.loads(truncated)

    assert stats.truncated_count == 1
    assert payload["truncated"] is True
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["truncated"] is True
    assert item["knowledge_type"] == "managed"
    assert "knowledge_id" not in item
    assert "knowledge_expected_version" not in item
    assert "llm_maintainable" not in item
