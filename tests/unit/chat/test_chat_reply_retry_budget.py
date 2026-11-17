import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.dispatchers import interactive_helpers as interactive_helpers_module
from app.core.dispatchers import interactive_runtime as interactive_runtime_module
from app.core.dispatchers import stream as stream_module
from app.core.exceptions import LLMException
from app.core.utils.dispatcher import markdown_instruction as markdown_instruction_module
from app.models.message import InternalMessage, InternalResponse, MessageRole
from app.models.profile import OtherConfig
from app.schemas.response import LLMResponse
from tests.unit.chat.reply_retry_context_support import (
    _build_max_tokens_runtime_instruction,
    _build_runtime_snapshot,
    _Channel,
    _Dispatcher,
    _Logger,
    _passthrough_context_summary_checkpoint,
    _Session,
    _StreamDispatcher,
)
from tests.unit.chat.reply_retry_context_support import _patch_session_crud as _patch_session_crud


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
        other=OtherConfig(),
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

    async def get_tools(db, current_profile, *, goal_mode=False, tool_workflow=None):
        return [], []

    async def prepare_messages(*args, **kwargs):
        return [
            InternalMessage(
                id=1,
                role=MessageRole.USER,
                content="original request",
                environment_prompt=_build_runtime_snapshot(kwargs["max_tokens"]),
            )
        ]

    async def generate(**kwargs):
        model_requests.append(kwargs)
        if kwargs["model_id"] == "model-1":
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="",
                ),
                model=kwargs["model_id"],
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 0,
                    "total_tokens": 100,
                    "cached_tokens": 100,
                },
            )
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="ok",
            ),
            model=kwargs["model_id"],
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
    monkeypatch.setattr(interactive_runtime_module, "logger", logger)
    monkeypatch.setattr(interactive_runtime_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_runtime_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_runtime_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_runtime_module, "mark_initial_message_processed", mark_processed)
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
            "max_tokens": model_entry["max_tokens"],
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
            "max_tokens": model_entry["max_tokens"],
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(interactive_runtime_module, "prepare_messages", prepare_messages)
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
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
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
        request_metadata_callback=request_metadata_callback,
    )

    assert [request["model_id"] for request in model_requests] == ["model-1", "model-2"]
    assert [event_type for event_type, _ in callback_order] == ["metadata", "metadata", "message"]
    assert len(request_metadatas) == 2
    assert request_metadatas[0]["total_input_tokens"] == 100
    assert request_metadatas[0]["total_cached_tokens"] == 100
    assert request_metadatas[1]["total_input_tokens"] == 200
    assert request_metadatas[1]["total_cached_tokens"] == 100
    assert [metadata["cache_hit_rate"] for metadata in request_metadatas] == [1.0, 0.5]
    first_user_payload = json.loads(model_requests[0]["messages"][0].content)
    second_user_payload = json.loads(model_requests[1]["messages"][0].content)
    assert first_user_payload["user_message"] == "original request"
    assert first_user_payload["response_settings"]["max_output_tokens"] == 1024
    assert second_user_payload["user_message"] == "original request"
    assert second_user_payload["response_settings"]["max_output_tokens"] == 256
    assert second_user_payload["response_settings"]["max_output_tokens"] != 1024
    assert persisted_environment_prompts[-1] == (1, _build_runtime_snapshot(256))
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
    assert response["llm_request_metadata"]["cache_hit_rate"] == 0.5


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
        other=OtherConfig(),
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

    async def get_tools(db, current_profile, *, goal_mode=False, tool_workflow=None):
        return [], []

    async def prepare_messages(*args, **kwargs):
        return [
            InternalMessage(
                id=1,
                role=MessageRole.USER,
                content="original request",
                environment_prompt=_build_runtime_snapshot(kwargs["max_tokens"]),
            )
        ]

    async def fetch_new_messages(*args, **kwargs):
        return []

    async def generate_with_stream_callback(**kwargs):
        model_requests.append(kwargs)
        if kwargs["model_id"] == "model-1":
            raise LLMException(message="ERR_LLM_UNEXPECTED_ERROR")
        return InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, content="ok"), model=kwargs["model_id"], usage={})

    async def save_assistant(db, session_id, uid, profile_id, ai_msg, dedupe_key=None, created_at=None):
        saved_created_at.append(created_at)
        return SimpleNamespace(content="ok")

    async def set_environment_prompt(_db, message_id, environment_prompt):
        persisted_environment_prompts.append((message_id, environment_prompt))
        return True

    monkeypatch.setattr(markdown_instruction_module, "build_user_runtime_instructions", _build_max_tokens_runtime_instruction)
    monkeypatch.setattr(markdown_instruction_module.message_crud, "set_environment_prompt", set_environment_prompt)
    monkeypatch.setattr(stream_module, "logger", logger)
    monkeypatch.setattr(interactive_runtime_module, "get_logger", lambda _name: logger)
    monkeypatch.setattr(interactive_runtime_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_runtime_module, "resolve_profile_for_session", get_profile)
    monkeypatch.setattr(interactive_runtime_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_runtime_module, "save_initial_message", save_initial)
    monkeypatch.setattr(interactive_runtime_module, "mark_initial_message_processed", mark_processed)
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
            "max_tokens": model_entry["max_tokens"],
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
            "max_tokens": model_entry["max_tokens"],
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(interactive_runtime_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(
        interactive_helpers_module,
        "fetch_and_merge_new_user_messages",
        fetch_new_messages,
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
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    monkeypatch.setattr(interactive_generation_module, "get_local_time", lambda: next(attempt_started_at))
    monkeypatch.setattr(interactive_runtime_module, "save_assistant_message", save_assistant)

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
    first_user_payload = json.loads(model_requests[0]["messages"][0].content)
    second_user_payload = json.loads(model_requests[1]["messages"][0].content)
    assert first_user_payload["user_message"] == "original request"
    assert first_user_payload["response_settings"]["max_output_tokens"] == 1024
    assert second_user_payload["user_message"] == "original request"
    assert second_user_payload["response_settings"]["max_output_tokens"] == 256
    assert second_user_payload["response_settings"]["max_output_tokens"] != 1024
    assert persisted_environment_prompts[-1] == (1, _build_runtime_snapshot(256))
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
