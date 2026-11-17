import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.core.constants import (
    ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH,
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
)
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.dispatchers import interactive_helpers as interactive_helpers_module
from app.core.dispatchers import interactive_runtime as interactive_runtime_module
from app.core.exceptions import LLMContextLengthException, LLMException
from app.core.tools import todo as todo_module
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.profile import OtherConfig
from app.models.session_todo import SessionTodoPlan
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse
from tests.unit.chat.audited_reply_support import _run_audited_interactive_dispatch
from tests.unit.chat.reply_retry_context_support import (
    _Channel,
    _context_length_generation_state,
    _Dispatcher,
    _Logger,
    _passthrough_context_summary_checkpoint,
    _Session,
)
from tests.unit.chat.reply_retry_context_support import _patch_session_crud as _patch_session_crud
from tests.unit.response_test_support import multimodal_read_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_context_length_forces_summary_on_current_channel_before_fallback(monkeypatch, streaming):
    events = []

    async def stream_event_callback(event):
        events.append(event)

    state = _context_length_generation_state(
        stream_event_callback=stream_event_callback if streaming else None,
    )
    attempts = []
    summary_calls = []
    selection_calls = []

    async def apply_checkpoint(_db, **kwargs):
        summary_calls.append(dict(kwargs))
        if kwargs.get("force"):
            return [InternalMessage(id=1, role=MessageRole.USER, content="compressed request")]
        return kwargs["messages"]

    async def select_channel(_db, _channel_config, _expected_usage, **kwargs):
        selection_calls.append(set(kwargs.get("excluded_priorities") or set()))
        raise AssertionError("successful same-channel recovery must not switch channels")

    async def generate_response(**kwargs):
        attempts.append(kwargs["model_id"])
        if len(attempts) == 1:
            raise LLMContextLengthException(provider_message="maximum context length exceeded")
        return InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, content="ok"), model=kwargs["model_id"], usage={})

    async def generate_stream_response(**kwargs):
        response = await generate_response(**kwargs)
        await kwargs["on_content"]("ok")
        return response

    monkeypatch.setattr(interactive_generation_module, "apply_context_summary_checkpoint", apply_checkpoint)
    monkeypatch.setattr(interactive_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_generation_module, "get_multimodal_from_entry", lambda _model_entry: (False, False, False))
    monkeypatch.setattr(interactive_generation_module, "resolve_chat_params", lambda _model_entry, _channel: dict(state.chat_params))
    monkeypatch.setattr(
        interactive_generation_module.ContextManager,
        "prepare_messages_for_model_request",
        lambda **kwargs: kwargs["messages"],
    )
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate_response)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "generate_with_stream_callback",
        generate_stream_response,
    )

    result = await interactive_generation_module.generate_interactive_turn(
        state,
        current_tools=[],
        response_id="response-1",
    )

    assert result.message.content == "ok"
    assert attempts == ["model-1", "model-1"]
    assert sum(1 for call in summary_calls if call.get("force")) == 1
    assert selection_calls == []
    assert state.messages[0].content == "compressed request"


@pytest.mark.asyncio
async def test_frozen_context_length_compacts_locally_before_channel_fallback(monkeypatch):
    state = _context_length_generation_state()
    state.context_frozen = True
    attempts = []
    saved_checkpoints = []
    local_compaction_calls = []

    async def save_checkpoint(checkpoint):
        saved_checkpoints.append(checkpoint)

    state.checkpoint_state.callback = save_checkpoint

    async def compact_frozen_context(_db, **kwargs):
        local_compaction_calls.append(dict(kwargs))
        if kwargs.get("force"):
            return [InternalMessage(id=1, role=MessageRole.USER, content="locally compressed request")]
        return kwargs["messages"]

    async def shared_checkpoint(*_args, **_kwargs):
        raise AssertionError("frozen context must not mutate the shared session summary")

    async def select_channel(*_args, **_kwargs):
        raise AssertionError("successful local recovery must retry the current channel first")

    async def generate_response(**kwargs):
        attempts.append(kwargs["model_id"])
        if len(attempts) == 1:
            raise LLMContextLengthException(provider_message="maximum context length exceeded")
        assert any(message["content"] == "locally compressed request" for checkpoint in saved_checkpoints for message in checkpoint["messages"])
        assert json.loads(kwargs["messages"][0].content)["user_message"] == "locally compressed request"
        return InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, content="ok"), model=kwargs["model_id"], usage={})

    monkeypatch.setattr(
        interactive_generation_module,
        "compact_frozen_context_messages",
        compact_frozen_context,
        raising=False,
    )
    monkeypatch.setattr(interactive_generation_module, "apply_context_summary_checkpoint", shared_checkpoint)
    monkeypatch.setattr(interactive_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(
        interactive_generation_module.ContextManager,
        "prepare_messages_for_model_request",
        lambda **kwargs: kwargs["messages"],
    )
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate_response)

    result = await interactive_generation_module.generate_interactive_turn(
        state,
        current_tools=[],
        response_id="response-1",
    )

    assert result.message.content == "ok"
    assert attempts == ["model-1", "model-1"]
    assert sum(call.get("force") is True for call in local_compaction_calls) == 1
    assert state.messages[0].content == "locally compressed request"


@pytest.mark.asyncio
async def test_context_length_failed_summary_falls_through_each_channel_then_raises_i18n(monkeypatch):
    state = _context_length_generation_state()
    attempts = []
    summary_calls = []
    selection_calls = []

    async def apply_checkpoint(_db, **kwargs):
        summary_calls.append(dict(kwargs))
        return kwargs["messages"]

    async def select_channel(_db, _channel_config, _expected_usage, **kwargs):
        excluded = set(kwargs.get("excluded_priorities") or set())
        selection_calls.append(excluded)
        if excluded == {1}:
            return _Channel(), {"model_id": "model-2", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=2)
        if excluded == {1, 2}:
            return _Channel(), {"model_id": "model-3", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=3)
        return None

    async def generate_response(**kwargs):
        attempts.append(kwargs["model_id"])
        raise LLMContextLengthException(provider_message="maximum context length exceeded")

    monkeypatch.setattr(interactive_generation_module, "apply_context_summary_checkpoint", apply_checkpoint)
    monkeypatch.setattr(interactive_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_generation_module, "get_multimodal_from_entry", lambda _model_entry: (False, False, False))
    monkeypatch.setattr(interactive_generation_module, "resolve_chat_params", lambda _model_entry, _channel: dict(state.chat_params))
    monkeypatch.setattr(
        interactive_generation_module.ContextManager,
        "prepare_messages_for_model_request",
        lambda **kwargs: kwargs["messages"],
    )
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate_response)

    with pytest.raises(LLMContextLengthException) as exc_info:
        await interactive_generation_module.generate_interactive_turn(
            state,
            current_tools=[],
            response_id="response-1",
        )

    assert attempts == ["model-1", "model-2", "model-3"]
    assert sum(1 for call in summary_calls if call.get("force")) == 3
    assert selection_calls == [{1}, {1, 2}, {1, 2, 3}]
    assert exc_info.value.message == ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_single_channel_invalid_request_fails_after_one_model_attempt(monkeypatch, streaming):
    async def stream_event_callback(_event):
        return None

    state = _context_length_generation_state(
        stream_event_callback=stream_event_callback if streaming else None,
    )
    attempts = []
    selection_calls = []

    async def select_channel(_db, _channel_config, _expected_usage, **kwargs):
        selection_calls.append(set(kwargs.get("excluded_priorities") or set()))
        return None

    async def generate_response(**kwargs):
        attempts.append(kwargs["model_id"])
        raise LLMException(
            message="ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS",
            status=400,
            detail={
                "error": {
                    "message": "Audio input is not available.",
                    "type": "invalid_request_error",
                    "param": "input",
                    "code": None,
                }
            },
        )

    async def generate_stream_response(**kwargs):
        return await generate_response(**kwargs)

    monkeypatch.setattr(interactive_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(
        interactive_generation_module,
        "apply_context_summary_checkpoint",
        _passthrough_context_summary_checkpoint,
    )
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate_response)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "generate_with_stream_callback",
        generate_stream_response,
    )
    monkeypatch.setattr(
        interactive_generation_module.ContextManager,
        "prepare_messages_for_model_request",
        lambda **kwargs: kwargs["messages"],
    )

    with pytest.raises(LLMException) as exc_info:
        await interactive_generation_module.generate_interactive_turn(
            state,
            current_tools=[],
            response_id="response-1",
        )

    assert attempts == ["model-1"]
    assert selection_calls == [{1}]
    assert exc_info.value.kwargs["status"] == 400


@pytest.mark.asyncio
async def test_handle_stream_content_buffers_leading_whitespace_until_text():
    emitted_events = []

    async def publish_event(event):
        emitted_events.append(event)

    stream_state = interactive_helpers_module._AgentLoopStreamState(
        callback=publish_event,
        current_turn=1,
        response_id="response-1",
        expose_tool_call_content=True,
        show_tool_calls=True,
    )

    await interactive_helpers_module._handle_stream_content(stream_state, " ")
    await interactive_helpers_module._handle_stream_content(stream_state, "\n")

    assert emitted_events == []

    await interactive_helpers_module._handle_stream_content(stream_state, "hello")

    assert [event["type"] for event in emitted_events] == ["agent_loop_output", "content"]
    assert emitted_events[-1]["content"] == " \nhello"

    await interactive_helpers_module._handle_stream_content(stream_state, " ")
    await interactive_helpers_module._handle_stream_content(stream_state, "world")

    content_events = [event for event in emitted_events if event["type"] == "content"]
    assert [event["content"] for event in content_events] == [" \nhello", " ", "world"]
    assert "".join(event["content"] for event in content_events) == " \nhello world"


@pytest.mark.asyncio
async def test_llm_stream_callback_preserves_first_visible_character_after_whitespace(monkeypatch):
    emitted_events = []
    chunks = [
        {"choices": [{"delta": {"content": " "}}]},
        {"choices": [{"delta": {"content": "政"}}]},
        {"choices": [{"delta": {"content": "策"}, "finish_reason": "stop"}]},
    ]

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def publish_event(event):
        emitted_events.append(event)

    stream_state = interactive_helpers_module._AgentLoopStreamState(
        callback=publish_event,
        current_turn=1,
        response_id="response-1",
        expose_tool_call_content=True,
        show_tool_calls=True,
    )

    async def on_content(content):
        await interactive_helpers_module._handle_stream_content(stream_state, content)

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate_stream", classmethod(generate_stream))

    response = await interactive_generation_module.LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    content_events = [event for event in emitted_events if event["type"] == "content"]
    assert [event["content"] for event in content_events] == [" 政", "策"]
    assert "".join(event["content"] for event in content_events) == " 政策"
    assert response.message.content == " 政策"


@pytest.mark.asyncio
async def test_dispatcher_resume_uses_checkpoint_without_replaying_initial_message(monkeypatch):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        tool=SimpleNamespace(
            max_turns=5,
            max_parallel_tools=5,
            executor_max_workers=1,
        ),
        other=OtherConfig(),
    )
    checkpoint_messages = [
        InternalMessage(role=MessageRole.USER, content="original request"),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[
                InternalToolCall(
                    id="tool-1",
                    name="execute_shell",
                    arguments={"command": "echo 1"},
                )
            ],
        ),
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="tool-1",
            content="1",
        ),
    ]
    resume_state = {
        "messages": [message.model_dump(mode="json") for message in checkpoint_messages],
        "turn_messages": [],
        "files_to_user": [],
        "current_turn": 1,
    }
    model_requests = []
    prepare_calls = []
    checkpoint_calls = []
    logger = _Logger()

    async def get_user(db, uid):
        return SimpleNamespace(username="admin")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        if kwargs.get("excluded_priorities"):
            return _Channel(), {"model_id": "model-2", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=2)
        return _Channel(), {"model_id": "model-1", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile, *, goal_mode=False, tool_workflow=None):
        return [], []

    async def prepare_messages(*args, **kwargs):
        prepare_calls.append((args, kwargs))
        return [InternalMessage(role=MessageRole.USER, content="unexpected replay")]

    async def apply_checkpoint(db, **kwargs):
        checkpoint_calls.append(kwargs)
        return kwargs["messages"]

    async def generate(**kwargs):
        model_requests.append(kwargs["messages"])
        if len(model_requests) == 1:
            raise LLMException(message="ERR_LLM_UNEXPECTED_ERROR")
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="continued response",
            ),
            model=kwargs["model_id"],
            usage={},
        )

    async def save_assistant(*args, **kwargs):
        return SimpleNamespace(content="continued response")

    async def fetch_additional():
        return []

    monkeypatch.setattr(interactive_runtime_module, "logger", logger)
    monkeypatch.setattr(interactive_runtime_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_runtime_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_runtime_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_runtime_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_runtime_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(interactive_runtime_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(interactive_generation_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        interactive_runtime_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(
        interactive_generation_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(interactive_runtime_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(
        interactive_generation_module,
        "apply_context_summary_checkpoint",
        apply_checkpoint,
    )
    monkeypatch.setattr(
        interactive_generation_module.ContextManager,
        "prepare_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_runtime_module, "save_assistant_message", save_assistant)

    response = await _Dispatcher.dispatch(
        db=_Session(),
        message="original request",
        uid="user-1",
        session_id="session-1",
        persisted_initial_message=InternalMessage(
            id=2,
            role=MessageRole.USER,
            content="original request",
        ),
        frozen_user_message_ids=[1, 2],
        persisted_profile_id=1,
        additional_user_messages_fetcher=fetch_additional,
        execution_resume_state=resume_state,
    )

    assert prepare_calls == []
    assert len(model_requests) == 2
    assert len(checkpoint_calls) == len(model_requests)
    assert [call["fixed_upper_message_id"] for call in checkpoint_calls] == [1, 1]
    assert all(call.get("reserved_tokens", 0) == 0 for call in checkpoint_calls)
    for request_messages in model_requests:
        assert [message.role for message in request_messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
        ]
        assert json.loads(request_messages[0].content)["user_message"] == "original request"
        assert request_messages[1].tool_calls[0].id == "tool-1"
        assert request_messages[2].tool_call_id == "tool-1"
        assert json.loads(request_messages[2].content) == {"tool_result": 1}
    assert all("用户消息" not in message and "User message" not in message for message in logger.info_messages)
    assert LLMResponse.model_validate(response).choices[0] == LLMChoice(
        message=LLMChoiceMessage(
            role=MessageRole.ASSISTANT,
            content="continued response",
        ),
        finish_reason="stop",
        created_at=response["choices"][0]["created_at"],
    )


@pytest.mark.asyncio
async def test_hidden_stream_content_does_not_prevent_channel_retry(monkeypatch):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        tool=SimpleNamespace(
            max_turns=5,
            max_parallel_tools=5,
            executor_max_workers=1,
        ),
        other=OtherConfig(),
    )
    selected_models = []
    emitted_events = []
    logger = _Logger()

    async def get_user(db, uid):
        return SimpleNamespace(username="admin")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        if kwargs.get("excluded_priorities"):
            return _Channel(), {"model_id": "model-2", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=2)
        return _Channel(), {"model_id": "model-1", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile, *, goal_mode=False, tool_workflow=None):
        return [], []

    async def generate_with_stream_callback(**kwargs):
        selected_models.append(kwargs["model_id"])
        await kwargs["on_content"]("处理中" if kwargs["model_id"] == "model-1" else "已完成")
        if kwargs["model_id"] == "model-1":
            raise LLMException(message="ERR_LLM_UNEXPECTED_ERROR")
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="已完成",
            ),
            model=kwargs["model_id"],
            usage={},
        )

    async def save_assistant(*args, **kwargs):
        return SimpleNamespace(content="已完成")

    async def fetch_additional():
        return []

    async def stream_event_callback(event):
        emitted_events.append(event)

    monkeypatch.setattr(interactive_runtime_module, "logger", logger)
    monkeypatch.setattr(interactive_runtime_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_runtime_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_runtime_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_runtime_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_runtime_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(interactive_runtime_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(interactive_generation_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        interactive_runtime_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(
        interactive_generation_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(
        interactive_generation_module,
        "apply_context_summary_checkpoint",
        _passthrough_context_summary_checkpoint,
    )
    monkeypatch.setattr(
        interactive_generation_module.ContextManager,
        "prepare_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "generate_with_stream_callback",
        generate_with_stream_callback,
    )
    monkeypatch.setattr(interactive_runtime_module, "save_assistant_message", save_assistant)

    response = await _Dispatcher.dispatch(
        db=_Session(),
        message="original request",
        uid="user-1",
        session_id="session-1",
        persisted_initial_message=InternalMessage(
            id=1,
            role=MessageRole.USER,
            content="original request",
        ),
        persisted_profile_id=1,
        additional_user_messages_fetcher=fetch_additional,
        execution_resume_state={
            "messages": [
                InternalMessage(
                    role=MessageRole.USER,
                    content="original request",
                ).model_dump(mode="json")
            ],
            "turn_messages": [],
            "files_to_user": [],
            "current_turn": 0,
        },
        stream_event_callback=stream_event_callback,
        expose_tool_call_content=False,
    )

    assert selected_models == ["model-1", "model-2"]
    assert [event["content"] for event in emitted_events if event["type"] == "content"] == ["已完成"]
    assert LLMResponse.model_validate(response).choices[0].message.content == "已完成"


@pytest.mark.asyncio
async def test_stream_emits_todo_update_even_when_tool_output_is_hidden(monkeypatch):
    tool_call = InternalToolCall(
        id="call-todo",
        name="manage_todo",
        arguments={"operation": "read"},
    )
    executor = todo_module.ManageTodoExecutor(project_root=".", uid="user-1")
    executor.set_runtime_context(db=object(), session_id="session-1")

    async def read_session_todo(*_args, **_kwargs):
        return True, SessionTodoPlan(
            session_id="session-1",
            uid="user-1",
            revision=2,
            todos=[{"content": "verify", "status": "in_progress"}],
        )

    monkeypatch.setattr(todo_module.session_todo_crud, "read", read_session_todo)

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(current_tool_call, *args, **kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=current_tool_call.id,
            content=await executor.execute(**current_tool_call.arguments),
        )

    async def persist_todo_snapshot(_db, *, uid, session_id, tool_results):
        assert uid == "user-1"
        assert session_id == "session-1"
        assert len(tool_results) == 1
        snapshot = {
            "revision": 2,
            "todos": [{"content": "verify", "status": "in_progress"}],
        }
        tool_results[-1].current_session_todo = snapshot
        return snapshot

    events, _unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_dispatch=True,
        show_tool_calls=False,
        tool_call=tool_call,
        persist_todo_snapshot_handler=persist_todo_snapshot,
    )

    todo_events = [event for event in events if event.get("type") == "todo_update"]
    assert len(todo_events) == 1
    assert todo_events[0] == {
        "type": "todo_update",
        "session_id": "session-1",
        "revision": 2,
        "todos": [{"content": "verify", "status": "in_progress"}],
        "request_id": "request-1",
    }
    assert all(event.get("type") != "tool_end" for event in events)


@pytest.mark.asyncio
async def test_interactive_omits_whitespace_only_tool_turn_content_events(monkeypatch):
    emitted_events = []
    tool_call = InternalToolCall(
        id="call-whitespace",
        name="execute_shell",
        arguments={"command": "echo 1"},
    )

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(current_tool_call, *args, **kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=current_tool_call.id,
            content='{"status":"success"}',
        )

    async def publish_event(event):
        emitted_events.append(event)

    response, _unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        tool_call=tool_call,
        response_messages=[
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content=" \n\t",
                tool_calls=[tool_call],
            ),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ],
    )

    tool_start_events = [event for event in emitted_events if event["type"] == "tool_start"]
    assert len(tool_start_events) == 1
    tool_round_response_id = tool_start_events[0]["response_id"]
    tool_round_turn_end_events = [event for event in emitted_events if event["type"] == "turn_end" and event["response_id"] == tool_round_response_id]
    assert len(tool_round_turn_end_events) == 1
    assert "content" not in tool_round_turn_end_events[0]
    assert [event["content"] for event in emitted_events if event["type"] == "content"] == ["finished"]
    assert response["choices"][0]["message"]["content"] == "finished"


@pytest.mark.asyncio
async def test_hidden_tool_round_suppresses_reasoning_but_keeps_final_round_reasoning(monkeypatch):
    emitted_events = []
    tool_call = InternalToolCall(
        id="call-hidden-reasoning",
        name="execute_shell",
        arguments={"command": "echo 1"},
    )

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(current_tool_call, *args, **kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=current_tool_call.id,
            content='{"status":"success"}',
        )

    async def publish_event(event):
        emitted_events.append(event)

    response, _unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        show_tool_calls=False,
        tool_call=tool_call,
        response_messages=[
            InternalMessage(
                role=MessageRole.ASSISTANT,
                reasoning_content="tool round reasoning",
                tool_calls=[tool_call],
            ),
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content="finished",
                reasoning_content="final round reasoning",
            ),
        ],
    )

    reasoning_events = [event for event in emitted_events if event["type"] == "reasoning"]
    assert [event["content"] for event in reasoning_events] == ["final round reasoning"]
    assert all(event["type"] not in {"tool_start", "tool_end"} for event in emitted_events)
    assert response["choices"][0]["message"]["content"] == "finished"
    assert response["choices"][0]["message"]["reasoning_content"] == "final round reasoning"
    assert all(item.get("role") != MessageRole.TOOL for item in response["history"])
    assert all(not item.get("tool_calls") for item in response["history"])


@pytest.mark.asyncio
async def test_interactive_injects_read_multimodal_result_only_into_next_model_request(monkeypatch, tmp_path):
    image_path = tmp_path / "read-result.png"
    Image.new("RGB", (2, 2), color=(30, 60, 90)).save(image_path)
    tool_call = InternalToolCall(
        id="call-image",
        name="read_multimodal_file",
        arguments={"path": str(image_path)},
    )
    tool_message = "下一条 role=user 消息由系统根据本工具结果自动生成，不是用户的新输入"
    generated_calls = []
    checkpoints = []

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(current_tool_call, *args, **kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=current_tool_call.id,
            content=json.dumps(
                multimodal_read_payload(image_path, message=tool_message),
                ensure_ascii=False,
            ),
        )

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        generated_calls_target=generated_calls,
        response_messages=[
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ],
        tool_call=tool_call,
        multimodal_capabilities=(True, False, False),
    )

    assert len(generated_calls) == 2
    assert not any(message.role == MessageRole.USER and isinstance(message.content, list) for message in generated_calls[0]["messages"])
    temporary_message = generated_calls[1]["messages"][-1]
    assert temporary_message.role == MessageRole.USER
    assert any(part.type == "text" and "不是用户的新输入" in part.text for part in temporary_message.content)
    assert any(part.type == "image_url" and part.image_url["url"].startswith("data:image/") for part in temporary_message.content)

    history = response["history"]
    assert not any(item.get("role") == "user" and isinstance(item.get("content"), list) for item in history)
    assert all("pending_multimodal_inputs" not in checkpoint for checkpoint in checkpoints)
    assert all("data:image" not in json.dumps(checkpoint, ensure_ascii=False) for checkpoint in checkpoints)
    assert all(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in checkpoint for checkpoint in checkpoints)
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_injects_read_audio_result_only_when_audio_understanding_is_enabled(monkeypatch, tmp_path):
    audio_path = tmp_path / "read-result.mp3"
    audio_path.write_bytes(b"audio")
    tool_call = InternalToolCall(
        id="call-audio",
        name="read_multimodal_file",
        arguments={"path": str(audio_path)},
    )
    generated_calls = []

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(current_tool_call, *args, **kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=current_tool_call.id,
            content=json.dumps(
                multimodal_read_payload(
                    audio_path,
                    modality="audio",
                    message="下一条 role=user 消息不是用户新输入",
                ),
                ensure_ascii=False,
            ),
        )

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        generated_calls_target=generated_calls,
        response_messages=[
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ],
        tool_call=tool_call,
        multimodal_capabilities=(False, True, False),
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert len(generated_calls) == 2
    temporary_message = generated_calls[1]["messages"][-1]
    audio_parts = [part for part in temporary_message.content if part.type == "audio"]
    assert len(audio_parts) == 1
    assert audio_parts[0].path == str(audio_path.resolve())
    assert audio_parts[0].format == "mp3"
    assert audio_parts[0].data is None
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_resume_reinjects_multimodal_image_from_messages_with_trailing_confirmation(monkeypatch, tmp_path):
    image_path = tmp_path / "resume-image.png"
    Image.new("RGB", (2, 2), color=(90, 60, 30)).save(image_path)
    tool_call = InternalToolCall(
        id="call-image",
        name="read_multimodal_file",
        arguments={"path": str(image_path)},
    )
    tool_result = InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call.id,
        content=json.dumps(
            multimodal_read_payload(
                image_path,
                message="下一条 role=user 消息不是用户新输入",
            ),
            ensure_ascii=False,
        ),
    )
    generated_calls = []

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(*args, **kwargs):
        raise AssertionError("resume with a completed multimodal result must not execute a tool")

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        generated_calls_target=generated_calls,
        response_messages=[InternalMessage(role=MessageRole.ASSISTANT, content="finished")],
        tool_call=tool_call,
        multimodal_capabilities=(True, False, False),
        execution_resume_state={
            "messages": [
                InternalMessage(role=MessageRole.USER, content="request").model_dump(mode="json"),
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]).model_dump(mode="json"),
                tool_result.model_dump(mode="json"),
                InternalMessage(role=MessageRole.USER, content="确认执行").model_dump(mode="json"),
            ],
            "turn_messages": [],
            "files_to_user": [],
            "current_turn": 0,
        },
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    temporary_message = generated_calls[0]["messages"][-1]
    assert temporary_message.role == MessageRole.USER
    assert any(part.type == "image_url" and part.image_url["url"].startswith("data:image/") for part in temporary_message.content)
    assert unknown_calls == []
