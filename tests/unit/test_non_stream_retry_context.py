import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from PIL import Image

from app.core.constants import SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY
from app.core.dispatchers import interactive as interactive_module
from app.core.dispatchers import interactive_helpers as interactive_helpers_module
from app.core.dispatchers import non_stream as non_stream_module
from app.core.dispatchers import stream as stream_module
from app.core.exceptions import LLMException
from app.core.terminal.schemas import (
    ShellInteractiveHandoffResult,
    TerminalOutputBufferState,
    TerminalSessionStatus,
)
from app.core.utils.dispatcher import markdown_instruction as markdown_instruction_module
from app.core.utils.dispatcher.markdown_instruction import build_max_output_tokens_instruction
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse


class _Session:
    def __init__(self):
        self.commit_count = 0

    async def commit(self):
        self.commit_count += 1


class _DatabaseLikeSession(_Session):
    async def execute(self, *args, **kwargs):
        return None


class _Dispatcher(non_stream_module.NonStreamDispatcherMixin):
    @classmethod
    async def validate_initial_message_before_save(cls, db, message, uid, session_id, profile, attachments):
        return None


class _StreamDispatcher(stream_module.StreamDispatcherMixin):
    @classmethod
    async def validate_initial_message_before_save(cls, db, message, uid, session_id, profile, attachments):
        return None


async def _passthrough_context_summary_checkpoint(db, **kwargs):
    return kwargs["messages"]


async def _build_max_tokens_runtime_instruction(_db, _session_id, max_tokens):
    return build_max_output_tokens_instruction(max_tokens)


class _Channel:
    id = 1
    name = "test-channel"
    base_url = "https://example.invalid"

    def get_decrypted_api_key(self):
        return "secret"


class _Logger:
    def __init__(self):
        self.info_messages = []

    def bind(self, **kwargs):
        return self

    def info(self, message):
        self.info_messages.append(message)

    def warning(self, message):
        return None

    def error(self, message, **kwargs):
        return None


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

    monkeypatch.setattr(interactive_module.LLMClient, "generate_stream", classmethod(generate_stream))

    response = await interactive_module.LLMClient.generate_with_stream_callback(
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

    async def get_tools(db, current_profile):
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
        return SimpleNamespace(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="continued response",
            )
        )

    async def save_assistant(*args, **kwargs):
        return SimpleNamespace(content="continued response")

    async def fetch_additional():
        return []

    monkeypatch.setattr(interactive_module, "logger", logger)
    monkeypatch.setattr(interactive_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(interactive_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        interactive_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(interactive_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(
        interactive_module,
        "apply_context_summary_checkpoint",
        apply_checkpoint,
    )
    monkeypatch.setattr(
        interactive_module.ContextManager,
        "trim_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(interactive_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_module, "save_assistant_message", save_assistant)

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
    for request_messages in model_requests:
        assert [message.role for message in request_messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
        ]
        assert request_messages[0].content == "original request"
        assert request_messages[1].tool_calls[0].id == "tool-1"
        assert request_messages[2].tool_call_id == "tool-1"
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

    async def get_tools(db, current_profile):
        return [], []

    async def generate_with_stream_callback(**kwargs):
        selected_models.append(kwargs["model_id"])
        await kwargs["on_content"]("处理中" if kwargs["model_id"] == "model-1" else "已完成")
        if kwargs["model_id"] == "model-1":
            raise LLMException(message="ERR_LLM_UNEXPECTED_ERROR")
        return SimpleNamespace(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="已完成",
            )
        )

    async def save_assistant(*args, **kwargs):
        return SimpleNamespace(content="已完成")

    async def fetch_additional():
        return []

    async def stream_event_callback(event):
        emitted_events.append(event)

    monkeypatch.setattr(interactive_module, "logger", logger)
    monkeypatch.setattr(interactive_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(interactive_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        interactive_module,
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
        interactive_module,
        "apply_context_summary_checkpoint",
        _passthrough_context_summary_checkpoint,
    )
    monkeypatch.setattr(
        interactive_module.ContextManager,
        "trim_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(
        interactive_module.LLMClient,
        "generate_with_stream_callback",
        generate_with_stream_callback,
    )
    monkeypatch.setattr(interactive_module, "save_assistant_message", save_assistant)

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
async def test_non_stream_retry_accumulates_empty_response_usage_and_refreshes_max_tokens_context(monkeypatch):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=SimpleNamespace(chat_timeout=60)),
        tool=SimpleNamespace(
            max_turns=5,
            max_parallel_tools=5,
            executor_max_workers=1,
        ),
    )
    model_requests = []
    persisted_environment_prompts = []
    saved_created_at = []
    request_metadatas = []
    callback_order = []
    logger = _Logger()

    async def get_user(db, uid):
        return SimpleNamespace(username="admin")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        if kwargs.get("excluded_priorities"):
            return _Channel(), {"model_id": "model-2", "max_tokens": 256, "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=2)
        return _Channel(), {"model_id": "model-1", "max_tokens": 1024, "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile):
        return [], []

    async def prepare_messages(*args, **kwargs):
        return [
            InternalMessage(
                id=1,
                role=MessageRole.USER,
                content="original request",
                environment_prompt=build_max_output_tokens_instruction(kwargs["max_tokens"]),
            )
        ]

    async def generate(**kwargs):
        model_requests.append(kwargs)
        if kwargs["model_id"] == "model-1":
            return SimpleNamespace(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="",
                ),
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 0,
                    "total_tokens": 100,
                    "cached_tokens": 100,
                },
            )
        return SimpleNamespace(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="ok",
            ),
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "cached_tokens": 0,
            },
        )

    async def request_metadata_callback(metadata):
        metadata_copy = dict(metadata)
        request_metadatas.append(metadata_copy)
        callback_order.append(("metadata", metadata_copy))

    async def save_assistant(db, session_id, uid, profile_id, ai_msg, dedupe_key=None, created_at=None):
        callback_order.append(("message", ai_msg))
        saved_created_at.append(created_at)
        return SimpleNamespace(content="ok")

    async def fetch_additional():
        return []

    async def mark_processed(db, message_id):
        return None

    async def set_environment_prompt(_db, message_id, environment_prompt):
        persisted_environment_prompts.append((message_id, environment_prompt))
        return True

    monkeypatch.setattr(markdown_instruction_module, "build_user_runtime_instructions", _build_max_tokens_runtime_instruction)
    monkeypatch.setattr(markdown_instruction_module.message_crud, "set_environment_prompt", set_environment_prompt)
    monkeypatch.setattr(interactive_module, "logger", logger)
    monkeypatch.setattr(interactive_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_module, "mark_initial_message_processed", mark_processed)
    monkeypatch.setattr(interactive_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(interactive_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        interactive_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": model_entry["max_tokens"],
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(interactive_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(
        interactive_module,
        "apply_context_summary_checkpoint",
        _passthrough_context_summary_checkpoint,
    )
    monkeypatch.setattr(
        interactive_module.ContextManager,
        "trim_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(interactive_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_module, "save_assistant_message", save_assistant)

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
        request_metadata_callback=request_metadata_callback,
    )

    assert [request["model_id"] for request in model_requests] == ["model-1", "model-2"]
    assert [event_type for event_type, _ in callback_order] == ["metadata", "metadata", "message"]
    assert len(request_metadatas) == 2
    assert request_metadatas[0]["total_input_tokens"] == 100
    assert request_metadatas[0]["total_cached_tokens"] == 100
    assert request_metadatas[1]["total_input_tokens"] == 200
    assert request_metadatas[1]["total_cached_tokens"] == 100
    assert request_metadatas[1]["cache_hit_rate"] == pytest.approx(0.5)
    assert "The hard maximum for this response is 1024 output tokens." in model_requests[0]["messages"][0].content
    assert "The hard maximum for this response is 256 output tokens." in model_requests[1]["messages"][0].content
    assert "The hard maximum for this response is 1024 output tokens." not in model_requests[1]["messages"][0].content
    assert persisted_environment_prompts[-1] == (1, build_max_output_tokens_instruction(256))
    assert model_requests[1]["max_tokens"] == 256
    assert saved_created_at == [None]
    assert LLMResponse.model_validate(response).choices[0].message.content == "ok"
    assert response["llm_request_metadata"]["input_tokens"] == 100
    assert response["llm_request_metadata"]["context_window_tokens"] == 4000
    assert response["llm_request_metadata"]["max_output_tokens"] == 256
    assert response["llm_request_metadata"]["output_tokens"] == 10
    assert response["llm_request_metadata"]["cached_tokens"] == 0
    assert response["llm_request_metadata"]["total_input_tokens"] == 200
    assert response["llm_request_metadata"]["total_cached_tokens"] == 100
    assert response["llm_request_metadata"]["cache_hit_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_stream_retry_refreshes_max_tokens_instruction_for_new_channel(monkeypatch):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=SimpleNamespace(chat_timeout=60)),
        tool=SimpleNamespace(
            max_turns=5,
            max_parallel_tools=5,
            executor_max_workers=1,
        ),
    )
    model_requests = []
    channel_call_contexts = []
    persisted_environment_prompts = []
    attempt_started_at = iter(
        [
            datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
            datetime(2026, 7, 21, 6, 1, tzinfo=UTC),
        ]
    )
    saved_created_at = []
    logger = _Logger()

    async def get_user(db, uid):
        return SimpleNamespace(username="admin")

    async def get_profile(db, *, uid, session_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def save_initial(db, session_id, uid, current_profile, message, attachments, source):
        return InternalMessage(id=1, role=MessageRole.USER, content=message)

    async def mark_processed(db, message_id):
        return None

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        channel_call_contexts.append(kwargs["call_context"])
        if kwargs.get("excluded_priorities"):
            return _Channel(), {"model_id": "model-2", "max_tokens": 256, "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=2)
        return _Channel(), {"model_id": "model-1", "max_tokens": 1024, "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile):
        return [], []

    async def prepare_messages(*args, **kwargs):
        return [
            InternalMessage(
                id=1,
                role=MessageRole.USER,
                content="original request",
                environment_prompt=build_max_output_tokens_instruction(kwargs["max_tokens"]),
            )
        ]

    async def fetch_new_messages(*args, **kwargs):
        return []

    async def generate_with_stream_callback(**kwargs):
        model_requests.append(kwargs)
        if kwargs["model_id"] == "model-1":
            raise LLMException(message="ERR_LLM_UNEXPECTED_ERROR")
        return SimpleNamespace(message=InternalMessage(role=MessageRole.ASSISTANT, content="ok"))

    async def save_assistant(db, session_id, uid, profile_id, ai_msg, dedupe_key=None, created_at=None):
        saved_created_at.append(created_at)
        return SimpleNamespace(content="ok")

    async def set_environment_prompt(_db, message_id, environment_prompt):
        persisted_environment_prompts.append((message_id, environment_prompt))
        return True

    monkeypatch.setattr(markdown_instruction_module, "build_user_runtime_instructions", _build_max_tokens_runtime_instruction)
    monkeypatch.setattr(markdown_instruction_module.message_crud, "set_environment_prompt", set_environment_prompt)
    monkeypatch.setattr(stream_module, "logger", logger)
    monkeypatch.setattr(interactive_module, "get_logger", lambda _name: logger)
    monkeypatch.setattr(interactive_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_module, "resolve_profile_for_session", get_profile)
    monkeypatch.setattr(interactive_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_module, "save_initial_message", save_initial)
    monkeypatch.setattr(interactive_module, "mark_initial_message_processed", mark_processed)
    monkeypatch.setattr(interactive_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(interactive_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        interactive_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": model_entry["max_tokens"],
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(interactive_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(
        interactive_helpers_module,
        "fetch_and_merge_new_user_messages",
        fetch_new_messages,
    )
    monkeypatch.setattr(
        interactive_module,
        "apply_context_summary_checkpoint",
        _passthrough_context_summary_checkpoint,
    )
    monkeypatch.setattr(
        interactive_module.ContextManager,
        "trim_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(interactive_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    monkeypatch.setattr(interactive_module, "get_local_time", lambda: next(attempt_started_at))
    monkeypatch.setattr(interactive_module, "save_assistant_message", save_assistant)

    events = [
        event
        async for event in _StreamDispatcher.dispatch_stream(
            db=_Session(),
            message="original request",
            uid="user-1",
            session_id="session-1",
            request_id="request-1",
        )
    ]

    assert [request["model_id"] for request in model_requests] == ["model-1", "model-2"]
    assert channel_call_contexts == ["chat_dispatch_stream", "chat_dispatch_stream_retry"]
    assert "The hard maximum for this response is 1024 output tokens." in model_requests[0]["messages"][0].content
    assert "The hard maximum for this response is 256 output tokens." in model_requests[1]["messages"][0].content
    assert "The hard maximum for this response is 1024 output tokens." not in model_requests[1]["messages"][0].content
    assert persisted_environment_prompts[-1] == (1, build_max_output_tokens_instruction(256))
    assert model_requests[1]["max_tokens"] == 256
    assert saved_created_at == [datetime(2026, 7, 21, 6, 1, tzinfo=UTC)]
    assert [event["type"] for event in events] == [
        "task_start",
        "llm_request_metadata",
        "agent_loop_start",
        "llm_request_metadata",
        "agent_loop_output",
        "content",
        "turn_end",
        "done",
    ]
    metadata_events = [event for event in events if event["type"] == "llm_request_metadata"]
    assert [event["max_output_tokens"] for event in metadata_events] == [1024, 256]
    assert metadata_events[0]["response_id"] == metadata_events[1]["response_id"]


_DEFAULT_AUDIT_RESULT = object()


async def _run_audited_interactive_dispatch(
    monkeypatch,
    checkpoint_callback,
    process_tool,
    unknown_calls_target=None,
    finish_round_result=True,
    generated_calls_target=None,
    audit_result=_DEFAULT_AUDIT_RESULT,
    audit_waiter=None,
    claim_execution_success=True,
    stream_event_callback=None,
    stream_dispatch=False,
    additional_user_messages_fetcher=None,
    persist_pending_confirmation_bundle_handler=None,
    persist_cancelled_pending_audit_results_handler=None,
    supersede_pending_confirmation_bundle_handler=None,
    response_usages=None,
    execution_resume_state=None,
    response_messages=None,
    audit_calls_target=None,
    audit_results=None,
    generate_hook=None,
    finish_attempt_calls_target=None,
    finish_round_if_complete_calls_target=None,
    finish_round_if_complete_result=None,
    use_execution_round_if_complete=False,
    tool_call=None,
    multimodal_capabilities=None,
):
    profile = SimpleNamespace(id=1)
    audit_configured = audit_results is not None or audit_result is not None
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        security=SimpleNamespace(
            audit_channel_id=1 if audit_configured else None,
            audit_model_id="audit-model" if audit_configured else None,
        ),
        tool=SimpleNamespace(
            max_turns=5,
            max_parallel_tools=5,
            executor_max_workers=1,
        ),
    )
    tool_call = tool_call or InternalToolCall(
        id="call-1",
        name="execute_shell",
        arguments={"command": "echo 1"},
    )
    responses = (
        list(response_messages)
        if response_messages is not None
        else [
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ]
    )
    saved_message_id = 10
    response_usage_iterator = iter(response_usages or [])
    audit_results_iterator = iter(audit_results) if audit_results is not None else None
    next_audit_record_id = 42
    audit_details_by_record_id = {}
    executable_audit_record_ids = set()

    async def get_user(db, uid):
        return SimpleNamespace(username="operator")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        return _Channel(), {"model_id": "model-1", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile):
        return [SimpleNamespace(name=tool_call.name)], []

    async def mark_initial_message_processed(db, initial_message_id):
        return None

    async def generate(**kwargs):
        if generated_calls_target is not None:
            generated_calls_target.append(
                {
                    **kwargs,
                    "messages": [message.model_copy(deep=True) for message in kwargs["messages"]],
                }
            )
        response_message = responses.pop(0)
        if generate_hook is not None:
            await generate_hook(response_message)
        return SimpleNamespace(message=response_message, usage=next(response_usage_iterator, None))

    async def generate_with_stream_callback(**kwargs):
        return await generate(**kwargs)

    async def save_assistant(*args, **kwargs):
        nonlocal saved_message_id
        message = args[4]
        saved_message_id += 1
        return SimpleNamespace(id=saved_message_id, content=message.content)

    async def save_tool_response(db, session_id, uid, profile_id, tool_result, messages, turn_messages):
        messages.append(tool_result)
        turn_messages.append(tool_result)
        return SimpleNamespace(id=200)

    async def audit_round(*args, **kwargs):
        nonlocal next_audit_record_id
        audit_record_id = next_audit_record_id
        next_audit_record_id += 1
        round_tool_calls = [item.model_copy(deep=True) for item in kwargs["tool_calls"]]
        if audit_calls_target is not None:
            audit_calls_target.append(round_tool_calls)
        audit_details_by_record_id[audit_record_id] = [SimpleNamespace(original_tool_call_id=tool_call.id, id=(audit_record_id * 100) + index) for index, tool_call in enumerate(round_tool_calls, start=1)]
        if audit_waiter is not None:
            await audit_waiter()
        selected_audit_result = audit_result
        if audit_results_iterator is not None:
            try:
                selected_audit_result = next(audit_results_iterator)
            except StopIteration:
                pass
        if selected_audit_result is _DEFAULT_AUDIT_RESULT:
            selected_audit_result = SimpleNamespace(
                may_execute=True,
                audit_record_id=audit_record_id,
                tool_results=[],
                confirmation_payload=None,
            )
        if selected_audit_result is not None:
            audit_details_by_record_id[selected_audit_result.audit_record_id] = audit_details_by_record_id[audit_record_id]
            if selected_audit_result.may_execute:
                executable_audit_record_ids.add(selected_audit_result.audit_record_id)
        return selected_audit_result

    async def persist_confirmation_bundle(
        db,
        *,
        tool_results,
        confirmation_payload,
        **kwargs,
    ):
        stored_results = []
        for index, tool_result in enumerate(tool_results, start=1):
            stored_result = tool_result.model_copy(deep=True)
            stored_result.id = 200 + index
            stored_results.append(stored_result)
        confirmation_message = InternalMessage(
            id=300,
            role=MessageRole.ASSISTANT,
            content=json.dumps(confirmation_payload, ensure_ascii=False),
        )
        return stored_results, confirmation_message

    async def claim_execution(db, *, audit_record_id):
        if audit_record_id not in executable_audit_record_ids:
            raise AssertionError("non-executable audit must not create a claim")
        if not claim_execution_success:
            return None, None
        return SimpleNamespace(execution_claim_token="claim-token"), "claim-token"

    async def list_details(db, audit_record_id):
        return audit_details_by_record_id[audit_record_id]

    async def create_execution(db, **kwargs):
        if kwargs["audit_record_id"] not in executable_audit_record_ids:
            raise AssertionError("non-executable audit must not create execution records")
        return SimpleNamespace(id=8)

    async def finish_attempt(db, **kwargs):
        if finish_attempt_calls_target is not None:
            finish_attempt_calls_target.append(dict(kwargs))
        return True

    async def finish_round(db, **kwargs):
        return finish_round_result

    async def finish_round_if_complete(db, **kwargs):
        if finish_round_if_complete_calls_target is not None:
            finish_round_if_complete_calls_target.append(dict(kwargs))
        return finish_round_if_complete_result

    async def update_confirmation(db, *, audit_record_id):
        return None

    unknown_calls = unknown_calls_target if unknown_calls_target is not None else []

    async def mark_unknown(db, **kwargs):
        unknown_calls.append(kwargs)
        return True

    monkeypatch.setattr(interactive_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_module, "mark_initial_message_processed", mark_initial_message_processed)
    monkeypatch.setattr(interactive_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(
        interactive_module,
        "get_multimodal_from_entry",
        lambda model_entry: multimodal_capabilities or (False, False, False),
    )
    monkeypatch.setattr(
        interactive_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )

    async def prepare_messages(*args, **kwargs):
        return [InternalMessage(role=MessageRole.USER, content="request")]

    async def materialize_environment_prompt(db, session_id, messages, max_tokens):
        if multimodal_capabilities is not None:
            return [message.model_copy(deep=True) for message in messages]
        return messages

    monkeypatch.setattr(interactive_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(interactive_module, "apply_context_summary_checkpoint", _passthrough_context_summary_checkpoint)
    monkeypatch.setattr(interactive_module, "materialize_latest_user_environment_prompt", materialize_environment_prompt)
    monkeypatch.setattr(interactive_module.ContextManager, "trim_messages_for_model_request", lambda **kwargs: kwargs["messages"])
    monkeypatch.setattr(interactive_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    monkeypatch.setattr(interactive_module, "save_assistant_message", save_assistant)
    monkeypatch.setattr(
        interactive_module,
        "persist_pending_confirmation_bundle",
        persist_pending_confirmation_bundle_handler or persist_confirmation_bundle,
    )
    if persist_cancelled_pending_audit_results_handler is not None:
        monkeypatch.setattr(
            interactive_module,
            "persist_cancelled_pending_audit_results",
            persist_cancelled_pending_audit_results_handler,
        )
    if supersede_pending_confirmation_bundle_handler is not None:
        monkeypatch.setattr(
            interactive_module,
            "supersede_persisted_pending_confirmation_bundle",
            supersede_pending_confirmation_bundle_handler,
        )
    monkeypatch.setattr(interactive_module, "save_tool_response", save_tool_response)
    monkeypatch.setattr(interactive_module, "audit_tool_round", audit_round)
    monkeypatch.setattr(interactive_module.audit_crud, "claim_passed_for_execution", claim_execution)
    monkeypatch.setattr(interactive_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(interactive_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(interactive_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(interactive_module.audit_crud, "finish_execution_round", finish_round)
    if use_execution_round_if_complete:
        monkeypatch.setattr(interactive_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(interactive_module.audit_crud, "mark_execution_unknown", mark_unknown)
    monkeypatch.setattr(interactive_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(interactive_helpers_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(interactive_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(interactive_helpers_module, "process_single_tool_with_isolated_db", process_tool)

    async def get_session_by_id(*args, **kwargs):
        return None

    db = _DatabaseLikeSession() if use_execution_round_if_complete else _Session()
    if use_execution_round_if_complete:
        monkeypatch.setattr(interactive_module.session_crud, "get_by_session_id", get_session_by_id)

    if stream_dispatch:
        response = [
            event
            async for event in _StreamDispatcher.dispatch_stream(
                db=db,
                message="request",
                uid="user-1",
                session_id="session-1",
                request_id="request-1",
                persisted_initial_message=InternalMessage(id=1, role=MessageRole.USER, content="request"),
                frozen_user_message_ids=[1],
                persisted_profile_id=1,
                execution_checkpoint_callback=checkpoint_callback,
                additional_user_messages_fetcher=additional_user_messages_fetcher,
            )
        ]
    else:
        response = await _Dispatcher.dispatch(
            db=db,
            message="request",
            uid="user-1",
            session_id="session-1",
            persisted_initial_message=InternalMessage(id=1, role=MessageRole.USER, content="request"),
            frozen_user_message_ids=[1],
            persisted_profile_id=1,
            execution_checkpoint_callback=checkpoint_callback,
            stream_event_callback=stream_event_callback,
            additional_user_messages_fetcher=additional_user_messages_fetcher,
            execution_resume_state=execution_resume_state,
        )
    return response, unknown_calls


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
async def test_interactive_persists_handoff_binding_when_round_is_not_complete(monkeypatch):
    checkpoints = []
    finish_attempt_calls = []
    finish_round_if_complete_calls = []
    terminal_session_id = "h" * 32

    async def save_checkpoint(checkpoint):
        checkpoints.append(dict(checkpoint))

    async def process_tool(tool_call, *args, **kwargs):
        handoff = ShellInteractiveHandoffResult(
            terminal_session_id=terminal_session_id,
            status=TerminalSessionStatus.STARTING,
            output_buffer=TerminalOutputBufferState(
                capacity_bytes=1_048_576,
                oldest_offset=0,
                next_offset=0,
                oldest_sequence=1,
                next_sequence=1,
            ),
            output_stream="merged_stdout_stderr",
        )
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=handoff.model_dump_json(),
        )

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        finish_attempt_calls_target=finish_attempt_calls,
        finish_round_if_complete_calls_target=finish_round_if_complete_calls,
        finish_round_if_complete_result=None,
        use_execution_round_if_complete=True,
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert finish_attempt_calls == []
    assert finish_round_if_complete_calls == [{"audit_record_id": 42, "claim_token": "claim-token"}]
    handoff_checkpoints = [checkpoint for checkpoint in checkpoints if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint]
    assert handoff_checkpoints[-1][SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
        "handoff_state": "persisted",
        "terminal_session_ids": [terminal_session_id],
    }
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_accumulates_output_tokens_and_session_cache_metrics(monkeypatch):
    checkpoints = []
    events = []

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    async def publish_event(event):
        events.append(event)

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        response_usages=[
            {
                "prompt_tokens": 1000,
                "completion_tokens": 12,
                "cached_tokens": 200,
            },
            {
                "prompt_tokens": 1100,
                "completion_tokens": 20,
                "cached_tokens": 440,
            },
        ],
    )

    metadata_events = [event for event in events if event["type"] == "llm_request_metadata"]
    assert [event["input_tokens_source"] for event in metadata_events] == [
        "estimated",
        "provider",
        "estimated",
        "provider",
    ]
    provider_metadata_events = [event for event in metadata_events if event["input_tokens_source"] == "provider"]
    assert [event["input_tokens"] for event in provider_metadata_events] == [1000, 1100]
    assert [event["total_input_tokens"] for event in metadata_events] == [0, 1000, 1000, 2100]
    assert [event["total_cached_tokens"] for event in metadata_events] == [0, 200, 200, 640]
    assert metadata_events[1]["output_tokens"] == 12
    assert metadata_events[1]["total_output_tokens"] == 12
    assert metadata_events[2]["output_tokens"] == 12
    assert metadata_events[2]["total_output_tokens"] == 12
    assert metadata_events[2]["cached_tokens"] == 200
    assert metadata_events[2]["cache_hit_rate"] == pytest.approx(0.2)
    assert metadata_events[3]["output_tokens"] == 32
    assert metadata_events[3]["total_output_tokens"] == 32
    assert metadata_events[3]["cached_tokens"] == 440
    assert metadata_events[3]["cache_hit_rate"] == pytest.approx(640 / 2100)
    assert any(checkpoint["total_output_tokens"] == 12 for checkpoint in checkpoints)
    assert any(checkpoint["session_total_output_tokens"] == 12 for checkpoint in checkpoints)
    assert any(checkpoint["session_total_input_tokens"] == 1000 and checkpoint["session_total_cached_tokens"] == 200 for checkpoint in checkpoints)
    assert response["llm_request_metadata"]["output_tokens"] == 32
    assert response["llm_request_metadata"]["total_output_tokens"] == 32
    assert response["llm_request_metadata"]["cache_hit_rate"] == pytest.approx(640 / 2100)
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_preserves_output_tokens_when_first_usage_has_no_prompt_tokens(monkeypatch):
    events = []

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    async def publish_event(event):
        events.append(event)

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        response_usages=[
            {"completion_tokens": 12},
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 30,
            },
        ],
    )

    metadata_events = [event for event in events if event["type"] == "llm_request_metadata"]
    assert metadata_events[2]["input_tokens_source"] == "estimated"
    assert metadata_events[2]["output_tokens"] == 12
    assert metadata_events[3]["input_tokens_source"] == "provider"
    assert metadata_events[3]["output_tokens"] == 32
    assert response["llm_request_metadata"]["output_tokens"] == 32
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_resume_continues_total_output_tokens(monkeypatch):
    events = []

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    async def publish_event(event):
        events.append(event)

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        response_usages=[
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "cached_tokens": 20,
            },
            {
                "prompt_tokens": 120,
                "completion_tokens": 3,
                "cached_tokens": 30,
            },
        ],
        execution_resume_state={
            "messages": [InternalMessage(role=MessageRole.USER, content="request").model_dump(mode="json")],
            "turn_messages": [],
            "files_to_user": [],
            "current_turn": 0,
            "total_output_tokens": 50,
            "session_total_output_tokens": 12,
            "session_total_input_tokens": 500,
            "session_total_cached_tokens": 100,
        },
    )

    provider_metadata_events = [event for event in events if event["type"] == "llm_request_metadata" and event["input_tokens_source"] == "provider"]
    assert [event["output_tokens"] for event in provider_metadata_events] == [57, 60]
    assert [event["total_output_tokens"] for event in provider_metadata_events] == [19, 22]
    assert [event["total_input_tokens"] for event in provider_metadata_events] == [600, 720]
    assert [event["total_cached_tokens"] for event in provider_metadata_events] == [120, 150]
    assert provider_metadata_events[0]["cache_hit_rate"] == pytest.approx(120 / 600)
    assert provider_metadata_events[1]["cache_hit_rate"] == pytest.approx(150 / 720)
    assert response["llm_request_metadata"]["output_tokens"] == 60
    assert response["llm_request_metadata"]["total_output_tokens"] == 22
    assert response["llm_request_metadata"]["total_input_tokens"] == 720
    assert response["llm_request_metadata"]["total_cached_tokens"] == 150
    assert response["llm_request_metadata"]["cache_hit_rate"] == pytest.approx(150 / 720)
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_without_audit_configuration_executes_tool_without_audit_binding(monkeypatch):
    tool_calls = []
    checkpoints = []

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert tool_calls == ["call-1"]
    assert all(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in checkpoint for checkpoint in checkpoints)
    assert unknown_calls == []


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
                {
                    "type": "multimodal_file_read",
                    "status": "success",
                    "modality": "image",
                    "path": str(image_path.resolve()),
                    "message": tool_message,
                },
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
            {
                "type": "multimodal_file_read",
                "status": "success",
                "modality": "image",
                "path": str(image_path.resolve()),
                "message": "下一条 role=user 消息不是用户新输入",
            },
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


@pytest.mark.asyncio
async def test_interactive_persists_audit_binding_before_tool_and_clears_after_round(monkeypatch):
    current_state = {}
    persisted_states = []
    tool_states = []

    async def save_checkpoint(checkpoint):
        marker_present = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint
        marker = checkpoint.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
        current_state.update({"dispatcher_checkpoint": checkpoint})
        if marker_present:
            if marker is None:
                current_state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
            else:
                current_state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = marker
        persisted_states.append(dict(current_state))

    async def process_tool(tool_call, *args, **kwargs):
        tool_states.append(current_state.get(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY))
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    response, unknown_calls = await _run_audited_interactive_dispatch(monkeypatch, save_checkpoint, process_tool)

    assert response["choices"][0]["message"]["content"] == "finished"
    assert tool_states == [{"audit_record_id": 42, "claim_token": "claim-token"}]
    assert persisted_states[0][SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
    }
    assert SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in persisted_states[-1]
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_audits_each_successive_tool_round_before_execution(monkeypatch):
    checkpoints = []
    tool_names = []
    audit_calls = []

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_names.append(tool_call.name)
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        response_messages=[
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo 1"})],
            ),
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="call-2", name="write_file", arguments={"file_path": "note.txt", "content": "done"})],
            ),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ],
        audit_calls_target=audit_calls,
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert tool_names == ["execute_shell", "write_file"]
    assert [[tool_call.name for tool_call in tool_calls] for tool_calls in audit_calls] == [
        ["execute_shell"],
        ["write_file"],
    ]
    assert [checkpoint[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] for checkpoint in checkpoints if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint] == [
        {"audit_record_id": 42, "claim_token": "claim-token"},
        None,
        {"audit_record_id": 43, "claim_token": "claim-token"},
        None,
    ]
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_audit_binding_persistence_failure_prevents_tool_execution(monkeypatch):
    tool_calls = []
    unknown_calls = []

    async def save_checkpoint(checkpoint):
        if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint:
            raise RuntimeError("checkpoint storage failed")

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    with pytest.raises(interactive_module.AuditExecutionStatePersistenceError):
        await _run_audited_interactive_dispatch(monkeypatch, save_checkpoint, process_tool, unknown_calls)

    assert tool_calls == []
    assert unknown_calls[0]["audit_record_id"] == 42
    assert unknown_calls[0]["claim_token"] == "claim-token"


@pytest.mark.asyncio
async def test_interactive_keeps_active_binding_when_audit_round_finish_fails(monkeypatch):
    current_state = {}
    generated_calls = []

    async def save_checkpoint(checkpoint):
        marker_present = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint
        marker = checkpoint.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
        current_state.update({"dispatcher_checkpoint": checkpoint})
        if marker_present:
            if marker is None:
                current_state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
            else:
                current_state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = marker

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    with pytest.raises(interactive_module.AuditExecutionStatePersistenceError):
        await _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            finish_round_result=False,
            generated_calls_target=generated_calls,
        )

    assert len(generated_calls) == 1
    assert current_state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
    }


@pytest.mark.asyncio
async def test_streamed_pending_audit_publishes_tool_events_before_confirmation(monkeypatch):
    confirmation_payload = {
        "type": "audit_confirmation",
        "audit_record_id": 42,
        "summary": "Confirm command",
        "risk": 8,
        "status": "pending",
    }
    audit_result = SimpleNamespace(
        may_execute=False,
        audit_record_id=42,
        tool_results=[
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps({"status": "pending", "tool_name": "execute_shell"}),
            )
        ],
        confirmation_payload=confirmation_payload,
    )

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(*args, **kwargs):
        raise AssertionError("pending audit must not execute tools")

    events, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=audit_result,
        stream_dispatch=True,
    )

    assert [event["type"] for event in events] == [
        "task_start",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
        "tool_end",
        "done",
    ]
    assert events[5]["tool_call_id"] == "call-1"
    assert events[5]["tool_call_index"] == 0
    assert events[5]["tool_call_count"] == 1
    assert json.loads(events[6]["result"])["status"] == "pending"
    assert json.loads(events[-1]["response"]["choices"][0]["message"]["content"]) == confirmation_payload
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_streamed_tool_events_include_batch_order_for_parallel_tools(monkeypatch):
    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"status":"success"}',
        )

    events, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_dispatch=True,
        response_messages=[
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content="我先检查",
                tool_calls=[
                    InternalToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo 1"},
                    ),
                    InternalToolCall(
                        id="call-2",
                        name="read_text_file",
                        arguments={"file_path": "note.txt"},
                    ),
                ],
            ),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ],
    )

    tool_start_events = [event for event in events if event["type"] == "tool_start"]
    assert [event["tool_call_index"] for event in tool_start_events] == [0, 1]
    assert [event["tool_call_count"] for event in tool_start_events] == [2, 2]
    assert tool_start_events[0]["response_id"] == tool_start_events[1]["response_id"]
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_pending_audit_cancels_unpersisted_confirmation_when_batch_arrives_during_audit(monkeypatch):
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    additional_messages_arrived = asyncio.Event()
    queued_batch = UserInputBatch(
        messages=(
            InternalMessage(id=32, role=MessageRole.USER, content="first append"),
            InternalMessage(id=30, role=MessageRole.USER, content="second append"),
            InternalMessage(id=31, role=MessageRole.USER, content="third append"),
        ),
        source_message_ids=(32, 30, 31),
    )
    audit_result = SimpleNamespace(
        may_execute=False,
        audit_record_id=42,
        tool_results=[
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps({"status": "pending", "tool_name": "execute_shell"}),
            )
        ],
        confirmation_payload={"type": "audit_confirmation", "audit_record_id": 42, "status": "pending"},
    )
    checkpoints = []
    generated_calls = []
    cancelled_audit_records = []
    tool_calls = []
    batch_delivery_count = 0
    fetch_count = 0
    none_fetch_count = 0

    async def wait_during_audit():
        audit_started.set()
        await asyncio.wait_for(release_audit.wait(), timeout=1)

    async def fetch_additional_messages():
        nonlocal batch_delivery_count, fetch_count, none_fetch_count
        fetch_count += 1
        if additional_messages_arrived.is_set() and batch_delivery_count == 0:
            batch_delivery_count += 1
            return queued_batch
        none_fetch_count += 1
        return None

    async def persist_pending_confirmation_bundle(*args, **kwargs):
        raise AssertionError("new messages must cancel the pending audit before confirmation persistence")

    async def persist_cancelled_pending_audit_results(
        db,
        *,
        audit_record_id,
        uid,
        session_id,
        profile_id,
        tool_results,
    ):
        cancelled_audit_records.append(audit_record_id)
        assert (uid, session_id, profile_id) == ("user-1", "session-1", 1)
        assert [tool_result.tool_call_id for tool_result in tool_results] == ["call-1"]
        return [
            InternalMessage(
                id=201,
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps(
                    {
                        "status": "cancelled",
                        "confirmation_status": "superseded",
                        "tool_name": "execute_shell",
                    }
                ),
            )
        ]

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        raise AssertionError("cancelled pending audit must not execute tools")

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_result=audit_result,
            audit_waiter=wait_during_audit,
            generated_calls_target=generated_calls,
            additional_user_messages_fetcher=fetch_additional_messages,
            persist_pending_confirmation_bundle_handler=persist_pending_confirmation_bundle,
            persist_cancelled_pending_audit_results_handler=persist_cancelled_pending_audit_results,
        )
    )

    await asyncio.wait_for(audit_started.wait(), timeout=1)
    additional_messages_arrived.set()
    release_audit.set()
    response, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert cancelled_audit_records == [42]
    assert tool_calls == []
    assert len(generated_calls) == 2
    next_request = generated_calls[1]["messages"]
    assert [tool_call.id for message in next_request if message.role == MessageRole.ASSISTANT and message.tool_calls for tool_call in message.tool_calls] == ["call-1"]
    cancelled_results = [message for message in next_request if message.role == MessageRole.TOOL and message.tool_call_id == "call-1"]
    assert len(cancelled_results) == 1
    assert json.loads(cancelled_results[0].content)["status"] == "cancelled"
    assert json.loads(cancelled_results[0].content)["confirmation_status"] == "superseded"
    appended_message_ids = [message.id for message in next_request if message.role == MessageRole.USER and message.id in queued_batch.source_message_ids]
    assert appended_message_ids == [32, 30, 31]
    assert all(appended_message_ids.count(message_id) == 1 for message_id in queued_batch.source_message_ids)
    assert fetch_count == 4
    assert none_fetch_count == 3
    assert batch_delivery_count == 1
    assert checkpoints[-1]["current_turn"] == 0
    assert checkpoints[-1]["context_summary_fixed_upper_message_id"] == 30
    assert response["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_allowed_tool_executes_once_before_appended_batch_reaches_next_model_round(monkeypatch):
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    additional_messages_arrived = asyncio.Event()
    queued_batch = UserInputBatch(
        messages=(
            InternalMessage(id=52, role=MessageRole.USER, content="first append"),
            InternalMessage(id=50, role=MessageRole.USER, content="second append"),
            InternalMessage(id=51, role=MessageRole.USER, content="third append"),
        ),
        source_message_ids=(52, 50, 51),
    )
    generated_calls = []
    tool_calls = []
    batch_delivery_count = 0
    fetch_count = 0
    none_fetch_count = 0

    async def fetch_additional_messages():
        nonlocal batch_delivery_count, fetch_count, none_fetch_count
        fetch_count += 1
        if additional_messages_arrived.is_set() and batch_delivery_count == 0:
            batch_delivery_count += 1
            return queued_batch
        none_fetch_count += 1
        return None

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        tool_started.set()
        await asyncio.wait_for(release_tool.wait(), timeout=1)
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"status":"success"}',
        )

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            generated_calls_target=generated_calls,
            additional_user_messages_fetcher=fetch_additional_messages,
        )
    )

    await asyncio.wait_for(tool_started.wait(), timeout=1)
    additional_messages_arrived.set()
    release_tool.set()
    response, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert tool_calls == ["call-1"]
    assert len(generated_calls) == 2
    next_request = generated_calls[1]["messages"]
    completed_results = [message for message in next_request if message.role == MessageRole.TOOL and message.tool_call_id == "call-1"]
    assert len(completed_results) == 1
    assert json.loads(completed_results[0].content)["status"] == "success"
    appended_message_ids = [message.id for message in next_request if message.role == MessageRole.USER and message.id in queued_batch.source_message_ids]
    assert appended_message_ids == [52, 50, 51]
    assert all(appended_message_ids.count(message_id) == 1 for message_id in queued_batch.source_message_ids)
    assert fetch_count == 3
    assert none_fetch_count == 2
    assert batch_delivery_count == 1
    assert response["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_streamed_pending_audit_bundle_is_superseded_when_batch_arrives_during_persistence(monkeypatch):
    confirmation_payload = {
        "type": "audit_confirmation",
        "audit_record_id": 42,
        "summary": "Confirm command",
        "risk": 8,
        "status": "pending",
    }
    audit_result = SimpleNamespace(
        may_execute=False,
        audit_record_id=42,
        tool_results=[
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps({"status": "pending", "tool_name": "execute_shell"}),
            )
        ],
        confirmation_payload=confirmation_payload,
    )
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    additional_messages_arrived = asyncio.Event()
    queued_batch = UserInputBatch(
        messages=(
            InternalMessage(id=62, role=MessageRole.USER, content="continue first"),
            InternalMessage(id=60, role=MessageRole.USER, content="continue second"),
            InternalMessage(id=61, role=MessageRole.USER, content="continue third"),
        ),
        source_message_ids=(62, 60, 61),
    )
    batch_delivery_count = 0
    fetch_count = 0
    none_fetch_count = 0
    lifecycle = []
    checkpoints = []
    generated_calls = []
    tool_calls = []

    async def fetch_additional_messages():
        nonlocal batch_delivery_count, fetch_count, none_fetch_count
        fetch_count += 1
        if additional_messages_arrived.is_set() and batch_delivery_count == 0:
            batch_delivery_count += 1
            return queued_batch
        none_fetch_count += 1
        return None

    async def persist_confirmation_bundle(db, *, tool_results, confirmation_payload, **kwargs):
        persistence_started.set()
        await asyncio.wait_for(release_persistence.wait(), timeout=1)
        lifecycle.append("persisted")
        stored_results = []
        for index, tool_result in enumerate(tool_results, start=1):
            stored_tool_result = tool_result.model_copy(deep=True)
            stored_tool_result.id = 200 + index
            stored_results.append(stored_tool_result)
        return stored_results, InternalMessage(
            id=300,
            role=MessageRole.ASSISTANT,
            content=json.dumps(confirmation_payload, ensure_ascii=False),
        )

    async def supersede_confirmation_bundle(db, *, audit_record_id, uid, session_id):
        lifecycle.append("superseded")
        assert (audit_record_id, uid, session_id) == (42, "user-1", "session-1")
        return [
            InternalMessage(
                id=201,
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps(
                    {
                        "status": "cancelled",
                        "confirmation_status": "superseded",
                        "tool_name": "execute_shell",
                    }
                ),
            )
        ]

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        raise AssertionError("superseded pending audit must not execute tools")

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_result=audit_result,
            generated_calls_target=generated_calls,
            additional_user_messages_fetcher=fetch_additional_messages,
            persist_pending_confirmation_bundle_handler=persist_confirmation_bundle,
            supersede_pending_confirmation_bundle_handler=supersede_confirmation_bundle,
            stream_dispatch=True,
        )
    )

    await asyncio.wait_for(persistence_started.wait(), timeout=1)
    additional_messages_arrived.set()
    release_persistence.set()
    events, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert fetch_count == 5
    assert none_fetch_count == 4
    assert batch_delivery_count == 1
    assert lifecycle == ["persisted", "superseded"]
    assert len(generated_calls) == 2
    appended_message_ids = [message.id for message in generated_calls[1]["messages"] if message.role == MessageRole.USER and message.id in queued_batch.source_message_ids]
    assert appended_message_ids == [62, 60, 61]
    assert all(appended_message_ids.count(message_id) == 1 for message_id in queued_batch.source_message_ids)
    cancelled_result = next(message for message in generated_calls[1]["messages"] if message.role == MessageRole.TOOL)
    cancelled_payload = json.loads(cancelled_result.content)
    assert cancelled_payload["status"] == "cancelled"
    assert cancelled_payload["confirmation_status"] == "superseded"
    assert checkpoints[-1]["current_turn"] == 0
    assert checkpoints[-1]["context_summary_fixed_upper_message_id"] == 60
    assert tool_calls == []
    assert [event["type"] for event in events] == [
        "task_start",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
        "tool_end",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "content",
        "turn_end",
        "done",
    ]
    first_round_response_id = events[1]["response_id"]
    assert first_round_response_id == events[2]["response_id"] == events[3]["response_id"]
    assert first_round_response_id == events[4]["response_id"] == events[5]["response_id"] == events[6]["response_id"]
    final_round_response_id = events[7]["response_id"]
    assert final_round_response_id == events[8]["response_id"] == events[9]["response_id"]
    assert final_round_response_id == events[10]["response_id"] == events[11]["response_id"] == events[12]["response_id"]
    assert first_round_response_id != final_round_response_id
    assert events[-1]["response"]["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_pending_audit_publishes_tool_start_before_audit_finishes(monkeypatch):
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    published_events = []
    audit_result = SimpleNamespace(
        may_execute=False,
        audit_record_id=42,
        tool_results=[
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps({"status": "pending", "tool_name": "execute_shell"}),
            )
        ],
        confirmation_payload={"type": "audit_confirmation", "audit_record_id": 42, "status": "pending"},
    )

    async def wait_during_audit():
        audit_started.set()
        await release_audit.wait()

    async def publish_event(event):
        published_events.append(event)

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(*args, **kwargs):
        raise AssertionError("pending audit must not execute tools")

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_result=audit_result,
            audit_waiter=wait_during_audit,
            stream_event_callback=publish_event,
        )
    )

    await asyncio.wait_for(audit_started.wait(), timeout=1)
    assert [event["type"] for event in published_events] == [
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
    ]

    release_audit.set()
    response, unknown_calls = await dispatch_task

    assert [event["type"] for event in published_events] == [
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
        "tool_end",
    ]
    assert json.loads(response["choices"][0]["message"]["content"])["type"] == "audit_confirmation"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_streamed_audit_claim_failure_closes_started_tool_event(monkeypatch):
    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(*args, **kwargs):
        raise AssertionError("claim failure must not execute tools")

    events, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        claim_execution_success=False,
        stream_dispatch=True,
    )

    event_types = [event["type"] for event in events]
    assert event_types == [
        "task_start",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
        "tool_end",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "content",
        "turn_end",
        "done",
    ]
    assert events[1]["response_id"] == events[2]["response_id"]
    assert events[7]["response_id"] == events[8]["response_id"] == events[9]["response_id"]
    assert events[1]["response_id"] != events[7]["response_id"]
    assert events[4]["message_id"] == 11
    assert events[11]["message_id"] == 12
    assert json.loads(events[6]["result"])["status"] == "failed"
    assert events[6]["tool_call_id"] == events[5]["tool_call_id"] == "call-1"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_batches_advance_confirmation_and_summary_boundaries_without_replay(monkeypatch):
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    confirmation_persistence_started = asyncio.Event()
    release_confirmation_persistence = asyncio.Event()
    batch_b = UserInputBatch(
        messages=(
            InternalMessage(id=102, role=MessageRole.USER, content="batch-b-first"),
            InternalMessage(id=100, role=MessageRole.USER, content="batch-b-second"),
        ),
        source_message_ids=(102, 100),
    )
    batch_c = UserInputBatch(
        messages=(
            InternalMessage(id=112, role=MessageRole.USER, content="batch-c-first"),
            InternalMessage(id=110, role=MessageRole.USER, content="batch-c-second"),
        ),
        source_message_ids=(112, 110),
    )
    batch_d = UserInputBatch(
        messages=(
            InternalMessage(id=122, role=MessageRole.USER, content="batch-d-first"),
            InternalMessage(id=120, role=MessageRole.USER, content="batch-d-second"),
        ),
        source_message_ids=(122, 120),
    )
    pending_results = [
        SimpleNamespace(
            may_execute=False,
            audit_record_id=42,
            tool_results=[
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="call-pending-a",
                    content=json.dumps({"status": "pending", "tool_name": "execute_shell"}),
                )
            ],
            confirmation_payload={"type": "audit_confirmation", "audit_record_id": 42, "status": "pending"},
        ),
        SimpleNamespace(
            may_execute=False,
            audit_record_id=43,
            tool_results=[
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="call-pending-b",
                    content=json.dumps({"status": "pending", "tool_name": "execute_shell"}),
                )
            ],
            confirmation_payload={"type": "audit_confirmation", "audit_record_id": 43, "status": "pending"},
        ),
        SimpleNamespace(
            may_execute=True,
            audit_record_id=44,
            tool_results=[],
            confirmation_payload=None,
        ),
    ]
    queued_batches: list[UserInputBatch] = []
    delivered_batches: list[UserInputBatch] = []
    checkpoints = []
    generated_calls = []
    cancellation_records = []
    executed_tool_calls = []
    audit_round_count = 0

    async def fetch_additional_messages():
        if not queued_batches:
            return None
        batch = queued_batches.pop(0)
        delivered_batches.append(batch)
        return batch

    async def wait_during_first_audit():
        nonlocal audit_round_count
        audit_round_count += 1
        if audit_round_count != 1:
            return
        audit_started.set()
        await asyncio.wait_for(release_audit.wait(), timeout=1)

    async def persist_cancelled_pending_audit_results(*_args, audit_record_id, tool_results, **_kwargs):
        cancellation_records.append(("before_persist", audit_record_id))
        return [
            InternalMessage(
                id=201,
                role=MessageRole.TOOL,
                tool_call_id=tool_results[0].tool_call_id,
                content=json.dumps({"status": "cancelled", "confirmation_status": "superseded"}),
            )
        ]

    async def persist_pending_confirmation_bundle(*_args, audit_record_id, tool_results, confirmation_payload, **_kwargs):
        assert audit_record_id == 43
        confirmation_persistence_started.set()
        await asyncio.wait_for(release_confirmation_persistence.wait(), timeout=1)
        return [
            InternalMessage(
                id=202,
                role=MessageRole.TOOL,
                tool_call_id=tool_results[0].tool_call_id,
                content=tool_results[0].content,
            )
        ], InternalMessage(
            id=301,
            role=MessageRole.ASSISTANT,
            content=json.dumps(confirmation_payload),
        )

    async def supersede_confirmation_bundle(*_args, audit_record_id, **_kwargs):
        cancellation_records.append(("after_persist", audit_record_id))
        return [
            InternalMessage(
                id=203,
                role=MessageRole.TOOL,
                tool_call_id="call-pending-b",
                content=json.dumps({"status": "cancelled", "confirmation_status": "superseded"}),
            )
        ]

    async def save_checkpoint(checkpoint):
        checkpoints.append(dict(checkpoint))

    async def process_tool(tool_call, *_args, **_kwargs):
        executed_tool_calls.append(tool_call.id)
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"status":"success"}',
        )

    async def enqueue_batch_d_after_intermediate_response(response_message):
        if response_message.content == "intermediate":
            queued_batches.append(batch_d)

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_waiter=wait_during_first_audit,
            audit_results=pending_results,
            generated_calls_target=generated_calls,
            generate_hook=enqueue_batch_d_after_intermediate_response,
            additional_user_messages_fetcher=fetch_additional_messages,
            persist_pending_confirmation_bundle_handler=persist_pending_confirmation_bundle,
            persist_cancelled_pending_audit_results_handler=persist_cancelled_pending_audit_results,
            supersede_pending_confirmation_bundle_handler=supersede_confirmation_bundle,
            response_messages=[
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="call-pending-a", name="execute_shell", arguments={"command": "echo a"})],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="call-pending-b", name="execute_shell", arguments={"command": "echo b"})],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="call-allowed", name="execute_shell", arguments={"command": "echo allowed"})],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="intermediate"),
                InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
            ],
        )
    )

    await asyncio.wait_for(audit_started.wait(), timeout=1)
    queued_batches.append(batch_b)
    release_audit.set()
    await asyncio.wait_for(confirmation_persistence_started.wait(), timeout=1)
    queued_batches.append(batch_c)
    release_confirmation_persistence.set()
    response, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert delivered_batches == [batch_b, batch_c, batch_d]
    assert cancellation_records == [("before_persist", 42), ("after_persist", 43)]
    assert executed_tool_calls == ["call-allowed"]
    batch_ids = [*batch_b.source_message_ids, *batch_c.source_message_ids, *batch_d.source_message_ids]
    request_batch_ids = [[message.id for message in call["messages"] if message.role == MessageRole.USER and message.id in batch_ids] for call in generated_calls]
    assert request_batch_ids == [
        [],
        list(batch_b.source_message_ids),
        [*batch_b.source_message_ids, *batch_c.source_message_ids],
        [*batch_b.source_message_ids, *batch_c.source_message_ids],
        [*batch_b.source_message_ids, *batch_c.source_message_ids, *batch_d.source_message_ids],
    ]
    assert all(len(message_ids) == len(set(message_ids)) for message_ids in request_batch_ids)
    checkpoint_upper_ids = [checkpoint["context_summary_fixed_upper_message_id"] for checkpoint in checkpoints]
    assert checkpoint_upper_ids == sorted(checkpoint_upper_ids)
    assert list(dict.fromkeys(checkpoint_upper_ids)) == [100, 110, 120]
    assert response["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []
