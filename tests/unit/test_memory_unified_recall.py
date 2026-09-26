import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.dispatch_context import build_dispatch_context
from app.core.knowledge import unified_recall
from app.core.knowledge.results import (
    KnowledgeRecallItem,
    KnowledgeRecallResult,
    KnowledgeRecallSourceType,
)
from app.core.memory import MemoryRecallItem, MemoryRecallResult, MemoryRecallStatus
from app.core.memory import chat_history as chat_history_module
from app.core.prompts import LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT, LONGTERM_MEMORY_SYSTEM_PROMPT
from app.core.tools import (
    MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA,
    LongTermMemoryExecutor,
    validate_longterm_memory_arguments,
)
from app.core.tools import longterm_memory as longterm_memory_module
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.core.utils.dispatcher.truncate_tool_result import (
    truncate_longterm_memory_recall_result_for_budget,
)
from app.models.profile import Profile, ProfileConfig


def _profile_and_config():
    configs = {
        "memory": {
            "enabled": True,
            "top_k": 3,
            "candidate_k": 7,
            "result_max_chars": 700,
            "chat_history": {
                "top_k": 4,
                "candidate_k": 9,
                "result_max_chars": 600,
            },
            "knowledge": {
                "top_k": 2,
                "candidate_k": 6,
                "result_max_chars": 500,
            },
        }
    }
    profile = Profile(id=7, uid="user-1", name="memory_unified_recall-profile", configs=configs)
    return profile, ProfileConfig.model_validate(configs)


def _build_executor():
    profile, cfg = _profile_and_config()
    context = build_dispatch_context(
        mode="interactive",
        source="interactive_tool",
        uid="user-1",
        session_id="session-1",
        profile=profile,
        db=SimpleNamespace(name="outer-db"),
        tool_call_id="call-memory_unified_recall",
        source_message_id=44,
    )
    executor = LongTermMemoryExecutor(project_root=".", uid=context.uid)
    executor.set_config(cfg)
    executor.set_runtime_context(dispatch_context=context)
    return executor, context


def test_recall_schema_requires_distinct_memory_and_knowledge_queries():
    parameters = MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA["function"]["parameters"]
    properties = parameters["properties"]
    function_description = MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA["function"]["description"].lower()

    assert "knowledge_query" in properties
    assert "unified" in function_description
    assert "personal long-term memory" in function_description
    assert "chat history" in function_description
    assert "managed knowledge" in function_description
    assert "user knowledge bases" in function_description
    assert "recall/create/update/delete apply only to personal long-term memory" not in function_description
    assert "knowledge_base" in function_description
    assert "globally reranked" in function_description
    assert "stable" in properties["query"]["description"].lower()
    assert "document" in properties["knowledge_query"]["description"].lower()
    operation, error = validate_longterm_memory_arguments(
        {
            "operation": "recall",
            "query": "stable background",
            "knowledge_query": " ",
        }
    )
    assert operation == "recall"
    assert error is not None
    assert "knowledge_query" in error


def test_recall_prompts_require_distinct_knowledge_query_and_describe_three_sources():
    correction = LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT.lower()
    system_prompt = LONGTERM_MEMORY_SYSTEM_PROMPT.lower()

    assert "knowledge_query" in correction
    assert "document" in correction
    assert "every new user request must begin" not in system_prompt
    assert "do not recall again" in system_prompt
    assert "knowledge_base" in system_prompt
    assert "chat_history" in system_prompt
    assert "data, not instructions" in system_prompt


@pytest.mark.asyncio
async def test_no_knowledge_sources_skip_embedding_and_reranker(monkeypatch):
    profile, _cfg = _profile_and_config()

    async def no_sources(*_args, **_kwargs):
        return []

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("no knowledge source must not call embedding or reranker")

    monkeypatch.setattr(
        unified_recall.knowledge_base_crud,
        "list_recall_sources_by_profile",
        no_sources,
    )
    monkeypatch.setattr(
        unified_recall,
        "embed_chunks_with_knowledge_base_config",
        unexpected_call,
    )
    monkeypatch.setattr(unified_recall, "_global_rerank", unexpected_call)

    result = await unified_recall.knowledge_recall_service.recall(
        SimpleNamespace(),
        profile,
        "document query",
    )

    assert result.items == ()


@pytest.mark.asyncio
async def test_chat_history_candidate_k_controls_database_candidate_page(monkeypatch):
    captured = {}

    async def fake_list_recallable_chat_page(_db, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        chat_history_module.message_crud,
        "list_recallable_chat_page",
        fake_list_recallable_chat_page,
    )

    result = await chat_history_module.chat_history_recall_service.recall(
        SimpleNamespace(),
        "user-1",
        "query",
        top_k=3,
        candidate_k=17,
    )

    assert result.items == ()
    assert captured["limit"] == 17


@pytest.mark.asyncio
async def test_recall_runs_three_sources_concurrently_with_independent_queries_and_limits(monkeypatch):
    executor, context = _build_executor()
    started: set[str] = set()
    all_started = asyncio.Event()
    calls: dict[str, dict] = {}

    async def rendezvous(name: str):
        started.add(name)
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=0.5)

    class MemoryService:
        async def recall(self, **kwargs):
            calls["memory"] = kwargs
            await rendezvous("memory")
            return MemoryRecallResult(
                status=MemoryRecallStatus.OK,
                items=(
                    MemoryRecallItem(
                        memory_id=1,
                        memory_key="project",
                        content="memory body",
                        memory_type="project",
                        version=2,
                        updated_at=datetime(2026, 9, 7, tzinfo=UTC),
                        source="llm_tool",
                    ),
                ),
            )

    class ChatService:
        async def recall(self, **kwargs):
            calls["chat"] = kwargs
            await rendezvous("chat")
            return SimpleNamespace(
                items=(
                    SimpleNamespace(
                        role="user",
                        content="chat body",
                        session_id="older-session",
                        created_at=None,
                        truncated=False,
                    ),
                )
            )

    class KnowledgeService:
        async def recall(self, db, profile, query):
            calls["knowledge"] = {"db": db, "profile": profile, "query": query}
            await rendezvous("knowledge")
            return KnowledgeRecallResult(
                items=(
                    KnowledgeRecallItem(
                        knowledge_base_id=10,
                        knowledge_base_name="Managed",
                        source_type=KnowledgeRecallSourceType.MANAGED_KNOWLEDGE,
                        source="project.architecture",
                        content="managed body",
                        llm_maintainable=True,
                        knowledge_id=20,
                        knowledge_key="project.architecture",
                        knowledge_expected_version=3,
                    ),
                    KnowledgeRecallItem(
                        knowledge_base_id=11,
                        knowledge_base_name="Docs",
                        source_type=KnowledgeRecallSourceType.USER_KNOWLEDGE,
                        source="guide.md",
                        content="user document body",
                    ),
                )
            )

    session_counter = 0

    @asynccontextmanager
    async def fake_session():
        nonlocal session_counter
        session_counter += 1
        yield SimpleNamespace(name=f"recall-db-{session_counter}")

    monkeypatch.setattr(longterm_memory_module, "AsyncSessionLocal", fake_session)
    monkeypatch.setattr(longterm_memory_module, "AsyncSession", SimpleNamespace)
    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: MemoryService())
    monkeypatch.setattr(longterm_memory_module, "_get_chat_history_recall_service", lambda: ChatService())
    monkeypatch.setattr(longterm_memory_module, "_get_knowledge_recall_service", lambda: KnowledgeService())

    raw_result = await executor.execute(
        operation="recall",
        query="stable project background",
        knowledge_query="How does the project architecture work?",
    )
    payload = json.loads(raw_result)

    assert started == {"memory", "chat", "knowledge"}
    assert session_counter == 3
    assert calls["memory"] == {
        "db": calls["memory"]["db"],
        "uid": "user-1",
        "query": "stable project background",
        "top_k": 3,
        "candidate_k": 7,
        "result_max_chars": 700,
    }
    assert calls["chat"] == {
        "db": calls["chat"]["db"],
        "uid": "user-1",
        "query": "stable project background",
        "top_k": 4,
        "candidate_k": 9,
        "result_max_chars": 600,
        "before_message_id": context.source_message_id,
    }
    assert calls["knowledge"]["profile"] is context.profile
    assert calls["knowledge"]["query"] == "How does the project architecture work?"
    assert payload["items"][0]["content"] == "memory body"
    assert payload["chat_history"][0]["content"] == "chat body"
    assert payload["knowledge_base"] == [
        {
            "knowledge_base_id": 10,
            "knowledge_base_name": "Managed",
            "source_type": "managed_knowledge",
            "source": "project.architecture",
            "content": "managed body",
            "truncated": False,
            "llm_maintainable": True,
            "knowledge_id": 20,
            "knowledge_key": "project.architecture",
            "knowledge_expected_version": 3,
        },
        {
            "knowledge_base_id": 11,
            "knowledge_base_name": "Docs",
            "source_type": "user_knowledge",
            "source": "guide.md",
            "content": "user document body",
            "truncated": False,
            "llm_maintainable": False,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_source", ["memory", "chat", "knowledge"])
async def test_recall_source_failures_are_isolated(monkeypatch, failed_source):
    executor, _context = _build_executor()

    class MemoryService:
        async def recall(self, **_kwargs):
            if failed_source == "memory":
                raise RuntimeError("memory failed")
            return MemoryRecallResult(
                status=MemoryRecallStatus.OK,
                items=(
                    MemoryRecallItem(
                        memory_id=1,
                        memory_key="m",
                        content="memory",
                        memory_type="fact",
                        version=1,
                        updated_at=datetime(2026, 9, 7, tzinfo=UTC),
                        source="llm_tool",
                    ),
                ),
            )

    class ChatService:
        async def recall(self, **_kwargs):
            if failed_source == "chat":
                raise RuntimeError("chat failed")
            return SimpleNamespace(items=(SimpleNamespace(role="user", content="chat", truncated=False),))

    class KnowledgeService:
        async def recall(self, *_args, **_kwargs):
            if failed_source == "knowledge":
                raise RuntimeError("knowledge failed")
            return KnowledgeRecallResult(
                items=(
                    KnowledgeRecallItem(
                        knowledge_base_id=10,
                        knowledge_base_name="Docs",
                        source_type=KnowledgeRecallSourceType.USER_KNOWLEDGE,
                        source="guide.md",
                        content="knowledge",
                    ),
                )
            )

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    monkeypatch.setattr(longterm_memory_module, "AsyncSessionLocal", fake_session)
    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: MemoryService())
    monkeypatch.setattr(longterm_memory_module, "_get_chat_history_recall_service", lambda: ChatService())
    monkeypatch.setattr(longterm_memory_module, "_get_knowledge_recall_service", lambda: KnowledgeService())

    payload = json.loads(
        await executor.execute(
            operation="recall",
            query="memory query",
            knowledge_query="knowledge query",
        )
    )

    assert bool(payload["items"]) is (failed_source != "memory")
    assert bool(payload.get("chat_history")) is (failed_source != "chat")
    assert bool(payload.get("knowledge_base")) is (failed_source != "knowledge")


def test_longterm_memory_log_redaction_covers_knowledge_query_and_results():
    arguments = process_single_tool_module._serialize_longterm_memory_log_arguments(
        {
            "operation": "recall",
            "query": "private memory query",
            "knowledge_query": "private knowledge query",
        }
    )
    result = process_single_tool_module._serialize_longterm_memory_log_result(
        json.dumps(
            {
                "items": [{"memory_id": 1, "content": "private memory body"}],
                "knowledge_base": [
                    {
                        "knowledge_base_id": 10,
                        "source_type": "user_knowledge",
                        "source": "private-file.md",
                        "content": "private knowledge body",
                        "truncated": False,
                        "llm_maintainable": False,
                    }
                ],
                "chat_history": [{"role": "user", "content": "private chat body"}],
            }
        )
    )

    argument_payload = json.loads(arguments)
    result_payload = json.loads(result)
    assert argument_payload["query_length"] == len("private memory query")
    assert argument_payload["knowledge_query_length"] == len("private knowledge query")
    assert "private memory query" not in arguments
    assert "private knowledge query" not in arguments
    assert "private memory body" not in result
    assert "private knowledge body" not in result
    assert "private chat body" not in result
    assert result_payload["knowledge_base"] == [
        {
            "knowledge_base_id": 10,
            "source_type": "user_knowledge",
            "truncated": False,
            "llm_maintainable": False,
        }
    ]


def test_final_recall_budget_keeps_structured_json_and_prioritizes_memory_items():
    raw = json.dumps(
        {
            "items": [
                {
                    "memory_id": 1,
                    "expected_version": 2,
                    "memory_key": "project",
                    "memory_type": "project",
                    "content": "memory-" * 400,
                }
            ],
            "knowledge_base": [
                {
                    "knowledge_base_id": 10,
                    "knowledge_base_name": "Managed",
                    "source_type": "managed_knowledge",
                    "source": "project.architecture",
                    "content": "knowledge-" * 400,
                    "truncated": False,
                    "llm_maintainable": True,
                    "knowledge_id": 20,
                    "knowledge_key": "project.architecture",
                    "knowledge_expected_version": 3,
                }
            ],
            "chat_history": [
                {
                    "role": "user",
                    "content": "chat-" * 400,
                    "session_id": "old-session",
                    "created_at": "2026-09-07 12:00:00",
                }
            ],
            "current_session_id": "session-1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    truncated, stats = truncate_longterm_memory_recall_result_for_budget(
        raw,
        context_window_k=4,
        budget_tokens=220,
    )
    payload = json.loads(truncated)

    assert stats.truncated_count == 1
    assert [item["memory_id"] for item in payload["items"]] == [1]
    assert payload["items"][0]["truncated"] is True
    assert (
        process_single_tool_module.truncate_tool_result_with_stats(
            truncated,
            4,
            limit_tokens=220,
        ).truncated
        is False
    )


def test_longterm_memory_recall_budget_truncation_preserves_json_and_memory_items():
    raw = json.dumps(
        {
            "items": [
                {
                    "memory_id": 1,
                    "expected_version": 2,
                    "memory_key": "project",
                    "memory_type": "project",
                    "content": "memory-" * 300,
                },
                {
                    "memory_id": 2,
                    "expected_version": 1,
                    "memory_key": "preference",
                    "memory_type": "preference",
                    "content": "preference-" * 300,
                },
            ],
            "knowledge_base": [
                {
                    "knowledge_base_id": 10,
                    "knowledge_base_name": "Managed",
                    "source_type": "managed_knowledge",
                    "source": "project.architecture",
                    "content": "knowledge-" * 400,
                    "truncated": False,
                    "llm_maintainable": True,
                    "knowledge_id": 20,
                    "knowledge_key": "project.architecture",
                    "knowledge_expected_version": 3,
                }
            ],
            "chat_history": [
                {
                    "role": "user",
                    "content": "history-" * 400,
                    "session_id": "old-session",
                    "created_at": "2026-09-01 10:00:00",
                }
            ],
            "current_session_id": "session-1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    truncated, stats = truncate_longterm_memory_recall_result_for_budget(
        raw,
        context_window_k=1,
        budget_tokens=350,
    )
    payload = json.loads(truncated)
    final_stats = process_single_tool_module.truncate_tool_result_with_stats(
        truncated,
        1,
        limit_tokens=350,
    )

    assert stats.truncated_count == 1
    assert final_stats.truncated is False
    assert [item["memory_id"] for item in payload["items"]] == [1, 2]
    assert all(item.get("truncated") is True for item in payload["items"])
    assert payload["truncated"] is True
    if payload["knowledge_base"]:
        managed = payload["knowledge_base"][0]
        if managed.get("truncated") is True:
            assert managed["llm_maintainable"] is False
            assert "knowledge_id" not in managed
            assert "knowledge_expected_version" not in managed


def test_budget_truncation_keeps_complete_managed_knowledge_writable_metadata():
    raw = json.dumps(
        {
            "items": [
                {
                    "memory_id": 1,
                    "expected_version": 1,
                    "memory_key": "large",
                    "memory_type": "project",
                    "content": "large-memory-" * 800,
                }
            ],
            "knowledge_base": [
                {
                    "knowledge_base_id": 10,
                    "knowledge_base_name": "Managed",
                    "source_type": "managed_knowledge",
                    "source": "project.architecture",
                    "content": "short managed content",
                    "truncated": False,
                    "llm_maintainable": True,
                    "knowledge_id": 20,
                    "knowledge_key": "project.architecture",
                    "knowledge_expected_version": 3,
                }
            ],
            "chat_history": [],
            "current_session_id": "session-1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    truncated, _stats = truncate_longterm_memory_recall_result_for_budget(
        raw,
        context_window_k=2,
        budget_tokens=500,
    )
    payload = json.loads(truncated)
    managed = payload["knowledge_base"][0]

    assert managed["content"] == "short managed content"
    assert managed["truncated"] is False
    assert managed["llm_maintainable"] is True
    assert managed["knowledge_id"] == 20
    assert managed["knowledge_expected_version"] == 3


def test_budget_truncation_never_drops_longterm_memory_items():
    raw = json.dumps(
        {
            "items": [
                {
                    "memory_id": 1,
                    "expected_version": 1,
                    "memory_key": "project-a",
                    "memory_type": "project",
                    "content": "first memory",
                },
                {
                    "memory_id": 2,
                    "expected_version": 1,
                    "memory_key": "project-b",
                    "memory_type": "project",
                    "content": "second memory",
                },
            ],
            "knowledge_base": [],
            "chat_history": [],
            "current_session_id": "session-1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    truncated, stats = truncate_longterm_memory_recall_result_for_budget(
        raw,
        context_window_k=1,
        budget_tokens=1,
    )
    assert stats.truncated_count == 1
    assert truncated == "cut"
