import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.core.dispatcher as dispatcher_module
from app.core.dispatchers.memory import request as memory_request_module
from app.core.dispatchers.memory.recall import run_memory_recall_precheck
from app.core.dispatchers.memory.types import MemoryRecallContext
from app.core.memory import service_recall as memory_service_recall_module
from app.core.memory.identifiers import build_memory_vector_item_id
from app.core.memory.normalization import build_memory_content_hash
from app.core.retrieval.schemas import RetrievalHit
from app.core.tools import longterm_memory as longterm_memory_module
from app.core.tools.longterm_memory import MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME
from app.core.utils.tokenizer import estimate_tokens
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseProfileBinding,
    ManagedKnowledgeItem,
)
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemorySource,
    LongTermMemoryStore,
    LongTermMemoryType,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession


@pytest_asyncio.fixture
async def memory_recall_session_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'memory-recall-workflow.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    tables = [
        ModelChannel.__table__,
        PromptLibrary.__table__,
        Profile.__table__,
        ChatSession.__table__,
        Message.__table__,
        KnowledgeBase.__table__,
        KnowledgeBaseCollectionOwner.__table__,
        KnowledgeBaseProfileBinding.__table__,
        KnowledgeBaseDocument.__table__,
        ManagedKnowledgeItem.__table__,
        LongTermMemoryStore.__table__,
        LongTermMemoryRecord.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=tables))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _profile_config() -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {},
            "security": {},
            "tool": {},
            "other": {},
            "memory": {
                "enabled": True,
                "embedding_channel_id": 7,
                "embedding_model_id": "memory-embedding-model",
                "top_k": 2,
                "candidate_k": 4,
                "result_max_chars": 4000,
                "chat_history": {
                    "top_k": 2,
                    "candidate_k": 20,
                    "result_max_chars": 4000,
                },
            },
        }
    )


async def _seed_memory_recall_workflow(session_factory):
    cfg = _profile_config()
    async with session_factory() as db:
        db.add(
            ModelChannel(
                id=7,
                name="memory-embedding",
                api_key="enc:v1:test-key",
                base_url="https://embedding.invalid",
                model_ids=[
                    {
                        "model_id": "memory-embedding-model",
                        "usage": "EMBEDDING",
                        "protocol": "OPENAI_EMBEDDING",
                        "is_enabled": True,
                        "embedding_dimensions": 2,
                    }
                ],
            )
        )
        profile = Profile(id=1, uid="owner", name="memory-recall", configs=cfg.model_dump(mode="json"))
        db.add(profile)
        db.add(ChatSession(session_id="session-recall", uid="owner", profile_id=1))
        await db.flush()

        old_user = Message(
            session_id="session-recall",
            uid="owner",
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="I prefer VS Code as my editor for Python projects.",
            is_processed=True,
        )
        old_assistant = Message(
            session_id="session-recall",
            uid="owner",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="Noted your VS Code editor preference.",
            is_processed=True,
        )
        current_user = Message(
            session_id="session-recall",
            uid="owner",
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="Which editor do I prefer?",
            is_processed=True,
        )
        db.add_all([old_user, old_assistant, current_user])
        await db.flush()

        store = LongTermMemoryStore(
            uid="owner",
            active_embedding_channel_id=7,
            active_embedding_model_id="memory-embedding-model",
            active_embedding_dimensions=2,
            active_embedding_signature="memory-signature",
            active_embedding_revision=1,
            active_collection_name="memory-owner-active",
            index_revision=1,
            index_status=LongTermMemoryIndexStatus.READY,
        )
        db.add(store)
        memory_content = "The user's preferred Python editor is VS Code."
        record = LongTermMemoryRecord(
            uid="owner",
            memory_key="preferred-editor",
            memory_type=LongTermMemoryType.PREFERENCE,
            content=memory_content,
            content_token_count=estimate_tokens(memory_content),
            content_hash=build_memory_content_hash(memory_content),
            version=1,
            indexed_version=1,
            source=LongTermMemorySource.USER_API,
            source_id="seed-preference",
            source_message_id=old_user.id,
            is_active=True,
            index_status=LongTermMemoryRecordIndexStatus.READY,
        )
        db.add(record)
        await db.flush()
        record.vector_item_id = build_memory_vector_item_id(record.id, record.version)
        await db.commit()
        return profile, cfg, current_user.id, record.id, record.vector_item_id


@pytest.mark.asyncio
async def test_memory_recall_precheck_persists_executes_and_recovers_idempotently(
    memory_recall_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, cfg, current_user_id, memory_id, vector_item_id = await _seed_memory_recall_workflow(memory_recall_session_factory)
    assert current_user_id is not None
    assert memory_id is not None
    assert vector_item_id is not None

    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", memory_recall_session_factory)
    monkeypatch.setattr(longterm_memory_module, "AsyncSessionLocal", memory_recall_session_factory)

    async def identity_summary_checkpoint(_db, **kwargs):
        return kwargs["messages"]

    monkeypatch.setattr(memory_request_module, "apply_context_summary_checkpoint", identity_summary_checkpoint)

    embedding_calls = 0
    vector_query_calls = 0

    async def fake_embedding_runtime(*_args, **_kwargs):
        return object()

    async def fake_embed(_config, texts, **kwargs):
        nonlocal embedding_calls
        embedding_calls += 1
        assert texts == ["VS Code editor preference"]
        assert kwargs["dimensions"] == 2
        return [[0.2, 0.8]]

    async def fake_hybrid_query(collection_name, vector, query, limit):
        nonlocal vector_query_calls
        vector_query_calls += 1
        assert collection_name == "memory-owner-active"
        assert vector == [0.2, 0.8]
        assert query == "VS Code editor preference"
        assert limit == 4
        return [
            RetrievalHit(
                id=vector_item_id,
                content="ignored vector payload",
                metadata={
                    "uid": "owner",
                    "memory_id": memory_id,
                    "version": 1,
                    "embedding_revision": 1,
                },
                fusion_score=0.95,
            )
        ]

    monkeypatch.setattr(memory_service_recall_module, "load_embedding_runtime_config", fake_embedding_runtime)
    monkeypatch.setattr(memory_service_recall_module, "embed_texts_with_config", fake_embed)
    monkeypatch.setattr(memory_service_recall_module, "_hybrid_query_collection", fake_hybrid_query)

    llm_calls = 0

    async def fake_generate(**kwargs):
        nonlocal llm_calls
        llm_calls += 1
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == [MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME]
        return SimpleNamespace(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="recall-call-1",
                        name=MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
                        arguments={
                            "operation": "recall",
                            "query": "VS Code editor preference",
                            "knowledge_query": "VS Code editor preference",
                            "top_k": 2,
                        },
                    )
                ],
            ),
            usage={"prompt_tokens": 120, "cached_tokens": 20, "completion_tokens": 12},
        )

    monkeypatch.setattr(memory_request_module.LLMClient, "generate", fake_generate)

    events: list[dict[str, Any]] = []

    async def capture_event(event_payload: dict[str, Any]) -> None:
        events.append(event_payload)

    chat_channel_obj = SimpleNamespace(
        base_url="https://chat.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "chat-key",
    )
    model_entry = {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "OPENAI",
        "is_enabled": True,
        "context_window_k": 32,
        "max_tokens": 1024,
    }
    chat_params = {
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 1024,
        "chat_timeout": 60,
        "context_window_k": 32,
    }

    def build_context(db: AsyncSession) -> MemoryRecallContext:
        return MemoryRecallContext(
            db=db,
            uid="owner",
            session_id="session-recall",
            profile=profile,
            cfg=cfg,
            username="owner",
            messages=[InternalMessage(id=current_user_id, role=MessageRole.USER, content="Which editor do I prefer?")],
            turn_messages=[],
            current_user_boundary_message_id=current_user_id,
            upper_message_id=current_user_id,
            chat_channel=cfg.channel.chat_channel,
            chat_cursor_key="1:CHAT",
            chat_channel_obj=chat_channel_obj,
            model_entry=model_entry,
            channel_rule=SimpleNamespace(priority=1),
            chat_params=dict(chat_params),
            stream_event_callback=capture_event,
        )

    async with memory_recall_session_factory() as db:
        first_context = build_context(db)
        first = await run_memory_recall_precheck(first_context)
        assert first.status == "completed"
        assert [message.role for message in first.turn_messages] == [MessageRole.ASSISTANT, MessageRole.TOOL]
        tool_payload = json.loads(first.turn_messages[-1].content)
        assert tool_payload["items"][0]["memory_id"] == memory_id
        assert tool_payload["items"][0]["content"] == "The user's preferred Python editor is VS Code."
        assert any("VS Code" in item["content"] for item in tool_payload["chat_history"])

    async with memory_recall_session_factory() as db:
        rows = list((await db.execute(select(Message).where(Message.session_id == "session-recall").order_by(Message.id))).scalars().all())
        recalled_record = await db.get(LongTermMemoryRecord, memory_id)
        assert recalled_record is not None and recalled_record.last_recalled_at is not None
        recall_rows = [row for row in rows if row.dedupe_key and row.dedupe_key.startswith("memory-recall-")]
        assert [row.type for row in recall_rows] == [MessageType.TOOL_CALL, MessageType.TOOL_RESULT]
        persisted_count = len(rows)

    assert llm_calls == 1
    assert embedding_calls == 1
    assert vector_query_calls == 1
    assert [event["type"] for event in events if event["type"] != "llm_request_metadata"] == [
        "agent_loop_start",
        "turn_end",
        "tool_start",
        "tool_end",
    ]

    async def unexpected_generate(**_kwargs):
        raise AssertionError("dedupe recovery must not call the LLM again")

    monkeypatch.setattr(memory_request_module.LLMClient, "generate", unexpected_generate)
    events.clear()
    async with memory_recall_session_factory() as db:
        resumed_context = build_context(db)
        resumed = await run_memory_recall_precheck(resumed_context)
        assert resumed.status == "completed"
        assert [message.role for message in resumed.turn_messages] == [MessageRole.ASSISTANT, MessageRole.TOOL]
        resumed_payload = json.loads(resumed.turn_messages[-1].content)
        assert resumed_payload["items"][0]["memory_id"] == memory_id

    async with memory_recall_session_factory() as db:
        current_count = len(list((await db.execute(select(Message).where(Message.session_id == "session-recall"))).scalars().all()))
    assert current_count == persisted_count
    assert embedding_calls == 1
    assert vector_query_calls == 1
    assert events == []
