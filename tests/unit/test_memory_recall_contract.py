import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BaseBusinessException
from app.core.knowledge.results import (
    KnowledgeRecallItem,
    KnowledgeRecallResult,
    KnowledgeRecallSourceType,
)
from app.core.memory import MemoryRecallItem, MemoryRecallResult, MemoryRecallStatus
from app.core.memory.chat_history import ChatHistoryRecallItem, ChatHistoryRecallResult
from app.core.tools import longterm_memory as longterm_memory_module
from app.core.tools.longterm_memory import LongTermMemoryExecutor
from app.models.profile import ProfileConfig
from tests.memory_recall_support import memory_recall_payload


def _build_executor():
    executor = LongTermMemoryExecutor(project_root=".", uid="user-1")
    executor.set_config(ProfileConfig.model_validate({"memory": {"enabled": True}}))
    executor.set_runtime_context(db=SimpleNamespace(), profile=SimpleNamespace(id=7), session_id="session-1")
    return executor


def _patch_recall_services(monkeypatch, *, memory_result=None, chat_result=None, knowledge_result=None, fail=False):
    if fail:
        memory_recall = AsyncMock(side_effect=RuntimeError("memory source failed"))
        chat_recall = AsyncMock(side_effect=RuntimeError("chat source failed"))
        knowledge_recall = AsyncMock(side_effect=RuntimeError("knowledge source failed"))
    else:
        memory_recall = AsyncMock(return_value=memory_result or MemoryRecallResult(status=MemoryRecallStatus.EMPTY))
        chat_recall = AsyncMock(return_value=chat_result or ChatHistoryRecallResult())
        knowledge_recall = AsyncMock(return_value=knowledge_result or KnowledgeRecallResult())

    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: SimpleNamespace(recall=memory_recall))
    monkeypatch.setattr(
        longterm_memory_module,
        "_get_chat_history_recall_service",
        lambda: SimpleNamespace(recall=chat_recall),
    )
    monkeypatch.setattr(
        longterm_memory_module,
        "_get_knowledge_recall_service",
        lambda: SimpleNamespace(recall=knowledge_recall),
    )


@pytest.mark.asyncio
async def test_recall_output_matches_independent_fixture_contract(monkeypatch):
    executor = _build_executor()
    _patch_recall_services(
        monkeypatch,
        memory_result=MemoryRecallResult(
            status=MemoryRecallStatus.OK,
            items=(
                MemoryRecallItem(
                    memory_id=1,
                    memory_key="project",
                    content="fixture memory",
                    memory_type="project",
                    version=2,
                    updated_at=datetime(2026, 9, 1, 10, 0, 0),
                    source="llm_tool",
                ),
            ),
        ),
        chat_result=ChatHistoryRecallResult(
            items=(
                ChatHistoryRecallItem(
                    role="user",
                    content="fixture user history",
                    session_id="older-session",
                    created_at=datetime(2026, 9, 1, 9, 0, 0),
                ),
                ChatHistoryRecallItem(
                    role="assistant",
                    content="fixture assistant history",
                    session_id="older-session",
                    created_at=None,
                ),
            ),
        ),
        knowledge_result=KnowledgeRecallResult(
            items=(
                KnowledgeRecallItem(
                    knowledge_base_id=10,
                    knowledge_base_name="Managed",
                    source_type=KnowledgeRecallSourceType.MANAGED_KNOWLEDGE,
                    source="project.architecture",
                    content="fixture managed knowledge",
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
                    content="fixture user knowledge",
                    llm_maintainable=False,
                ),
            ),
        ),
    )
    expected = {
        "memories": [
            {
                "memory_id": 1,
                "expected_version": 2,
                "memory_key": "project",
                "memory_type": "project",
                "content": "fixture memory",
                "updated_at": "2026-09-01 10:00:00",
            }
        ],
        "current_session_id": "session-1",
        "knowledge_base": [
            {
                "knowledge_base_id": 10,
                "knowledge_base_name": "Managed",
                "source_type": "managed_knowledge",
                "source": "project.architecture",
                "content": "fixture managed knowledge",
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
                "content": "fixture user knowledge",
                "truncated": False,
                "llm_maintainable": False,
            },
        ],
        "chat_history": [
            {
                "role": "user",
                "content": "fixture user history",
                "session_id": "older-session",
                "created_at": "2026-09-01 09:00:00",
            },
            {
                "role": "assistant",
                "content": "fixture assistant history",
                "session_id": "older-session",
                "created_at": None,
            },
        ],
    }

    assert memory_recall_payload() == expected
    actual = json.loads(await executor.execute(operation="recall", query="fixture", knowledge_query="fixture document"))
    assert actual == expected
    assert "items" not in actual
    assert "items" not in memory_recall_payload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "empty",
        "source-exception",
        "business-exception",
        "unexpected-exception",
        "invalid-arguments",
        "missing-runtime",
        "missing-config",
    ],
)
async def test_empty_recall_fallbacks_have_fixed_minimal_shape(monkeypatch, scenario):
    executor = _build_executor()
    _patch_recall_services(monkeypatch, fail=scenario == "source-exception")

    if scenario == "business-exception":
        executor._recall = AsyncMock(side_effect=BaseBusinessException(default_message="business failure"))
    elif scenario == "unexpected-exception":
        executor._recall = AsyncMock(side_effect=RuntimeError("unexpected failure"))
    elif scenario == "missing-runtime":
        executor.set_runtime_context()
    elif scenario == "missing-config":
        executor.set_config(SimpleNamespace())

    arguments = {"operation": "recall"}
    if scenario != "invalid-arguments":
        arguments.update(query="fixture", knowledge_query="fixture document")
    expected = {"memories": [], "current_session_id": "session-1"}
    if scenario == "missing-runtime":
        expected = {"memories": [], "current_session_id": None}

    actual = json.loads(await executor.execute(**arguments))
    support_payload = memory_recall_payload(current_session_id=None, empty=True) if scenario == "missing-runtime" else memory_recall_payload(empty=True)
    assert support_payload == expected
    assert actual == expected


def test_memory_recall_fixture_builder_returns_independent_payloads():
    first = memory_recall_payload()
    second = memory_recall_payload()
    for key in ("memories", "knowledge_base", "chat_history"):
        assert first[key] is not second[key]
        assert first[key][0] is not second[key][0]

    first["memories"][0]["content"] = "changed memory"
    first["knowledge_base"][0]["content"] = "changed knowledge"
    first["chat_history"][0]["content"] = "changed history"
    assert second["memories"][0]["content"] == "fixture memory"
    assert second["knowledge_base"][0]["content"] == "fixture managed knowledge"
    assert second["chat_history"][0]["content"] == "fixture user history"

    other_session = memory_recall_payload(current_session_id="other-session")
    empty = memory_recall_payload(current_session_id=None, empty=True)
    assert other_session["current_session_id"] == "other-session"
    assert empty == {"memories": [], "current_session_id": None}
    assert second["current_session_id"] == "session-1"
    assert memory_recall_payload()["current_session_id"] == "session-1"
