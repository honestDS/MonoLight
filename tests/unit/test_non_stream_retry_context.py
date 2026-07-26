import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.constants import SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY
from app.core.dispatchers import interactive as interactive_module
from app.core.dispatchers import interactive_helpers as interactive_helpers_module
from app.core.dispatchers import non_stream as non_stream_module
from app.core.dispatchers import stream as stream_module
from app.core.exceptions import LLMException
from app.core.utils.dispatcher import markdown_instruction as markdown_instruction_module
from app.core.utils.dispatcher.markdown_instruction import build_max_output_tokens_instruction
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse


class _Session:
    def __init__(self):
        self.commit_count = 0

    async def commit(self):
        self.commit_count += 1


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
async def test_non_stream_retry_refreshes_max_tokens_instruction_for_new_channel(monkeypatch):
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
            raise LLMException(message="ERR_LLM_UNEXPECTED_ERROR")
        return SimpleNamespace(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="ok",
            ),
            usage={
                "prompt_tokens": 777,
                "completion_tokens": 10,
                "total_tokens": 787,
                "cached_tokens": 222,
            },
        )

    async def save_assistant(db, session_id, uid, profile_id, ai_msg, dedupe_key=None, created_at=None):
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
    )

    assert [request["model_id"] for request in model_requests] == ["model-1", "model-2"]
    assert "最大输出 Token 数为 1024" in model_requests[0]["messages"][0].content
    assert "最大输出 Token 数为 256" in model_requests[1]["messages"][0].content
    assert "最大输出 Token 数为 1024" not in model_requests[1]["messages"][0].content
    assert persisted_environment_prompts[-1] == (1, build_max_output_tokens_instruction(256))
    assert model_requests[1]["max_tokens"] == 256
    assert saved_created_at == [None]
    assert LLMResponse.model_validate(response).choices[0].message.content == "ok"
    assert response["llm_request_metadata"]["input_tokens"] == 777
    assert response["llm_request_metadata"]["context_window_tokens"] == 4000
    assert response["llm_request_metadata"]["max_output_tokens"] == 256
    assert response["llm_request_metadata"]["output_tokens"] == 10
    assert response["llm_request_metadata"]["cached_tokens"] == 222
    assert response["llm_request_metadata"]["cache_hit_rate"] == pytest.approx(222 / 777)


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

    async def get_profile(db, uid):
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
    monkeypatch.setattr(interactive_module.profile_crud, "get_active", get_profile)
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
    assert "最大输出 Token 数为 1024" in model_requests[0]["messages"][0].content
    assert "最大输出 Token 数为 256" in model_requests[1]["messages"][0].content
    assert "最大输出 Token 数为 1024" not in model_requests[1]["messages"][0].content
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
    supersede_pending_confirmation_bundle_handler=None,
    response_usages=None,
    execution_resume_state=None,
):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        security=SimpleNamespace(
            audit_channel_id=None if audit_result is None else 1,
            audit_model_id=None if audit_result is None else "audit-model",
        ),
        tool=SimpleNamespace(
            max_turns=5,
            max_parallel_tools=5,
            executor_max_workers=1,
        ),
    )
    tool_call = InternalToolCall(
        id="call-1",
        name="execute_shell",
        arguments={"command": "echo 1"},
    )
    responses = [
        InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
        InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
    ]
    saved_message_id = 10
    response_usage_iterator = iter(response_usages or [])

    async def get_user(db, uid):
        return SimpleNamespace(username="operator")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        return _Channel(), {"model_id": "model-1", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile):
        return [SimpleNamespace(name="execute_shell")], []

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
        return SimpleNamespace(message=responses.pop(0), usage=next(response_usage_iterator, None))

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
        if audit_waiter is not None:
            await audit_waiter()
        if audit_result is not _DEFAULT_AUDIT_RESULT:
            return audit_result
        return SimpleNamespace(
            may_execute=True,
            audit_record_id=42,
            tool_results=[],
            confirmation_payload=None,
        )

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
        if audit_result is None:
            raise AssertionError("skipped audit must not create a claim")
        if not claim_execution_success:
            return None, None
        return SimpleNamespace(execution_claim_token="claim-token"), "claim-token"

    async def list_details(db, audit_record_id):
        return [SimpleNamespace(original_tool_call_id="call-1", id=7)]

    async def create_execution(db, **kwargs):
        if audit_result is None:
            raise AssertionError("skipped audit must not create execution records")
        return SimpleNamespace(id=8)

    async def finish_attempt(db, **kwargs):
        return True

    async def finish_round(db, **kwargs):
        return finish_round_result

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

    async def prepare_messages(*args, **kwargs):
        return [InternalMessage(role=MessageRole.USER, content="request")]

    async def materialize_environment_prompt(db, session_id, messages, max_tokens):
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
    monkeypatch.setattr(interactive_module.audit_crud, "mark_execution_unknown", mark_unknown)
    monkeypatch.setattr(interactive_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(interactive_helpers_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(interactive_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(interactive_helpers_module, "process_single_tool_with_isolated_db", process_tool)

    if stream_dispatch:
        response = [
            event
            async for event in _StreamDispatcher.dispatch_stream(
                db=_Session(),
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
            db=_Session(),
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
async def test_interactive_accumulates_output_tokens_and_preserves_latest_cache_metrics(monkeypatch):
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
    assert all("total_input_tokens" not in event for event in metadata_events)
    assert metadata_events[1]["output_tokens"] == 12
    assert metadata_events[1]["total_output_tokens"] == 12
    assert metadata_events[2]["output_tokens"] == 12
    assert metadata_events[2]["total_output_tokens"] == 12
    assert metadata_events[2]["cached_tokens"] == 200
    assert metadata_events[2]["cache_hit_rate"] == pytest.approx(0.2)
    assert metadata_events[3]["output_tokens"] == 32
    assert metadata_events[3]["total_output_tokens"] == 32
    assert metadata_events[3]["cached_tokens"] == 440
    assert metadata_events[3]["cache_hit_rate"] == pytest.approx(0.4)
    assert any(checkpoint["total_output_tokens"] == 12 for checkpoint in checkpoints)
    assert any(checkpoint["session_total_output_tokens"] == 12 for checkpoint in checkpoints)
    assert response["llm_request_metadata"]["output_tokens"] == 32
    assert response["llm_request_metadata"]["total_output_tokens"] == 32
    assert "total_input_tokens" not in response["llm_request_metadata"]
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
        },
    )

    provider_metadata_events = [event for event in events if event["type"] == "llm_request_metadata" and event["input_tokens_source"] == "provider"]
    assert [event["output_tokens"] for event in provider_metadata_events] == [57, 60]
    assert [event["total_output_tokens"] for event in provider_metadata_events] == [19, 22]
    assert response["llm_request_metadata"]["output_tokens"] == 60
    assert response["llm_request_metadata"]["total_output_tokens"] == 22
    assert all("total_input_tokens" not in event for event in events if event["type"] == "llm_request_metadata")
    assert "total_input_tokens" not in response["llm_request_metadata"]
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
    assert json.loads(events[6]["result"])["status"] == "pending"
    assert json.loads(events[-1]["response"]["choices"][0]["message"]["content"]) == confirmation_payload
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_pending_audit_bundle_is_superseded_when_message_arrives_after_persistence(monkeypatch):
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
    fetch_results = iter(
        [
            [],
            [],
            [InternalMessage(id=2, role=MessageRole.USER, content="continue without confirmation")],
            [],
            [],
        ]
    )
    fetch_count = 0
    lifecycle = []
    checkpoints = []
    generated_calls = []
    tool_calls = []

    async def fetch_additional_messages():
        nonlocal fetch_count
        fetch_count += 1
        return next(fetch_results)

    async def persist_confirmation_bundle(db, *, tool_results, confirmation_payload, **kwargs):
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

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=audit_result,
        generated_calls_target=generated_calls,
        additional_user_messages_fetcher=fetch_additional_messages,
        persist_pending_confirmation_bundle_handler=persist_confirmation_bundle,
        supersede_pending_confirmation_bundle_handler=supersede_confirmation_bundle,
    )

    assert fetch_count == 5
    assert lifecycle == ["persisted", "superseded"]
    assert len(generated_calls) == 2
    assert any(message.role == MessageRole.USER and "continue without confirmation" in str(message.content) for message in generated_calls[1]["messages"])
    cancelled_result = next(message for message in generated_calls[1]["messages"] if message.role == MessageRole.TOOL)
    cancelled_payload = json.loads(cancelled_result.content)
    assert cancelled_payload["status"] == "cancelled"
    assert cancelled_payload["confirmation_status"] == "superseded"
    assert checkpoints[-1]["current_turn"] == 0
    assert checkpoints[-1]["context_summary_fixed_upper_message_id"] == 2
    assert response["choices"][0]["message"]["content"] == "finished"
    assert tool_calls == []
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
    assert json.loads(events[6]["result"])["status"] == "failed"
    assert events[6]["tool_call_id"] == events[5]["tool_call_id"] == "call-1"
    assert unknown_calls == []
