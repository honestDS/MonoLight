import json
from types import SimpleNamespace

import pytest

from app.core.dispatchers import memory_recall_persistence as persistence_module
from app.core.dispatchers import memory_recall_request as request_module
from app.core.dispatchers.memory_recall_types import MemoryRecallContext
from app.core.tools.longterm_memory import (
    MANAGE_LONGTERM_MEMORY_TOOL_NAME,
    MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA,
)
from app.models.message import InternalMessage, InternalToolCall, MessageRole, MessageType


class _FakeDb:
    def __init__(self):
        self.commit_count = 0
        self.order = []

    async def commit(self):
        self.commit_count += 1
        self.order.append("commit")

    async def refresh(self, _session):
        return None


def _call(call_id="call-1", arguments=None, name=MANAGE_LONGTERM_MEMORY_TOOL_NAME):
    return InternalToolCall(
        id=call_id,
        name=name,
        arguments=arguments or {"operation": "recall", "query": "user context", "top_k": 3},
    )


def _assistant(call_id="call-1", message_id=None):
    return InternalMessage(
        id=message_id,
        role=MessageRole.ASSISTANT,
        tool_calls=[_call(call_id)],
    )


def _context(*, messages=None, turn_messages=None):
    channel = SimpleNamespace(
        base_url="https://example.invalid",
        chat_timeout=60,
        get_decrypted_api_key=lambda: "api-key",
    )
    return MemoryRecallContext(
        db=_FakeDb(),
        uid="uid-1",
        session_id="session-1",
        profile=SimpleNamespace(id=7),
        cfg=SimpleNamespace(memory=SimpleNamespace()),
        username="tester",
        messages=messages or [InternalMessage(id=1, role=MessageRole.USER, content="request")],
        turn_messages=turn_messages or [],
        current_user_boundary_message_id=42,
        upper_message_id=33,
        chat_channel="chat",
        chat_cursor_key="cursor",
        chat_channel_obj=channel,
        model_entry={"model_id": "model-1", "protocol": "OPENAI", "max_tokens": 128},
        channel_rule=SimpleNamespace(priority=1),
        chat_params={
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 128,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_save_and_execute_recall_orders_persistence_events_commit_and_isolated_tool(
    monkeypatch,
    show_tool_calls,
):
    context = _context()
    order = []
    context.db.order = order
    context.show_tool_calls = show_tool_calls
    events = []
    assistant = _assistant(call_id="recall-call")
    tool_result = InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id="recall-call",
        content=json.dumps({"items": []}),
    )
    saved_assistant_kwargs = []
    saved_tool_kwargs = []
    isolated_calls = []

    async def event_callback(event):
        event_type = event["type"]
        events.append(event_type)
        order.append("event:" + event_type)

    async def save_assistant(*_args, **kwargs):
        order.append("assistant-save")
        saved_assistant_kwargs.append(kwargs)
        assistant.id = 101
        return SimpleNamespace(id=101)

    async def isolated(*args, **kwargs):
        order.append("isolated-tool")
        isolated_calls.append((args, kwargs))
        return tool_result

    async def save_tool(*args, **kwargs):
        order.append("tool-save")
        saved_tool_kwargs.append(kwargs)
        stored = tool_result.model_copy(update={"id": 102})
        args[5].append(stored)
        args[6].append(stored)
        return stored

    context.stream_event_callback = event_callback
    monkeypatch.setattr(persistence_module, "save_assistant_message", save_assistant)
    monkeypatch.setattr(persistence_module, "process_single_tool_with_isolated_db", isolated)
    monkeypatch.setattr(persistence_module, "save_tool_response", save_tool)

    await persistence_module.save_and_execute_recall(
        context,
        assistant,
        assistant_key="assistant-key",
        tool_key="tool-key",
        response_id="response-id",
    )

    expected = (
        [
            "assistant-save",
            "event:agent_loop_start",
            "event:turn_end",
            "event:tool_start",
            "commit",
            "isolated-tool",
            "tool-save",
            "event:tool_end",
        ]
        if show_tool_calls
        else ["assistant-save", "commit", "isolated-tool", "tool-save"]
    )
    assert order == expected
    assert order.index("commit") < order.index("isolated-tool")
    assert saved_assistant_kwargs[0]["dedupe_key"] == "assistant-key"
    assert saved_tool_kwargs[0]["dedupe_key"] == "tool-key"
    assert isolated_calls[0][0][0] == assistant.tool_calls[0]
    assert isolated_calls[0][0][6] == 0
    assert isolated_calls[0][0][7] == "uid-1"
    assert isolated_calls[0][1]["source_message_id"] == 42
    assert isolated_calls[0][1]["context_summary_boundary_message_id"] == 33
    assert context.db.commit_count == 1
    assert context.messages[-1].role == MessageRole.TOOL
    assert context.turn_messages[-1].role == MessageRole.TOOL
    assert events == (["agent_loop_start", "turn_end", "tool_start", "tool_end"] if show_tool_calls else [])


def _row(
    message,
    *,
    uid="uid-1",
    session_id="session-1",
    profile_id=7,
    role=None,
    message_type=None,
    row_id=101,
):
    return SimpleNamespace(
        id=row_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        role=role or message.role.value,
        type=message_type or (MessageType.TOOL_CALL.value if message.tool_calls else MessageType.TOOL_RESULT.value),
        content=message.model_dump_json(exclude_none=True),
        created_at=0,
    )


@pytest.mark.asyncio
async def test_load_dedupe_messages_accepts_matching_pair(monkeypatch):
    context = _context()
    assistant = _assistant(call_id="call-1")
    tool = InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content=json.dumps({}))
    rows = {"assistant": _row(assistant), "tool": _row(tool, row_id=102)}

    async def get_by_key(_db, key):
        return rows["assistant" if key.startswith("memory-recall-assistant:") else "tool"]

    monkeypatch.setattr(persistence_module.message_crud, "get_by_dedupe_key", get_by_key)

    restored_assistant, restored_tool, assistant_key, tool_key = await persistence_module.load_dedupe_messages(context)

    assert restored_assistant.tool_calls[0].id == "call-1"
    assert restored_tool.tool_call_id == "call-1"
    assert assistant_key.startswith("memory-recall-assistant:")
    assert tool_key.startswith("memory-recall-tool:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        "uid",
        "session",
        "profile",
        "assistant_role",
        "assistant_type",
        "tool_uid",
        "tool_session",
        "tool_profile",
        "tool_role",
        "tool_type",
        "orphan",
        "tool_call",
    ],
)
async def test_load_dedupe_messages_rejects_mismatched_records(monkeypatch, broken):
    context = _context()
    assistant = _assistant(call_id="call-1")
    tool = InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content=json.dumps({}))
    assistant_row = _row(assistant)
    tool_row = _row(tool, row_id=102)
    if broken == "uid":
        assistant_row.uid = "other-user"
    elif broken == "session":
        assistant_row.session_id = "other-session"
    elif broken == "profile":
        assistant_row.profile_id = 8
    elif broken == "assistant_role":
        assistant_row.role = MessageRole.USER.value
    elif broken == "assistant_type":
        assistant_row.type = MessageType.TEXT.value
    elif broken == "tool_uid":
        tool_row.uid = "other-user"
    elif broken == "tool_session":
        tool_row.session_id = "other-session"
    elif broken == "tool_profile":
        tool_row.profile_id = 8
    elif broken == "tool_role":
        tool_row.role = MessageRole.USER.value
    elif broken == "tool_type":
        tool_row.type = MessageType.TEXT.value
    elif broken == "orphan":
        assistant_row = None
    else:
        tool_row.content = InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="other-call",
            content=json.dumps({}),
        ).model_dump_json(exclude_none=True)
    rows = {"assistant": assistant_row, "tool": tool_row}

    async def get_by_key(_db, key):
        return rows["assistant" if key.startswith("memory-recall-assistant:") else "tool"]

    monkeypatch.setattr(persistence_module.message_crud, "get_by_dedupe_key", get_by_key)

    with pytest.raises(ValueError):
        await persistence_module.load_dedupe_messages(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_generate_commits_before_network_and_discards_stream_content(monkeypatch, stream):
    context = _context()
    context.dispatcher_mode = "stream" if stream else "non_stream"
    order = context.db.order
    marker = object()

    async def non_stream_generate(**_kwargs):
        order.append("model")
        return marker

    async def stream_generate(**kwargs):
        order.append("model")
        assert await kwargs["on_content"]("hidden content") is None
        return marker

    monkeypatch.setattr(request_module, "get_channel_http_proxy", lambda _channel: None)
    monkeypatch.setattr(request_module, "get_model_custom_headers", lambda _entry: {})
    monkeypatch.setattr(request_module, "resolve_model_protocol", lambda _entry: "OPENAI")
    if stream:
        monkeypatch.setattr(request_module.LLMClient, "generate_with_stream_callback", stream_generate)
    else:
        monkeypatch.setattr(request_module.LLMClient, "generate", non_stream_generate)

    result = await request_module.generate(
        context,
        [InternalMessage(role=MessageRole.USER, content="request")],
        {"input_tokens": 1},
    )

    assert result is marker
    assert order == ["commit", "model"]


@pytest.mark.asyncio
async def test_prepare_request_messages_exposes_only_memory_tool_and_writes_summary(monkeypatch):
    context = _context(messages=[InternalMessage(id=10, role=MessageRole.USER, content="memory query secret")])
    context.context_summary_callback = object()
    summary_messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="summary result"),
        InternalMessage(id=20, role=MessageRole.USER, content="retrieved memory body"),
    ]
    checkpoint_calls = []
    trim_calls = []
    token_calls = []
    session = SimpleNamespace(
        context_summary_revision=4,
        context_content_revision=6,
        llm_request_metadata={"total_output_tokens": 8},
    )

    async def apply_checkpoint(_db, **kwargs):
        checkpoint_calls.append(kwargs)
        return summary_messages

    async def materialize(_db, _session_id, messages, _max_tokens):
        return list(messages)

    def trim(**kwargs):
        trim_calls.append(kwargs)
        return kwargs["messages"]

    async def get_session(_db, _session_id):
        return session

    def estimate(messages, tools):
        token_calls.append((messages, tools))
        return 321

    def baseline(*_args, **_kwargs):
        return {"token_fingerprint": "no-content"}

    monkeypatch.setattr(request_module, "apply_context_summary_checkpoint", apply_checkpoint)
    monkeypatch.setattr(request_module, "materialize_latest_user_environment_prompt", materialize)
    monkeypatch.setattr(request_module.ContextManager, "trim_messages_for_model_request", trim)
    monkeypatch.setattr(request_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(request_module, "estimate_request_context_tokens", estimate)
    monkeypatch.setattr(request_module, "build_request_token_baseline", baseline)
    monkeypatch.setattr(request_module, "resolve_model_protocol", lambda _entry: "OPENAI")

    request_messages, metadata, response_id = await request_module.prepare_request_messages(
        context,
        context.messages,
        is_main_context=True,
    )

    expected_tools = [MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA]
    assert context.messages is summary_messages
    assert request_messages == summary_messages
    assert checkpoint_calls[0]["tools"] == expected_tools
    assert checkpoint_calls[0]["trigger_mode"].value == "user_message"
    assert checkpoint_calls[0]["lifecycle_event_callback"] is context.context_summary_callback
    assert trim_calls[0]["tools"] == expected_tools
    assert token_calls[0][1] == expected_tools
    assert metadata["turn"] == 0
    assert metadata["input_tokens"] == 321
    assert metadata["total_output_tokens"] == 8
    assert metadata["response_id"] == response_id
    assert "memory query secret" not in json.dumps(metadata)
    assert "retrieved memory body" not in json.dumps(metadata)
    assert context.latest_llm_request_metadata is metadata
