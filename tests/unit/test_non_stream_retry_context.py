from types import SimpleNamespace

import pytest

from app.core.dispatchers import non_stream as non_stream_module
from app.core.exceptions import LLMException
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse


class _Dispatcher(non_stream_module.NonStreamDispatcherMixin):
    @classmethod
    async def validate_initial_message_before_save(cls, db, message, uid, session_id, profile, attachments):
        return None


class _Channel:
    id = 1
    name = "test-channel"
    base_url = "https://example.invalid"
    protocol = "openai"

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
    logger = _Logger()

    async def get_user(db, uid):
        return SimpleNamespace(username="admin")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        if kwargs.get("excluded_priorities"):
            return _Channel(), {"model_id": "model-2"}, SimpleNamespace(priority=2)
        return _Channel(), {"model_id": "model-1"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile):
        return [], []

    async def prepare_messages(*args, **kwargs):
        prepare_calls.append((args, kwargs))
        return [InternalMessage(role=MessageRole.USER, content="unexpected replay")]

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

    monkeypatch.setattr(non_stream_module, "logger", logger)
    monkeypatch.setattr(non_stream_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(non_stream_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(non_stream_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(non_stream_module, "select_channel", select_channel)
    monkeypatch.setattr(non_stream_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(non_stream_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        non_stream_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(non_stream_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(
        non_stream_module.ContextManager,
        "trim_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(non_stream_module.LLMClient, "generate", generate)
    monkeypatch.setattr(non_stream_module, "save_assistant_message", save_assistant)

    response = await _Dispatcher.dispatch(
        db=SimpleNamespace(),
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
        execution_resume_state=resume_state,
    )

    assert prepare_calls == []
    assert len(model_requests) == 2
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
        finish_reason=True,
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
            return _Channel(), {"model_id": "model-2"}, SimpleNamespace(priority=2)
        return _Channel(), {"model_id": "model-1"}, SimpleNamespace(priority=1)

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

    monkeypatch.setattr(non_stream_module, "logger", logger)
    monkeypatch.setattr(non_stream_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(non_stream_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(non_stream_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(non_stream_module, "select_channel", select_channel)
    monkeypatch.setattr(non_stream_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(non_stream_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        non_stream_module,
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
        non_stream_module.ContextManager,
        "trim_messages_for_model_request",
        lambda **kwargs: [message.model_copy(deep=True) for message in kwargs["messages"]],
    )
    monkeypatch.setattr(
        non_stream_module.LLMClient,
        "generate_with_stream_callback",
        generate_with_stream_callback,
    )
    monkeypatch.setattr(non_stream_module, "save_assistant_message", save_assistant)

    response = await _Dispatcher.dispatch(
        db=SimpleNamespace(),
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
