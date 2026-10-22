import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
from app.core.crud.session.session import session_crud
from app.core.dispatchers.memory import request as memory_request_module
from app.core.dispatchers.memory.recall import run_memory_recall_precheck
from app.core.dispatchers.memory.types import MemoryRecallContext
from app.core.memory import service_recall as memory_service_recall_module
from app.core.memory.identifiers import build_memory_vector_item_id
from app.core.memory.normalization import build_memory_content_hash
from app.core.prompts import (
    LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT,
    LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT,
)
from app.core.retrieval.schemas import RetrievalHit
from app.core.tools import longterm_memory as longterm_memory_module
from app.core.tools.longterm_memory import MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME
from app.core.utils.assistant_files import build_assistant_files_content
from app.core.utils.message_assembler import MessageAssembler
from app.core.utils.tokenizer import estimate_tokens
from app.models.channel import ChannelConfig, ChannelRule, ModelChannel
from app.models.channel_cursor import ChannelCursor
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
from app.models.message import (
    ImagePart,
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    Message,
    MessageContentFormat,
    MessageRole,
    MessageType,
    TextPart,
)
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan
from app.transformers import http_transport
from app.transformers.openai.chat_completions import OpenAIChatCompletionsTransformer
from tests.database_support import clone_sqlite_schema


@pytest_asyncio.fixture
async def memory_recall_session_factory(tmp_path, monkeypatch: pytest.MonkeyPatch):
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
        ChannelCursor.__table__,
        PromptLibrary.__table__,
        Profile.__table__,
        ChatSession.__table__,
        SessionTodoPlan.__table__,
        Message.__table__,
        KnowledgeBase.__table__,
        KnowledgeBaseCollectionOwner.__table__,
        KnowledgeBaseProfileBinding.__table__,
        KnowledgeBaseDocument.__table__,
        ManagedKnowledgeItem.__table__,
        LongTermMemoryStore.__table__,
        LongTermMemoryRecord.__table__,
    ]
    await clone_sqlite_schema(tmp_path / "memory-recall-workflow.db", tables=tables)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _profile_config() -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {"channel_id": 1, "model_id": "chat-model", "priority": 1, "weight": 1},
                    ],
                },
            },
            "security": {},
            "tool": {},
            "other": {},
            "memory": {
                "enabled": True,
                "embedding_channel_id": 7,
                "embedding_model_id": "memory-embedding-model",
                "top_k": 2,
                "candidate_k": 4,
                "chat_history": {
                    "top_k": 2,
                    "candidate_k": 20,
                },
            },
        }
    )


async def _seed_memory_recall_workflow(session_factory, *, chat_protocol: str = "OPENAI"):
    cfg = _profile_config()
    async with session_factory() as db:
        db.add(
            ModelChannel(
                id=1,
                name="memory-chat",
                api_key="enc:v1:test-key",
                base_url="https://chat.invalid",
                model_ids=[
                    {
                        "model_id": "chat-model",
                        "usage": "CHAT",
                        "protocol": chat_protocol,
                        "is_enabled": True,
                        "context_window_k": 32,
                        "max_tokens": 1024,
                    }
                ],
            )
        )
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
        db.add(
            ChatSession(
                session_id="session-recall",
                uid="owner",
                profile_id=1,
                llm_request_metadata={
                    "input_tokens": 100,
                    "input_tokens_source": "provider",
                    "context_window_tokens": 32768,
                    "max_output_tokens": 256,
                    "cached_tokens": 25,
                    "output_tokens": 10,
                    "total_input_tokens": 1000,
                    "total_cached_tokens": 250,
                    "total_output_tokens": 200,
                    "cache_hit_rate": 0.25,
                },
            )
        )
        await db.flush()
        db.add(
            SessionTodoPlan(
                session_id="session-recall",
                uid="owner",
                revision=8,
                todos=[
                    {"content": "Confirm VS Code preference", "status": "pending"},
                    {"content": "Review editor note", "status": "completed"},
                ],
            )
        )

        old_user = Message(
            session_id="session-recall",
            uid="owner",
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="I prefer VS Code as my editor for Python projects.",
            created_at=datetime(2026, 8, 4, 12, 31, 0),
            is_processed=True,
        )
        old_assistant = Message(
            session_id="session-recall",
            uid="owner",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="Noted your VS Code editor preference.",
            created_at=datetime(2026, 8, 4, 12, 32, 0),
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
            created_at=datetime(2026, 8, 4, 12, 30, 0),
            updated_at=datetime(2026, 8, 4, 12, 30, 0),
            is_active=True,
            index_status=LongTermMemoryRecordIndexStatus.READY,
        )
        db.add(record)
        await db.flush()
        record.vector_item_id = build_memory_vector_item_id(record.id, record.version)
        await db.commit()
        return profile, cfg, current_user.id, record.id, record.vector_item_id


@pytest.mark.parametrize("input_kind", ["text", "inline_image", "image_attachment"])
@pytest.mark.asyncio
async def test_memory_recall_precheck_persists_executes_and_recovers_idempotently(
    memory_recall_session_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    input_kind: str,
) -> None:
    profile, cfg, current_user_id, memory_id, vector_item_id = await _seed_memory_recall_workflow(memory_recall_session_factory)
    assert current_user_id is not None
    assert memory_id is not None
    assert vector_item_id is not None

    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", memory_recall_session_factory)
    monkeypatch.setattr(longterm_memory_module, "AsyncSessionLocal", memory_recall_session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "chat-key")

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
    captured_precheck_messages: list[list[InternalMessage]] = []

    async def fake_generate(**kwargs):
        nonlocal llm_calls
        llm_calls += 1
        captured_precheck_messages.append([message.model_copy(deep=True) for message in kwargs["messages"]])
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == [MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME]
        assert kwargs["max_tokens"] == 1024
        assert kwargs["temperature"] == 0.0
        assert "top_p" not in kwargs
        assert "reasoning_effort" not in kwargs
        return InternalResponse(
            model=kwargs["model_id"],
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

    chat_channel_obj = SimpleNamespace(
        id=1,
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
    history_user_content = "I prefer VS Code as my editor for Python projects."
    history_assistant_text = "Noted your VS Code editor preference."
    preserved_reasoning_content = "PRESERVED_INTERNAL_REASONING"
    preserved_provider_metadata = {"source": {"marker": "PRESERVED_PROVIDER_METADATA"}}
    summary_content = '<conversation_summary through_message_id="41">\nEarlier background: The user is working on Python projects.\n</conversation_summary>'
    todo_snapshot = '<current_session_todo_snapshot>{"revision":7,"todos":[{"content":"STALE_TODO_MUST_NOT_LEAK","status":"pending"}]}</current_session_todo_snapshot>'
    current_user_content = "Which editor do I prefer?"
    current_attachment_path = None
    if input_kind == "image_attachment":
        current_attachment_path = tmp_path / "current-user-image.png"
        Image.new("RGB", (2, 2), color="red").save(current_attachment_path)

    def build_context(db: AsyncSession) -> MemoryRecallContext:
        async def capture_event(event_payload: dict[str, Any]) -> None:
            events.append(event_payload)
            if event_payload.get("type") == "llm_request_metadata":
                updated = await session_crud.update_llm_request_metadata(
                    db,
                    session_id="session-recall",
                    uid="owner",
                    metadata=event_payload,
                    commit=False,
                )
                assert updated is True

        context_messages = [
            InternalMessage(role=MessageRole.USER, content=summary_content),
            InternalMessage(role=MessageRole.USER, content=history_user_content),
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content=build_assistant_files_content(
                    history_assistant_text,
                    [
                        {
                            "id": "private-file-token-must-not-leak",
                            "download_url": "https://download.invalid/private-file-token",
                        }
                    ],
                ),
                reasoning_content=preserved_reasoning_content,
                refusal="PRIVATE_REFUSAL_MUST_NOT_LEAK",
                provider_metadata=preserved_provider_metadata,
                environment_prompt="PRIVATE_ENVIRONMENT_MUST_NOT_LEAK",
                guidance_prompt="PRIVATE_GUIDANCE_MUST_NOT_LEAK",
                attachments=["PRIVATE_ATTACHMENT_MUST_NOT_LEAK"],
            ),
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="private-tool-call-id",
                        name="private_tool",
                        arguments={
                            "query": "PRIVATE_TOOL_ARGUMENT_MUST_NOT_LEAK",
                            "token": "PRIVATE_TOOL_TOKEN_MUST_NOT_LEAK",
                        },
                    )
                ],
            ),
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="private-tool-call-id",
                content=f'{{"status":"ok","result":"PRIVATE_TOOL_RESULT_MUST_NOT_LEAK","download_url":"https://download.invalid/tool-result"}}\n\n{todo_snapshot}',
            ),
        ]
        if input_kind == "inline_image":
            context_messages.append(
                InternalMessage.from_user_input(
                    content=[
                        TextPart(text=current_user_content),
                        ImagePart(image_url={"url": "data:image/png;base64,CURRENT_IMAGE_MUST_NOT_LEAK"}),
                    ]
                )
            )
        elif input_kind == "image_attachment":
            current_user_message = InternalMessage.from_user_input(
                content=[TextPart(text=current_user_content)],
                attachments=[str(current_attachment_path)],
            )
            context_messages.append(
                MessageAssembler.assemble(
                    current_user_message,
                    image_understanding=True,
                    is_history=False,
                )
            )
        else:
            context_messages.append(InternalMessage(role=MessageRole.USER, content=current_user_content))
        return MemoryRecallContext(
            db=db,
            uid="owner",
            session_id="session-recall",
            profile=profile,
            cfg=cfg,
            username="owner",
            messages=context_messages,
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
        source_current_message = first_context.messages[-1]
        first_context_messages_before = [message.model_dump(mode="json") for message in first_context.messages]
        first = await run_memory_recall_precheck(first_context)
        assert first.status == "completed"
        assert first.chat_params == chat_params
        assert first.session_total_input_tokens == 1000
        assert first.session_total_cached_tokens == 250
        assert [message.role for message in first.turn_messages] == [MessageRole.ASSISTANT, MessageRole.TOOL]
        tool_payload = json.loads(first.turn_messages[-1].content)
        assert tool_payload["items"][0]["memory_id"] == memory_id
        assert tool_payload["items"][0]["content"] == "The user's preferred Python editor is VS Code."
        assert tool_payload["items"][0]["updated_at"] == "2026-08-04 12:30:00"
        assert any("VS Code" in item["content"] for item in tool_payload["chat_history"])
        chat_history_by_content = {item["content"]: item for item in tool_payload["chat_history"]}
        assert chat_history_by_content[history_user_content]["role"] == MessageRole.USER.value
        assert chat_history_by_content[history_user_content]["created_at"] == "2026-08-04 12:31:00"
        assert chat_history_by_content[history_assistant_text]["role"] == MessageRole.ASSISTANT.value
        assert chat_history_by_content[history_assistant_text]["created_at"] == "2026-08-04 12:32:00"

    assert len(captured_precheck_messages) == 1
    precheck_messages = captured_precheck_messages[0]
    assert precheck_messages[0].role == MessageRole.SYSTEM
    assert precheck_messages[0].content == LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT
    assert sum(message.role == MessageRole.SYSTEM for message in precheck_messages) == 1
    assert all(not message.tool_calls and message.tool_call_id is None for message in precheck_messages)
    assert any(message.role == MessageRole.USER and message.content == history_user_content for message in precheck_messages)
    precheck_assistant = next(message for message in precheck_messages if message.role == MessageRole.ASSISTANT and message.content == history_assistant_text)
    source_assistant = next(message for message in first_context.messages if message.role == MessageRole.ASSISTANT and message.reasoning_content == preserved_reasoning_content)
    assert precheck_assistant.reasoning_content == source_assistant.reasoning_content == preserved_reasoning_content
    assert precheck_assistant.provider_metadata == source_assistant.provider_metadata == preserved_provider_metadata
    assert precheck_assistant.provider_metadata is not source_assistant.provider_metadata
    assert precheck_assistant.provider_metadata["source"] is not source_assistant.provider_metadata["source"]
    assert any(message.role == MessageRole.USER and message.content == summary_content for message in precheck_messages)
    todo_messages = [message for message in precheck_messages if message.role == MessageRole.USER and isinstance(message.content, str) and "current_session_todo" in message.content]
    assert len(todo_messages) == 1
    todo_payload = json.loads(todo_messages[0].content)
    assert todo_payload == {
        "current_session_todo": {
            "revision": 8,
            "todos": [
                {"content": "Confirm VS Code preference", "status": "pending"},
                {"content": "Review editor note", "status": "completed"},
            ],
        }
    }
    assert precheck_messages[-1].role == MessageRole.USER
    assert all(isinstance(message.content, str) for message in precheck_messages)
    assert all(not message.attachments for message in precheck_messages)
    assert precheck_messages[-1].content == current_user_content
    precheck_dump = "\n".join(message.model_dump_json(exclude_none=False) for message in precheck_messages)
    for forbidden in (
        "private-file-token-must-not-leak",
        "https://download.invalid/private-file-token",
        "PRIVATE_REFUSAL_MUST_NOT_LEAK",
        "PRIVATE_ENVIRONMENT_MUST_NOT_LEAK",
        "PRIVATE_GUIDANCE_MUST_NOT_LEAK",
        "PRIVATE_ATTACHMENT_MUST_NOT_LEAK",
        "PRIVATE_TOOL_ARGUMENT_MUST_NOT_LEAK",
        "PRIVATE_TOOL_TOKEN_MUST_NOT_LEAK",
        "PRIVATE_TOOL_RESULT_MUST_NOT_LEAK",
        "https://download.invalid/tool-result",
        "STALE_TODO_MUST_NOT_LEAK",
        "CURRENT_IMAGE_MUST_NOT_LEAK",
        "data:image/",
        "image_url",
        "[系统提示,此处不是用户说的话]",
        "[系统提示结束]",
    ):
        assert forbidden not in precheck_dump
    if current_attachment_path is not None:
        assert str(current_attachment_path) not in precheck_dump
    if input_kind in {"inline_image", "image_attachment"}:
        assert isinstance(source_current_message.content, list)
        assert any(isinstance(part, ImagePart) for part in source_current_message.content)
        provider_current_message = OpenAIChatCompletionsTransformer.to_provider([source_current_message])[0]
        assert any(part.get("type") == "image_url" for part in provider_current_message["content"])
    original_message_count = len(first_context_messages_before)
    first_context_messages_after = [message.model_dump(mode="json") for message in first_context.messages[:original_message_count]]
    assert first_context_messages_after == first_context_messages_before
    appended_messages = first_context.messages[original_message_count:]
    assert len(appended_messages) == 2
    assert [message.model_dump(mode="json") for message in appended_messages] == [message.model_dump(mode="json") for message in first.turn_messages]

    async with memory_recall_session_factory() as db:
        rows = list((await db.execute(select(Message).where(Message.session_id == "session-recall").order_by(Message.id))).scalars().all())
        recalled_record = await db.get(LongTermMemoryRecord, memory_id)
        assert recalled_record is not None and recalled_record.last_recalled_at is not None
        assert recalled_record.content == "The user's preferred Python editor is VS Code."
        assert recalled_record.updated_at == datetime(2026, 8, 4, 12, 30, 0)
        chat_session = await db.get(ChatSession, "session-recall")
        assert chat_session is not None
        assert chat_session.llm_request_metadata is not None
        assert chat_session.llm_request_metadata["input_tokens"] == 100
        assert chat_session.llm_request_metadata["input_tokens_source"] == "provider"
        assert chat_session.llm_request_metadata["cached_tokens"] == 25
        assert chat_session.llm_request_metadata["output_tokens"] == 10
        assert chat_session.llm_request_metadata["total_input_tokens"] == 1000
        assert chat_session.llm_request_metadata["total_cached_tokens"] == 250
        assert chat_session.llm_request_metadata["cache_hit_rate"] == 0.25
        assert chat_session.llm_request_metadata["total_output_tokens"] == 200
        recall_rows = [row for row in rows if row.dedupe_key and row.dedupe_key.startswith("memory-recall-")]
        assert [row.type for row in recall_rows] == [MessageType.TOOL_CALL, MessageType.TOOL_RESULT]
        persisted_tool_message = InternalMessage.model_validate_json(recall_rows[1].content)
        assert json.loads(persisted_tool_message.content) == tool_payload
        persisted_count = len(rows)

    assert llm_calls == 1
    assert embedding_calls == 1
    assert vector_query_calls == 1
    metadata_events = [event for event in events if event["type"] == "llm_request_metadata"]
    assert len(metadata_events) == 2
    assert metadata_events[-1]["input_tokens"] == 120
    assert metadata_events[-1]["input_tokens_source"] == "provider"
    assert metadata_events[-1]["cached_tokens"] == 20
    assert metadata_events[-1]["output_tokens"] == 12
    assert metadata_events[-1]["total_input_tokens"] == 1000
    assert metadata_events[-1]["total_cached_tokens"] == 250
    assert metadata_events[-1]["cache_hit_rate"] == 0.25
    assert metadata_events[-1]["total_output_tokens"] == 200
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
        assert resumed_payload == tool_payload

    async with memory_recall_session_factory() as db:
        current_count = len(list((await db.execute(select(Message).where(Message.session_id == "session-recall"))).scalars().all()))
    assert current_count == persisted_count
    assert llm_calls == 1
    assert embedding_calls == 1
    assert vector_query_calls == 1
    assert events == []


@pytest.mark.parametrize("protocol", ["openai", "deepseek"])
@pytest.mark.parametrize("dispatcher_mode", ["non_stream", "stream"])
@pytest.mark.parametrize("first_response_kind", ["empty", "reasoning_only"])
@pytest.mark.asyncio
async def test_memory_recall_retries_empty_response_before_recall_and_persistence(
    memory_recall_session_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    dispatcher_mode: str,
    first_response_kind: str,
) -> None:
    profile, cfg, current_user_id, memory_id, vector_item_id = await _seed_memory_recall_workflow(
        memory_recall_session_factory,
        chat_protocol=protocol.upper(),
    )
    assert current_user_id is not None
    assert memory_id is not None
    assert vector_item_id is not None

    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", memory_recall_session_factory)
    monkeypatch.setattr(longterm_memory_module, "AsyncSessionLocal", memory_recall_session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "chat-key")

    async def fake_embedding_runtime(*_args, **_kwargs):
        return object()

    async def fake_embed(_config, texts, **kwargs):
        assert texts == ["VS Code editor preference"]
        assert kwargs["dimensions"] == 2
        return [[0.2, 0.8]]

    async def fake_hybrid_query(collection_name, vector, query, limit):
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

    recall_arguments = {
        "operation": "recall",
        "query": "VS Code editor preference",
        "knowledge_query": "VS Code editor preference",
        "top_k": 2,
    }
    invalid_reasoning = "FIRST_INVALID_REASONING_MUST_NOT_LEAK"
    captured_payloads: list[dict[str, Any]] = []

    def build_response(request_index: int) -> dict[str, Any]:
        if request_index == 0:
            message: dict[str, Any] = {"role": "assistant", "content": None}
            if first_response_kind == "reasoning_only":
                message["reasoning_content"] = invalid_reasoning
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "recall-call-valid",
                        "type": "function",
                        "function": {
                            "name": MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
                            "arguments": json.dumps(recall_arguments),
                        },
                    }
                ],
            }
        return {
            "id": f"chatcmpl-memory-recall-{request_index}",
            "object": "chat.completion",
            "model": "chat-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if request_index else "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132},
        }

    async def capture_post_json(*_args, payload, **_kwargs):
        request_index = len(captured_payloads)
        captured_payloads.append(deepcopy(payload))
        return build_response(request_index)

    async def capture_stream(*_args, payload, **kwargs):
        request_index = len(captured_payloads)
        captured_payloads.append(deepcopy(payload))
        if request_index == 0:
            delta = {"role": "assistant"}
            if first_response_kind == "reasoning_only":
                delta["reasoning_content"] = invalid_reasoning
            events = [
                {"id": "chatcmpl-first", "model": "chat-model", "choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]},
            ]
        else:
            raw_tool_call = build_response(request_index)["choices"][0]["message"]["tool_calls"][0]
            events = [
                {
                    "id": "chatcmpl-second",
                    "model": "chat-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": [{"index": 0, **raw_tool_call}]},
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132},
                },
            ]
        for raw_event in events:
            normalized, _has_payload = kwargs["normalize_event"](raw_event)
            if normalized is not None:
                yield normalized

    monkeypatch.setattr(OpenAIChatCompletionsTransformer, "_post_json", capture_post_json)
    monkeypatch.setattr(OpenAIChatCompletionsTransformer, "_stream_sse_json", capture_stream)
    monkeypatch.setattr(http_transport, "post_json", capture_post_json)
    monkeypatch.setattr(http_transport, "stream_sse_json", capture_stream)

    chat_channel_obj = SimpleNamespace(
        id=1,
        base_url="https://chat.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "chat-key",
    )
    model_entry = {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": protocol.upper(),
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
    original_messages = [
        InternalMessage(role=MessageRole.USER, content="I prefer VS Code as my editor for Python projects."),
        InternalMessage(role=MessageRole.ASSISTANT, content="Noted your VS Code editor preference."),
        InternalMessage(role=MessageRole.USER, content="Which editor do I prefer?"),
    ]
    original_messages_before = [message.model_dump(mode="json") for message in original_messages]

    async with memory_recall_session_factory() as db:
        context = MemoryRecallContext(
            db=db,
            uid="owner",
            session_id="session-recall",
            profile=profile,
            cfg=cfg,
            username="owner",
            messages=original_messages,
            turn_messages=[],
            current_user_boundary_message_id=current_user_id,
            upper_message_id=current_user_id,
            chat_channel=cfg.channel.chat_channel,
            chat_cursor_key="1:CHAT",
            chat_channel_obj=chat_channel_obj,
            model_entry=model_entry,
            channel_rule=SimpleNamespace(priority=1),
            chat_params=chat_params,
            dispatcher_mode=dispatcher_mode,
        )
        result = await run_memory_recall_precheck(context)
        assert result.status == "completed"
        assert [message.role for message in result.turn_messages] == [MessageRole.ASSISTANT, MessageRole.TOOL]
        assert result.turn_messages[0].reasoning_content is None
        assert result.turn_messages[0].tool_calls is not None
        assert len(result.turn_messages[0].tool_calls) == 1
        assert result.turn_messages[0].tool_calls[0].arguments == recall_arguments
        tool_payload = json.loads(result.turn_messages[-1].content)
        assert tool_payload["items"][0]["memory_id"] == memory_id
        assert tool_payload["items"][0]["content"] == "The user's preferred Python editor is VS Code."

        assert [message.model_dump(mode="json") for message in original_messages[: len(original_messages_before)]] == original_messages_before
        appended_messages = original_messages[len(original_messages_before) :]
        assert len(appended_messages) == 2
        assert [message.model_dump(mode="json") for message in appended_messages] == [message.model_dump(mode="json") for message in result.turn_messages]

    assert len(captured_payloads) == 2
    assert all(payload["tool_choice"] == "auto" for payload in captured_payloads)
    assert [payload["stream"] for payload in captured_payloads] == [dispatcher_mode == "stream"] * 2
    assert captured_payloads[1]["messages"] == [
        *captured_payloads[0]["messages"],
        {"role": "user", "content": LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT},
    ]
    assert invalid_reasoning not in json.dumps(captured_payloads[1], ensure_ascii=False)

    async with memory_recall_session_factory() as db:
        rows = list((await db.execute(select(Message).where(Message.session_id == "session-recall").order_by(Message.id))).scalars().all())
        recall_rows = [row for row in rows if row.dedupe_key and row.dedupe_key.startswith("memory-recall-")]
        assert [row.type for row in recall_rows] == [MessageType.TOOL_CALL, MessageType.TOOL_RESULT]
        persisted_assistant = InternalMessage.model_validate_json(recall_rows[0].content)
        assert persisted_assistant.reasoning_content is None
        assert len(persisted_assistant.tool_calls or []) == 1
        assert persisted_assistant.tool_calls[0].arguments == recall_arguments
        assert invalid_reasoning not in "\n".join(row.content or "" for row in rows)
        recalled_record = await db.get(LongTermMemoryRecord, memory_id)
        assert recalled_record is not None and recalled_record.last_recalled_at is not None
        assert recalled_record.content == "The user's preferred Python editor is VS Code."


def _build_reasoning_provider_metadata(
    protocol: str,
    *,
    channel_id: int,
    model_id: str,
    reasoning_text: str,
    include_response_reasoning: bool,
) -> dict[str, Any]:
    metadata_protocol = "openai_chat_completions" if protocol == "openai" else protocol
    metadata: dict[str, Any] = {
        "protocol": metadata_protocol,
        "source": {"channel_id": channel_id, "model_id": model_id, "protocol": protocol},
        "response": {
            "id": "source-response-id",
            "status": "completed",
            "provider_marker": "SOURCE_RESPONSE_METADATA",
        },
    }
    if protocol in {"deepseek", "openai"}:
        metadata["message"] = {
            "reasoning": reasoning_text,
            "provider_marker": "SOURCE_MESSAGE_METADATA",
        }
    else:
        metadata["output"] = [
            {
                "type": "reasoning",
                "id": "source-reasoning-id",
                "content": ([{"type": "reasoning_text", "text": reasoning_text}] if include_response_reasoning else []),
                "summary": [{"type": "summary_text", "text": "SUMMARY_MUST_NOT_BE_SENT"}],
                "encrypted_content": "ENCRYPTED_MUST_NOT_BE_SENT",
            },
            {"type": "unknown_reasoning", "text": "UNKNOWN_MUST_NOT_BE_SENT"},
        ]
    return metadata


@pytest.mark.parametrize("target_protocol", ["deepseek", "deepseek_responses"])
@pytest.mark.parametrize(
    ("source_protocol", "source_kind"),
    [
        ("deepseek", "chat"),
        ("openai", "chat"),
        ("deepseek_responses", "responses"),
        ("openai_responses", "responses"),
        ("openai_responses", "summary_only"),
    ],
)
@pytest.mark.asyncio
async def test_memory_request_replays_only_recoverable_provider_reasoning(
    memory_recall_session_factory,
    monkeypatch: pytest.MonkeyPatch,
    source_protocol: str,
    source_kind: str,
    target_protocol: str,
) -> None:
    profile, cfg, current_user_id, _memory_id, _vector_item_id = await _seed_memory_recall_workflow(memory_recall_session_factory)
    assert current_user_id is not None

    chat_reasoning = "CHAT_REASONING_SECRET"
    responses_reasoning = "RESPONSES_REASONING_SECRET"
    summary_text = "OPENAI_RESPONSES_SUMMARY_NOT_REASONING"
    include_response_reasoning = source_kind == "responses"
    source_reasoning = chat_reasoning if source_kind == "chat" else responses_reasoning
    stored_reasoning = chat_reasoning if source_kind == "chat" else summary_text if source_protocol == "openai_responses" else responses_reasoning
    expected_reasoning = None if source_kind == "summary_only" else source_reasoning
    source_metadata = _build_reasoning_provider_metadata(
        source_protocol,
        channel_id=1,
        model_id="chat-model",
        reasoning_text=source_reasoning,
        include_response_reasoning=include_response_reasoning,
    )

    history_assistant_text = "Noted your VS Code editor preference."
    async with memory_recall_session_factory() as db:
        history_assistant = (
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.session_id == "session-recall",
                        Message.role == MessageRole.ASSISTANT,
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .first()
        )
        assert history_assistant is not None and history_assistant.id is not None
        history_assistant_id = int(history_assistant.id)
        history_assistant.reasoning_content = stored_reasoning
        history_assistant.provider_metadata = source_metadata
        await db.commit()

        context_messages = await dispatcher_module.ContextManager.get_messages(
            db,
            session_id="session-recall",
            uid="owner",
            profile=profile,
            current_message="Which editor do I prefer?",
            context_window_k=32,
        )
        context = MemoryRecallContext(
            db=db,
            uid="owner",
            session_id="session-recall",
            profile=profile,
            cfg=cfg,
            username="owner",
            messages=context_messages,
            turn_messages=[],
            current_user_boundary_message_id=current_user_id,
            upper_message_id=current_user_id,
            chat_channel=cfg.channel.chat_channel,
            chat_cursor_key="1:CHAT",
            chat_channel_obj=SimpleNamespace(
                id=1,
                base_url="https://chat.invalid",
                http_proxy=None,
                get_decrypted_api_key=lambda: "chat-key",
            ),
            model_entry={
                "model_id": "chat-model",
                "usage": "CHAT",
                "protocol": target_protocol.upper(),
                "is_enabled": True,
                "context_window_k": 32,
                "max_tokens": 1024,
            },
            channel_rule=SimpleNamespace(priority=1),
            chat_params={
                "temperature": 0.2,
                "top_p": 0.9,
                "max_tokens": 1024,
                "chat_timeout": 60,
                "context_window_k": 32,
            },
            auxiliary_channel=cfg.channel.resolve_auxiliary_channel(),
            auxiliary_channel_obj=SimpleNamespace(
                id=1,
                base_url="https://chat.invalid",
                http_proxy=None,
                get_decrypted_api_key=lambda: "chat-key",
            ),
            auxiliary_model_entry={
                "model_id": "chat-model",
                "usage": "CHAT",
                "protocol": target_protocol.upper(),
                "is_enabled": True,
                "context_window_k": 32,
                "max_tokens": 1024,
            },
            auxiliary_channel_rule=SimpleNamespace(priority=1),
            auxiliary_params={
                "temperature": 0.2,
                "top_p": 0.9,
                "max_tokens": 1024,
                "chat_timeout": 60,
                "context_window_k": 32,
            },
        )
        context_messages_before = [message.model_dump(mode="json") for message in context.messages]

        captured_payloads: list[dict[str, Any]] = []

        async def capture_post_json(*, payload, **_kwargs):
            captured_payloads.append(deepcopy(payload))
            if target_protocol == "deepseek":
                return {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "chat-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            return {
                "id": "resp-test",
                "object": "response",
                "status": "completed",
                "model": "chat-model",
                "output": [
                    {
                        "id": "msg-test",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        monkeypatch.setattr(http_transport, "post_json", capture_post_json)

        request_sources = [(1, "chat-model"), (2, "chat-model"), (1, "other-model"), (1, "chat-model")]
        for channel_id, model_id in request_sources:
            context.auxiliary_channel_obj.id = channel_id
            context.auxiliary_model_entry["model_id"] = model_id
            request_messages, metadata, _response_id = await memory_request_module.prepare_request_messages(
                context,
                context.messages,
                is_main_context=True,
            )
            assert all(message.role != MessageRole.TOOL and not message.tool_calls for message in request_messages)
            prepared_assistant = next(message for message in request_messages if message.role == MessageRole.ASSISTANT and message.content == history_assistant_text)
            assert prepared_assistant.reasoning_content == stored_reasoning
            assert prepared_assistant.provider_metadata == source_metadata
            await memory_request_module.generate(context, request_messages, metadata)

        assert len(captured_payloads) == 4
        for index, payload in enumerate(captured_payloads):
            is_original_source = index in {0, 3}
            request_reasoning = expected_reasoning if is_original_source else None
            payload_text = json.dumps(payload, ensure_ascii=False)
            assert history_assistant_text in payload_text
            assert MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME in payload_text
            for forbidden in (
                "SUMMARY_MUST_NOT_BE_SENT",
                "ENCRYPTED_MUST_NOT_BE_SENT",
                "UNKNOWN_MUST_NOT_BE_SENT",
                "OPENAI_RESPONSES_SUMMARY_NOT_REASONING",
            ):
                assert forbidden not in payload_text

            if target_protocol == "deepseek":
                assistant_payload = next(message for message in payload["messages"] if message.get("role") == "assistant" and message.get("content") == history_assistant_text)
                assert assistant_payload.get("reasoning_content", "") == (request_reasoning or "")
            else:
                request_items = [item for item in payload["input"] if isinstance(item, dict)]
                reasoning_items = [item for item in request_items if item.get("type") == "reasoning"]
                if request_reasoning is None:
                    assert reasoning_items == [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "(thinking unavailable)"}]}]
                else:
                    assert len(reasoning_items) == 1
                    assert request_reasoning in json.dumps(reasoning_items, ensure_ascii=False)

        db.expire_all()
        persisted_assistant = await db.get(Message, history_assistant_id)
        assert persisted_assistant is not None
        assert persisted_assistant.reasoning_content == stored_reasoning
        assert persisted_assistant.provider_metadata == source_metadata
        assert [message.model_dump(mode="json") for message in context.messages] == context_messages_before


@pytest.mark.parametrize("dispatcher_mode", ["non_stream", "stream"])
@pytest.mark.parametrize("failure_finish_reason", ["length", None, "incomplete"])
@pytest.mark.parametrize("fallback_exhausted", [False, True])
@pytest.mark.asyncio
async def test_memory_recall_output_budget_falls_back_without_mutating_main_context(
    memory_recall_session_factory,
    monkeypatch: pytest.MonkeyPatch,
    dispatcher_mode: str,
    failure_finish_reason: str | None,
    fallback_exhausted: bool,
) -> None:
    profile, cfg, current_user_id, memory_id, vector_item_id = await _seed_memory_recall_workflow(memory_recall_session_factory)
    assert current_user_id is not None
    assert memory_id is not None
    assert vector_item_id is not None

    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", memory_recall_session_factory)
    monkeypatch.setattr(longterm_memory_module, "AsyncSessionLocal", memory_recall_session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "chat-key")

    cfg.channel.auxiliary_channel = ChannelConfig(
        chat_timeout=17,
        rules=[
            ChannelRule(
                channel_id=2,
                model_id="aux-primary-model",
                priority=1,
                weight=1,
                reasoning_effort="high",
            ),
            ChannelRule(
                channel_id=3,
                model_id="aux-fallback-model",
                priority=2,
                weight=1,
                reasoning_effort=None,
            ),
        ],
    )
    profile.configs = cfg.model_dump(mode="json")
    large_memory_content = "VS Code is the preferred Python editor. " * 120 + "\nMEMORY_CONTENT_TAIL_MARKER"
    assert len(large_memory_content) > 4000

    async with memory_recall_session_factory() as db:
        for channel_id, name, model_id, context_window_k, max_tokens in (
            (2, "memory-aux-primary", "aux-primary-model", 4, 2048),
            (3, "memory-aux-fallback", "aux-fallback-model", 6, 4096),
        ):
            db.add(
                ModelChannel(
                    id=channel_id,
                    name=name,
                    api_key="enc:v1:test-key",
                    base_url=f"https://{name}.invalid",
                    model_ids=[
                        {
                            "model_id": model_id,
                            "usage": "CHAT",
                            "protocol": "OPENAI",
                            "is_enabled": True,
                            "context_window_k": context_window_k,
                            "max_tokens": max_tokens,
                        }
                    ],
                )
            )
        stored_profile = await db.get(Profile, profile.id)
        stored_record = await db.get(LongTermMemoryRecord, memory_id)
        assert stored_profile is not None
        assert stored_record is not None
        stored_profile.configs = profile.configs
        stored_record.content = large_memory_content
        stored_record.content_token_count = estimate_tokens(large_memory_content)
        stored_record.content_hash = build_memory_content_hash(large_memory_content)
        await db.commit()

    embedded_texts: list[str] = []
    vector_queries: list[str] = []

    async def fake_embedding_runtime(*_args, **_kwargs):
        return object()

    async def fake_embed(_config, texts, **kwargs):
        embedded_texts.extend(texts)
        assert kwargs["dimensions"] == 2
        return [[0.2, 0.8]]

    async def fake_hybrid_query(collection_name, vector, query, limit):
        vector_queries.append(query)
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

    primary_arguments = {
        "operation": "recall",
        "query": "PRIMARY_OVER_BUDGET_CALL_MUST_NOT_EXECUTE",
        "knowledge_query": "PRIMARY_OVER_BUDGET_CALL_MUST_NOT_EXECUTE",
        "top_k": 2,
    }
    accepted_arguments = {
        "operation": "recall",
        "query": "VS Code editor preference",
        "knowledge_query": "VS Code editor preference",
        "top_k": 2,
    }
    captured_requests: list[dict[str, Any]] = []

    async def fake_generate(**kwargs):
        request_index = len(captured_requests)
        captured_requests.append(
            {
                "channel_id": kwargs["channel_id"],
                "model_id": kwargs["model_id"],
                "max_tokens": kwargs["max_tokens"],
                "timeout": kwargs["timeout"],
                "reasoning_effort": kwargs.get("reasoning_effort"),
                "has_reasoning_effort": "reasoning_effort" in kwargs,
                "messages": [message.model_copy(deep=True) for message in kwargs["messages"]],
            }
        )
        if request_index == 0:
            finish_reason = failure_finish_reason
            completion_tokens = kwargs["max_tokens"] if finish_reason in {None, "incomplete"} else 8
            arguments = primary_arguments
            call_id = "over-budget-primary-call"
        elif fallback_exhausted:
            finish_reason = "incomplete"
            completion_tokens = kwargs["max_tokens"]
            arguments = accepted_arguments
            call_id = "over-budget-fallback-call"
        else:
            finish_reason = "tool_calls"
            completion_tokens = 12
            arguments = accepted_arguments
            call_id = "accepted-fallback-call"

        return InternalResponse(
            model=kwargs["model_id"],
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id=call_id,
                        name=MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
                        arguments=arguments,
                    )
                ],
            ),
            usage={
                "prompt_tokens": 120,
                "completion_tokens": completion_tokens,
                "total_tokens": 120 + completion_tokens,
            },
            finish_reason=finish_reason,
        )

    monkeypatch.setattr(memory_request_module.LLMClient, "generate", fake_generate)
    monkeypatch.setattr(memory_request_module.LLMClient, "generate_with_stream_callback", fake_generate)

    main_channel_obj = SimpleNamespace(
        id=1,
        base_url="https://chat.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "chat-key",
    )
    main_model_entry = {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "OPENAI",
        "is_enabled": True,
        "context_window_k": 32,
        "max_tokens": 1024,
    }
    main_channel_rule = SimpleNamespace(priority=1)
    main_chat_params = {
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 1024,
        "chat_timeout": 60,
        "context_window_k": 32,
    }
    original_messages = [
        InternalMessage(
            role=MessageRole.USER,
            content="I prefer VS Code as my editor for Python projects.",
            environment_prompt="MAIN_HISTORY_ENVIRONMENT_MUST_REMAIN",
        ),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="Noted your VS Code editor preference.",
            guidance_prompt="MAIN_HISTORY_GUIDANCE_MUST_REMAIN",
        ),
        InternalMessage(
            role=MessageRole.USER,
            content="Which editor do I prefer?",
            environment_prompt="MAIN_REQUEST_ENVIRONMENT_MUST_REMAIN",
        ),
    ]
    original_messages_before = [message.model_dump(mode="json") for message in original_messages]
    main_tools = [
        {
            "type": "function",
            "function": {
                "name": "main-chat-tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    events: list[dict[str, Any]] = []

    async with memory_recall_session_factory() as db:

        async def capture_event(event_payload: dict[str, Any]) -> None:
            events.append(deepcopy(event_payload))
            if event_payload.get("type") == "llm_request_metadata":
                updated = await session_crud.update_llm_request_metadata(
                    db,
                    session_id="session-recall",
                    uid="owner",
                    metadata=event_payload,
                    commit=False,
                )
                assert updated is True

        context = MemoryRecallContext(
            db=db,
            uid="owner",
            session_id="session-recall",
            profile=profile,
            cfg=cfg,
            username="owner",
            messages=original_messages,
            turn_messages=[],
            current_user_boundary_message_id=current_user_id,
            upper_message_id=current_user_id,
            chat_channel=cfg.channel.chat_channel,
            chat_cursor_key="1:CHAT",
            chat_channel_obj=main_channel_obj,
            model_entry=main_model_entry,
            channel_rule=main_channel_rule,
            chat_params=dict(main_chat_params),
            auxiliary_channel=cfg.channel.auxiliary_channel,
            dispatcher_mode=dispatcher_mode,
            stream_event_callback=capture_event,
            main_tools=main_tools,
        )
        result = await run_memory_recall_precheck(context)
        await db.commit()

        assert len(captured_requests) == 2
        assert [(request["channel_id"], request["model_id"], request["max_tokens"], request["timeout"]) for request in captured_requests] == [
            (2, "aux-primary-model", 2048, 17),
            (3, "aux-fallback-model", 4096, 17),
        ]
        assert captured_requests[0]["reasoning_effort"] == "high"
        assert captured_requests[0]["has_reasoning_effort"] is True
        assert captured_requests[1]["reasoning_effort"] is None
        assert captured_requests[1]["has_reasoning_effort"] is False
        assert all(not any(message.role == MessageRole.USER and message.content == LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT for message in request["messages"]) for request in captured_requests)

        assert context.chat_channel is cfg.channel.chat_channel
        assert context.chat_channel_obj is main_channel_obj
        assert context.model_entry == main_model_entry
        assert context.channel_rule is main_channel_rule
        assert context.chat_params == main_chat_params
        assert context.main_tools == main_tools
        assert [message.model_dump(mode="json") for message in context.messages[: len(original_messages_before)]] == original_messages_before

        if fallback_exhausted:
            assert result.status == "failed"
            assert result.error_type == "output_budget_exhausted"
            assert result.turn_messages == []
            assert embedded_texts == []
            assert vector_queries == []
        else:
            assert result.status == "completed"
            assert [message.role for message in result.turn_messages] == [MessageRole.ASSISTANT, MessageRole.TOOL]
            assert result.turn_messages[0].tool_calls[0].id == "accepted-fallback-call"
            tool_payload = json.loads(result.turn_messages[-1].content)
            assert tool_payload["items"][0]["memory_id"] == memory_id
            assert tool_payload["items"][0]["content"] == large_memory_content
            assert embedded_texts == ["VS Code editor preference"]
            assert vector_queries == ["VS Code editor preference"]

    request_metadata = [event_payload for event_payload in events if event_payload.get("type") == "llm_request_metadata" and event_payload.get("input_tokens_source") == "estimated"]
    assert [(item["channel_id"], item["model_id"], item["context_window_tokens"], item["max_output_tokens"]) for item in request_metadata] == [
        (2, "aux-primary-model", 4000, 2048),
        (3, "aux-fallback-model", 6000, 4096),
    ]

    async with memory_recall_session_factory() as db:
        rows = list((await db.execute(select(Message).where(Message.session_id == "session-recall").order_by(Message.id))).scalars().all())
        recall_rows = [row for row in rows if row.dedupe_key and row.dedupe_key.startswith("memory-recall-")]
        stored_record = await db.get(LongTermMemoryRecord, memory_id)
        session = await db.get(ChatSession, "session-recall")
        cursor_rows = list((await db.execute(select(ChannelCursor))).scalars().all())
        assert stored_record is not None
        assert session is not None and session.llm_request_metadata is not None
        assert session.llm_request_metadata["input_tokens"] == 100
        assert session.llm_request_metadata["input_tokens_source"] == "provider"
        assert session.llm_request_metadata["cached_tokens"] == 25
        assert session.llm_request_metadata["output_tokens"] == 10
        assert session.llm_request_metadata["total_input_tokens"] == 1000
        assert session.llm_request_metadata["total_cached_tokens"] == 250
        assert session.llm_request_metadata["total_output_tokens"] == 200
        assert session.llm_request_metadata["cache_hit_rate"] == 0.25
        assert {row.cursor_key for row in cursor_rows} == {
            "1:CHAT:AUXILIARY:1",
            "1:CHAT:AUXILIARY:2",
        }
        assert not any(row.cursor_key == "1:CHAT:1" for row in cursor_rows)

        persisted_content = "\n".join(row.content or "" for row in rows)
        assert "PRIMARY_OVER_BUDGET_CALL_MUST_NOT_EXECUTE" not in persisted_content
        if fallback_exhausted:
            assert recall_rows == []
            assert len(rows) == len(original_messages_before)
            assert stored_record.last_recalled_at is None
        else:
            assert [row.type for row in recall_rows] == [MessageType.TOOL_CALL, MessageType.TOOL_RESULT]
            persisted_call = InternalMessage.model_validate_json(recall_rows[0].content)
            persisted_result = InternalMessage.model_validate_json(recall_rows[1].content)
            assert persisted_call.tool_calls[0].id == "accepted-fallback-call"
            assert json.loads(persisted_result.content)["items"][0]["content"] == large_memory_content
            assert len(rows) == len(original_messages_before) + 2
            assert stored_record.last_recalled_at is not None


@pytest.mark.parametrize("input_kind", ["inline_image", "image_attachment_enabled", "image_attachment_disabled"])
@pytest.mark.parametrize("dispatcher_mode", ["non_stream", "stream"])
@pytest.mark.asyncio
async def test_memory_recall_precheck_skips_image_only_input(
    memory_recall_session_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    input_kind: str,
    dispatcher_mode: str,
) -> None:
    profile, cfg, current_user_id, memory_id, _vector_item_id = await _seed_memory_recall_workflow(memory_recall_session_factory)
    assert current_user_id is not None
    assert memory_id is not None

    current_user_content = " \t\n"
    environment_prompt = "SOURCE_IMAGE_ENVIRONMENT_PROMPT"
    guidance_prompt = "SOURCE_IMAGE_GUIDANCE_PROMPT"
    image_attachment_path = None
    if input_kind == "inline_image":
        source_current_message = InternalMessage.from_user_input(
            content=[
                TextPart(text=current_user_content),
                ImagePart(image_url={"url": "data:image/png;base64,CURRENT_IMAGE_ONLY_MUST_NOT_TRIGGER"}),
            ],
            environment_prompt=environment_prompt,
            guidance_prompt=guidance_prompt,
        )
    else:
        image_attachment_path = tmp_path / "image-only-input.png"
        Image.new("RGB", (2, 2), color="red").save(image_attachment_path)
        source_current_message = InternalMessage.from_user_input(
            content=current_user_content,
            attachments=[str(image_attachment_path)],
            environment_prompt=environment_prompt,
            guidance_prompt=guidance_prompt,
        )
        source_current_message = MessageAssembler.assemble(
            source_current_message,
            image_understanding=input_kind == "image_attachment_enabled",
            is_history=False,
        )

    with_todo_snapshot = '<current_session_todo_snapshot>{"revision":8,"todos":[{"content":"Confirm VS Code preference","status":"pending"},{"content":"Review editor note","status":"completed"}]}</current_session_todo_snapshot>'
    context_messages = [
        InternalMessage(
            role=MessageRole.USER,
            content='<conversation_summary through_message_id="41">\nEarlier background: The user is working on Python projects.\n</conversation_summary>',
        ),
        InternalMessage(role=MessageRole.USER, content="I prefer VS Code as my editor for Python projects."),
        InternalMessage(role=MessageRole.ASSISTANT, content="Noted your VS Code editor preference."),
        InternalMessage(role=MessageRole.TOOL, tool_call_id="existing-todo-tool-call", content=with_todo_snapshot),
        source_current_message,
    ]

    llm_requests: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def unexpected_llm_request(*args, **kwargs):
        llm_requests.append((args, kwargs))
        raise AssertionError("image-only input must skip the recall precheck request")

    monkeypatch.setattr(memory_request_module.LLMClient, "generate", unexpected_llm_request)
    monkeypatch.setattr(memory_request_module.LLMClient, "generate_with_stream_callback", unexpected_llm_request)

    events: list[dict[str, Any]] = []

    async def capture_event(event_payload: dict[str, Any]) -> None:
        events.append(deepcopy(event_payload))

    main_channel_obj = SimpleNamespace(
        id=1,
        base_url="https://chat.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "chat-key",
    )
    main_model_entry = {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "OPENAI",
        "is_enabled": True,
        "context_window_k": 32,
        "max_tokens": 1024,
    }
    main_channel_rule = SimpleNamespace(priority=1)
    main_chat_params = {
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 1024,
        "chat_timeout": 60,
        "context_window_k": 32,
    }
    main_model_entry_before = deepcopy(main_model_entry)
    main_chat_params_before = deepcopy(main_chat_params)
    total_output_tokens = 12
    session_total_output_tokens = 200
    session_total_input_tokens = 1000
    session_total_cached_tokens = 250

    async with memory_recall_session_factory() as db:
        current_user_row = await db.get(Message, current_user_id)
        assert current_user_row is not None
        if input_kind == "inline_image":
            assert isinstance(source_current_message.content, list)
            current_user_row.content = json.dumps(
                [part.model_dump(mode="json") for part in source_current_message.content],
                ensure_ascii=False,
            )
            current_user_row.content_format = MessageContentFormat.PARTS
            current_user_row.attachments = None
        else:
            current_user_row.content = current_user_content
            current_user_row.content_format = MessageContentFormat.TEXT
            current_user_row.attachments = [str(image_attachment_path)]
        current_user_row.environment_prompt = environment_prompt
        current_user_row.guidance_prompt = guidance_prompt
        await db.commit()

        message_snapshot_query = select(Message.__table__).where(Message.session_id == "session-recall").order_by(Message.id)
        database_messages_before = (await db.execute(message_snapshot_query)).mappings().all()
        session_before = await db.get(ChatSession, "session-recall")
        assert session_before is not None
        session_metadata_before = deepcopy(session_before.llm_request_metadata)
        todo_plan_before = (await db.execute(select(SessionTodoPlan).where(SessionTodoPlan.session_id == "session-recall"))).scalar_one()
        assert todo_plan_before.todos
        session_todos_before = deepcopy(todo_plan_before.todos)
        cursor_rows_before = list((await db.execute(select(ChannelCursor))).scalars().all())
        assert cursor_rows_before == []
        memory_before = await db.get(LongTermMemoryRecord, memory_id)
        assert memory_before is not None and memory_before.last_recalled_at is None

        messages_before = [message.model_dump(mode="json") for message in context_messages]
        provider_messages_before = OpenAIChatCompletionsTransformer.to_provider([source_current_message])
        expected_input_image = input_kind in {"inline_image", "image_attachment_enabled"}
        if expected_input_image:
            assert any(part.get("type") == "image_url" for part in provider_messages_before[0]["content"])

        context = MemoryRecallContext(
            db=db,
            uid="owner",
            session_id="session-recall",
            profile=profile,
            cfg=cfg,
            username="owner",
            messages=context_messages,
            turn_messages=[],
            current_user_boundary_message_id=current_user_id,
            upper_message_id=current_user_id,
            chat_channel=cfg.channel.chat_channel,
            chat_cursor_key="1:CHAT",
            chat_channel_obj=main_channel_obj,
            model_entry=main_model_entry,
            channel_rule=main_channel_rule,
            chat_params=main_chat_params,
            dispatcher_mode=dispatcher_mode,
            stream_event_callback=capture_event,
            total_output_tokens=total_output_tokens,
            session_total_output_tokens=session_total_output_tokens,
            session_total_input_tokens=session_total_input_tokens,
            session_total_cached_tokens=session_total_cached_tokens,
        )
        results = [await run_memory_recall_precheck(context) for _ in range(2)]

        for result in results:
            assert result.status == "completed"
            assert result.error_type is None
            assert result.turn_messages == []
            assert result.chat_channel is cfg.channel.chat_channel
            assert result.chat_channel_obj is main_channel_obj
            assert result.model_entry == main_model_entry_before
            assert result.channel_rule is main_channel_rule
            assert result.chat_params == main_chat_params_before
            assert result.total_output_tokens == total_output_tokens
            assert result.session_total_output_tokens == session_total_output_tokens
            assert result.session_total_input_tokens == session_total_input_tokens
            assert result.session_total_cached_tokens == session_total_cached_tokens

        assert llm_requests == []
        assert events == []
        assert context.turn_messages == []
        assert context.latest_precheck_request_metadata is None
        assert [message.model_dump(mode="json") for message in context.messages] == messages_before
        provider_messages_after = OpenAIChatCompletionsTransformer.to_provider([source_current_message])
        assert provider_messages_after == provider_messages_before
        if expected_input_image:
            assert any(part.get("type") == "image_url" for part in provider_messages_after[0]["content"])

    async with memory_recall_session_factory() as db:
        database_messages_after = (await db.execute(message_snapshot_query)).mappings().all()
        session_after = await db.get(ChatSession, "session-recall")
        assert session_after is not None
        todo_plan_after = (await db.execute(select(SessionTodoPlan).where(SessionTodoPlan.session_id == "session-recall"))).scalar_one()
        cursor_rows_after = list((await db.execute(select(ChannelCursor))).scalars().all())
        memory_after = await db.get(LongTermMemoryRecord, memory_id)
        assert memory_after is not None

    assert database_messages_after == database_messages_before
    assert session_after.llm_request_metadata == session_metadata_before
    assert todo_plan_after.todos == session_todos_before
    assert cursor_rows_after == []
    assert memory_after.last_recalled_at is None
