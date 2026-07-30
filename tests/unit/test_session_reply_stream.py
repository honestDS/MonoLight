import asyncio
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.adapters import chat_web as chat_web_module
from app.core.constants import ERR_LLM_STREAM_TIMEOUT, ERR_LLM_STREAM_TOOL_CALL_AMBIGUOUS
from app.core.dispatchers.stream import StreamDispatcherMixin
from app.core.exceptions import BaseBusinessException, LLMException
from app.core.session_reply_queue import executor as executor_module
from app.core.session_reply_queue.executor import _execute_foreground
from app.core.session_reply_queue.manager import (
    SessionReplyQueueManager,
    build_foreground_message_dedupe_key,
)
from app.core.utils.dispatcher.helpers import dump_output_history
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.session_reply_work_item import SessionReplyWorkStatus
from app.providers.llm import client as llm_client_module
from app.providers.llm.client import LLMClient


def test_llm_request_context_debug_log_contains_counts_and_estimated_tokens(monkeypatch):
    bound_fields = {}
    logged_messages = []
    estimate_calls = []

    class CapturingLogger:
        def bind(self, **kwargs):
            bound_fields.update(kwargs)
            return self

        def debug(self, message, **kwargs):
            logged_messages.append((message, kwargs))

    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(role=MessageRole.USER, content="question"),
        InternalMessage(role=MessageRole.ASSISTANT, content="answer"),
    ]
    tools = [{"type": "function", "function": {"name": "search"}}]
    monkeypatch.setattr(llm_client_module, "logger", CapturingLogger())

    def estimate_request_context_tokens(request_messages, request_tools):
        estimate_calls.append((request_messages, request_tools))
        return 321

    monkeypatch.setattr(
        llm_client_module,
        "estimate_request_context_tokens",
        estimate_request_context_tokens,
    )

    llm_client_module._log_request_context(
        model_id="model-1",
        protocol="openai",
        messages=messages,
        tools=tools,
        max_tokens=512,
        streaming=False,
    )

    assert logged_messages == [
        (
            "LLM request context: model={model_id}, protocol={protocol}, streaming={streaming}, messages={message_count}, roles={role_counts}, tools={tool_count}, estimated_tokens={estimated_context_tokens}, max_output_tokens={max_tokens}",
            {
                "model_id": "model-1",
                "protocol": "openai",
                "streaming": False,
                "message_count": 3,
                "role_counts": {"system": 1, "user": 1, "assistant": 1},
                "tool_count": 1,
                "estimated_context_tokens": 321,
                "max_tokens": 512,
            },
        )
    ]
    assert bound_fields == {
        "model_id": "model-1",
        "protocol": "openai",
        "streaming": False,
        "message_count": 3,
        "role_counts": {"system": 1, "user": 1, "assistant": 1},
        "tool_count": 1,
        "estimated_context_tokens": 321,
        "max_output_tokens": 512,
    }
    assert estimate_calls == [(messages, tools)]


def test_llm_request_context_debug_log_reuses_provided_estimated_tokens(monkeypatch):
    bound_fields = {}
    logged_fields = {}

    class CapturingLogger:
        def bind(self, **kwargs):
            bound_fields.update(kwargs)
            return self

        def debug(self, _message, **kwargs):
            logged_fields.update(kwargs)

    def unexpected_estimate(*_args, **_kwargs):
        raise AssertionError("provided request context tokens must be reused")

    monkeypatch.setattr(llm_client_module, "logger", CapturingLogger())
    monkeypatch.setattr(
        llm_client_module,
        "estimate_request_context_tokens",
        unexpected_estimate,
    )

    llm_client_module._log_request_context(
        model_id="model-1",
        protocol="openai",
        messages=[InternalMessage(role=MessageRole.USER, content="question")],
        tools=None,
        max_tokens=512,
        streaming=False,
        request_context_tokens=123,
    )

    assert bound_fields["estimated_context_tokens"] == 123
    assert logged_fields["estimated_context_tokens"] == 123


@pytest.mark.asyncio
async def test_dispatch_stream_raise_errors_controls_business_exception_surface():
    class FailingStreamDispatcher(StreamDispatcherMixin):
        @classmethod
        async def _dispatch_interactive(cls, **_kwargs):
            raise LLMException(message=ERR_LLM_STREAM_TIMEOUT, timeout=120.0)

    expected_error = LLMException(message=ERR_LLM_STREAM_TIMEOUT, timeout=120.0)

    events = [
        event
        async for event in FailingStreamDispatcher.dispatch_stream(
            db=None,
            message="测试",
            uid="user-1",
            session_id="session-1",
        )
    ]

    assert [event["type"] for event in events] == ["task_start", "error"]
    assert events[1]["message"] == expected_error.render_message()

    raised_events = []
    with pytest.raises(LLMException) as exc_info:
        async for event in FailingStreamDispatcher.dispatch_stream(
            db=None,
            message="测试",
            uid="user-1",
            session_id="session-1",
            raise_errors=True,
        ):
            raised_events.append(event)

    assert [event["type"] for event in raised_events] == ["task_start"]
    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert exc_info.value.kwargs["timeout"] == 120.0


@pytest.mark.asyncio
async def test_generate_with_stream_callback_emits_content_and_rebuilds_tool_calls(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "vendor_tag": "initial",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query":',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "model": "model-final",
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
            },
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": '"MonoLight"}',
                                },
                            }
                        ]
                    }
                }
            ],
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
    )

    assert emitted == ["你", "好"]
    assert response.message.content == "你好"
    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].id.startswith("call_")
    assert response.message.tool_calls[0].id != "call-1"
    assert len(response.message.tool_calls[0].id) == 37
    int(response.message.tool_calls[0].id.removeprefix("call_"), 16)
    assert response.message.tool_calls[0].name == "search"
    assert response.message.tool_calls[0].arguments == {"query": "MonoLight"}
    assert response.message.tool_calls[0].provider_metadata == {
        "protocol": "openai_chat_completions",
        "tool_call": {
            "type": "function",
            "vendor_tag": "initial",
        },
    }
    assert response.model == "model-final"
    assert response.usage["total_tokens"] == 3


@pytest.mark.asyncio
async def test_chat_stream_concatenates_reasoning_content_metadata(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "First reasoning. "}}]},
        {"choices": [{"delta": {"reasoning_content": "Second reasoning."}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(_content: str) -> None:
        return None

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    assert response.message.provider_metadata["message"]["reasoning_content"] == "First reasoning. Second reasoning."
    assert response.provider_metadata["message"]["reasoning_content"] == "First reasoning. Second reasoning."


@pytest.mark.asyncio
async def test_chat_stream_refusal_is_visible_and_normalizes_stop(monkeypatch):
    chunks = [
        {
            "id": "chatcmpl_refusal",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "refusal": "Request ",
                    },
                }
            ],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"refusal": "refused."},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ]
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    assert emitted == ["Request ", "refused."]
    assert response.message.content == "Request refused."
    assert response.message.refusal == "Request refused."
    assert response.finish_reason == "refusal"
    assert response.finish_details == {"raw_finish_reason": "stop"}
    assert response.provider_metadata == {
        "protocol": "openai_chat_completions",
        "response": {
            "id": "chatcmpl_refusal",
            "object": "chat.completion.chunk",
        },
        "choice": {"index": 0},
        "message": {"role": "assistant"},
    }
    assert response.message.provider_metadata == {
        "protocol": "openai_chat_completions",
        "choice": {"index": 0},
        "message": {"role": "assistant"},
    }


@pytest.mark.asyncio
async def test_chat_stream_length_returns_partial_content_and_raw_reason(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Partial answer"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "length",
                }
            ]
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    assert emitted == ["Partial answer"]
    assert response.message.content == "Partial answer"
    assert response.message.refusal is None
    assert response.finish_reason == "length"
    assert response.finish_details == {"raw_finish_reason": "length"}


async def _collect_stream_tool_calls(monkeypatch, chunks):
    async def generate_stream(cls, **kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(_content):
        return None

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))
    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
    )
    return response.message.tool_calls


@pytest.mark.asyncio
async def test_stream_tool_call_replay_on_another_index_is_assembled_once(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-X-0",
                                "function": {"name": "execute_shell", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-X-0",
                                "function": {"arguments": '{"command":"python test.py"}'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call-X-0",
                                "function": {
                                    "name": "execute_shell",
                                    "arguments": '{"command":"python test.py"}',
                                },
                            }
                        ]
                    }
                }
            ]
        },
    ]
    original_chunks = deepcopy(chunks)

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "execute_shell"
    assert tool_calls[0].arguments == {"command": "python test.py"}
    assert chunks == original_chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("include_provider_ids", [True, False])
async def test_stream_tool_calls_preserve_identical_batch_calls(monkeypatch, include_provider_ids):
    first = {
        "index": 0,
        "function": {"name": "search", "arguments": '{"query":"MonoLight"}'},
    }
    second = {
        "index": 1,
        "function": {"name": "search", "arguments": '{"query":"MonoLight"}'},
    }
    if include_provider_ids:
        first["id"] = "provider-call-1"
        second["id"] = "provider-call-2"
    chunks = [{"choices": [{"delta": {"tool_calls": [first, second]}}]}]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert [tool_call.name for tool_call in tool_calls] == ["search", "search"]
    assert [tool_call.arguments for tool_call in tool_calls] == [
        {"query": "MonoLight"},
        {"query": "MonoLight"},
    ]


@pytest.mark.asyncio
async def test_stream_tool_call_binds_late_id_and_keeps_argument_fragments(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "search", "arguments": '{"query":"'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "provider-call",
                                "function": {"arguments": "Mono"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "provider-call",
                                "function": {"arguments": 'Light"}'},
                            }
                        ]
                    }
                }
            ]
        },
    ]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "search"
    assert tool_calls[0].arguments == {"query": "MonoLight"}


@pytest.mark.asyncio
async def test_stream_tool_call_keeps_identical_fragments_on_same_index(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "provider-call",
                                "function": {"name": "search", "arguments": '{"query":"'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "same"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "same"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}}]},
    ]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0].arguments == {"query": "samesame"}


@pytest.mark.asyncio
async def test_stream_tool_calls_with_same_id_but_different_content_stay_separate(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "reused-provider-id",
                                "function": {"name": "search", "arguments": '{"query":"one"}'},
                            },
                            {
                                "index": 1,
                                "id": "reused-provider-id",
                                "function": {"name": "search", "arguments": '{"query":"two"}'},
                            },
                        ]
                    }
                }
            ]
        }
    ]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert [(tool_call.name, tool_call.arguments) for tool_call in tool_calls] == [
        ("search", {"query": "one"}),
        ("search", {"query": "two"}),
    ]


@pytest.mark.asyncio
async def test_stream_tool_calls_without_identity_in_same_delta_raise_llm_exception(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"function": {"name": "search", "arguments": "{}"}},
                            {"function": {"name": "search", "arguments": "{}"}},
                        ]
                    }
                }
            ]
        }
    ]

    with pytest.raises(LLMException) as exc_info:
        await _collect_stream_tool_calls(monkeypatch, chunks)

    assert exc_info.value.message == ERR_LLM_STREAM_TOOL_CALL_AMBIGUOUS


def test_normalize_tool_calls_replaces_upstream_ids_and_preserves_identical_calls():
    upstream_calls = [
        InternalToolCall(id="upstream-1", name="search", arguments={"query": "MonoLight"}),
        InternalToolCall(id="upstream-2", name="search", arguments={"query": "MonoLight"}),
        InternalToolCall(id="upstream-1", name="search", arguments={"query": "Kilo"}),
    ]

    normalized_calls = LLMClient.normalize_tool_calls(upstream_calls)

    assert normalized_calls is not None
    assert len(normalized_calls) == 3
    assert [tool_call.arguments for tool_call in normalized_calls] == [{"query": "MonoLight"}, {"query": "MonoLight"}, {"query": "Kilo"}]
    assert len({tool_call.id for tool_call in normalized_calls}) == 3
    assert all(tool_call.id.startswith("call_") and len(tool_call.id) == 37 for tool_call in normalized_calls)
    assert [tool_call.id for tool_call in upstream_calls] == ["upstream-1", "upstream-2", "upstream-1"]


@pytest.mark.asyncio
async def test_generate_replaces_provider_tool_call_ids(monkeypatch):
    class Transformer:
        async def generate(self, **_kwargs):
            return {"model": "model-final"}

        def to_internal_response(self, _raw_response, default_model):
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="provider-call",
                            name="search",
                            arguments={"query": "MonoLight"},
                            provider_metadata={
                                "protocol": "test",
                                "tool_call": {
                                    "type": "function",
                                    "vendor_tag": "preserved",
                                },
                            },
                        )
                    ],
                ),
                model=_raw_response.get("model", default_model),
                finish_reason="tool_calls",
                provider_metadata={
                    "protocol": "test",
                    "response": {"trace_id": "trace-1"},
                },
            )

    monkeypatch.setitem(LLMClient._transformers, "test", Transformer())

    response = await LLMClient.generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        protocol="test",
    )

    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].id.startswith("call_")
    assert response.message.tool_calls[0].id != "provider-call"
    assert response.message.tool_calls[0].provider_metadata == {
        "protocol": "test",
        "tool_call": {
            "type": "function",
            "vendor_tag": "preserved",
        },
    }
    assert response.finish_reason == "tool_calls"
    assert response.model == "model-final"
    assert response.provider_metadata == {
        "protocol": "test",
        "response": {"trace_id": "trace-1"},
    }


def test_foreground_message_dedupe_key_scopes_reused_message_id_to_session():
    first = build_foreground_message_dedupe_key("session-1", 1)
    second = build_foreground_message_dedupe_key("session-2", 1)

    assert first != second
    assert first == build_foreground_message_dedupe_key("session-1", 1)
    assert first.startswith("foreground-message:")
    assert len(first) <= 160


def test_dump_output_history_can_hide_tool_messages_without_mutating_messages():
    tool_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="准备查询资料",
        tool_calls=[
            InternalToolCall(
                id="call-1",
                name="search",
                arguments={"query": "MonoLight"},
            )
        ],
    )

    visible_history = dump_output_history([tool_message])
    hidden_history = dump_output_history(
        [tool_message],
        show_tool_calls=False,
    )

    assert visible_history[0]["content"] == "准备查询资料"
    assert hidden_history == []
    assert tool_message.content == "准备查询资料"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "stream_requested", "context_summary_events_requested", "show_tool_calls"),
    [
        ("http", False, False, True),
        ("ws", True, True, True),
        ("future-platform", False, False, False),
        ("future-platform", False, False, True),
    ],
)
async def test_enqueue_foreground_controls_tool_call_content_by_source(
    monkeypatch,
    source,
    stream_requested,
    context_summary_events_requested,
    show_tool_calls,
):
    manager = SessionReplyQueueManager()
    work = SimpleNamespace(execution_state={}, id=7)
    enqueue_calls = []

    class FakeDb:
        def __init__(self):
            self.pending = None

        def add(self, instance):
            self.pending = instance

        async def flush(self):
            if getattr(self.pending, "id", None) is None:
                self.pending.id = 11

        async def commit(self):
            return None

        async def refresh(self, instance):
            if isinstance(instance, InternalMessage):
                return
            if getattr(instance, "created_at", None) is None:
                instance.created_at = SimpleNamespace(timestamp=lambda: 1.0)
            if getattr(instance, "id", None) is None:
                instance.id = 11

    async def upsert_profile(*args, **kwargs):
        return SimpleNamespace(show_tool_calls=show_tool_calls)

    async def enqueue(*args, **kwargs):
        enqueue_calls.append(kwargs)
        return work, True

    async def activate_and_get_guidance_prompt(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_crud.upsert_profile",
        upsert_profile,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_work_item_crud.enqueue",
        enqueue,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.message_crud.activate_and_get_guidance_prompt",
        activate_and_get_guidance_prompt,
    )

    _message, queued_work = await manager.enqueue_foreground_message(
        FakeDb(),
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=3),
        message="测试",
        attachments=None,
        source=source,
        request_id="request-1",
    )

    assert queued_work.execution_state["stream_requested"] is stream_requested
    assert queued_work.execution_state["context_summary_events_requested"] is context_summary_events_requested
    assert queued_work.execution_state["show_tool_calls"] is show_tool_calls
    assert queued_work.execution_state["expose_tool_call_content"] is show_tool_calls
    assert queued_work.execution_state["request_ids"] == ["request-1"]
    assert enqueue_calls[0]["dedupe_key"] == build_foreground_message_dedupe_key("session-1", 11)


@pytest.mark.asyncio
async def test_http_foreground_can_request_summary_events_without_content_stream(monkeypatch):
    manager = SessionReplyQueueManager()
    work = SimpleNamespace(execution_state={}, id=7)

    class FakeDb:
        def __init__(self):
            self.pending = None

        def add(self, instance):
            self.pending = instance

        async def flush(self):
            if getattr(self.pending, "id", None) is None:
                self.pending.id = 11

        async def commit(self):
            return None

        async def refresh(self, instance):
            if getattr(instance, "created_at", None) is None:
                instance.created_at = SimpleNamespace(timestamp=lambda: 1.0)
            if getattr(instance, "id", None) is None:
                instance.id = 11

    async def upsert_profile(*args, **kwargs):
        return None

    async def enqueue(*args, **kwargs):
        return work, True

    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_crud.upsert_profile",
        upsert_profile,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_work_item_crud.enqueue",
        enqueue,
    )

    _message, queued_work = await manager.enqueue_foreground_message(
        FakeDb(),
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=3),
        message="测试",
        attachments=None,
        source="http",
        stream_requested=False,
        context_summary_events_requested=True,
    )

    assert queued_work.execution_state["stream_requested"] is False
    assert queued_work.execution_state["context_summary_events_requested"] is True
    assert queued_work.execution_state["request_ids"] == []


@pytest.mark.asyncio
async def test_http_stream_adapter_enqueues_stream_dispatch(monkeypatch):
    adapter = chat_web_module.WebChatAdapter()
    captured_kwargs = {}

    async def ensure_writable(*_args, **_kwargs):
        return None

    async def get_profile(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    async def validate_message(*_args, **_kwargs):
        return None

    async def enqueue_message(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return (
            SimpleNamespace(id=1),
            SimpleNamespace(id=9),
            "queued",
            [
                {
                    "type": "audit_confirmation_status",
                    "event_id": "audit-confirmation:1:rejected",
                    "session_id": "session-1",
                    "audit_record_id": 1,
                    "status": "rejected",
                },
                {
                    "type": "audit_tool_results_update",
                    "event_id": "audit-tool-results:1:rejected",
                    "session_id": "session-1",
                    "audit_record_id": 1,
                    "messages": [{"tool_call_id": "call-1", "content": '{"status":"rejected"}'}],
                },
            ],
        )

    async def wait_for_stream(_work_id):
        yield {"type": "done", "response": {"history": [], "files": None}}

    monkeypatch.setattr(chat_web_module, "ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr(chat_web_module, "resolve_profile_for_session", get_profile)
    monkeypatch.setattr(chat_web_module.ChatDispatcher, "validate_initial_message_before_save", validate_message)
    monkeypatch.setattr(chat_web_module.session_reply_queue_manager, "submit_user_message", enqueue_message)
    monkeypatch.setattr(chat_web_module.session_reply_queue_manager, "wait_for_stream", wait_for_stream)

    events = [
        event
        async for event in adapter.chat_stream(
            db=SimpleNamespace(),
            message="test",
            uid="user-1",
            session_id="session-1",
            request_id="request-1",
        )
    ]

    assert captured_kwargs["stream_requested"] is True
    assert captured_kwargs["context_summary_events_requested"] is True
    assert captured_kwargs["request_id"] == "request-1"
    assert [event["type"] for event in events] == [
        "audit_confirmation_status",
        "audit_tool_results_update",
        "input_queued",
        "done",
    ]
    assert events[0]["event_id"] == "audit-confirmation:1:rejected"
    assert events[0]["status"] == "rejected"
    assert events[1]["event_id"] == "audit-tool-results:1:rejected"
    assert events[1]["messages"][0]["content"] == '{"status":"rejected"}'
    assert events[2] == {
        "type": "input_queued",
        "session_id": "session-1",
        "request_id": "request-1",
        "work_id": 9,
        "submission_status": "queued",
    }
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_http_adapter_merges_confirmation_updates_into_non_stream_response(monkeypatch):
    adapter = chat_web_module.WebChatAdapter()
    work = SimpleNamespace(id=9, execution_state={})
    confirmation_update_events = [
        {
            "type": "audit_confirmation_status",
            "event_id": "audit-confirmation:1:executing",
            "session_id": "session-1",
            "audit_record_id": 1,
            "status": "executing",
        },
        {
            "type": "audit_tool_results_update",
            "event_id": "audit-tool-results:1:executing",
            "session_id": "session-1",
            "audit_record_id": 1,
            "messages": [{"tool_call_id": "call-1", "content": '{"status":"executing"}'}],
        },
    ]
    final_db = object()
    final_event_calls = []
    session_factory_calls = []
    request_bind = object()
    request_db = SimpleNamespace(bind=request_bind)
    final_confirmation_update_events = [
        {
            "type": "audit_confirmation_status",
            "event_id": "audit-confirmation:1:succeeded",
            "session_id": "session-1",
            "audit_record_id": 1,
            "status": "succeeded",
        },
        {
            "type": "audit_tool_results_update",
            "event_id": "audit-tool-results:1:succeeded",
            "session_id": "session-1",
            "audit_record_id": 1,
            "messages": [{"tool_call_id": "call-1", "content": '{"status":"succeeded"}'}],
        },
    ]

    async def ensure_writable(*_args, **_kwargs):
        return None

    async def get_profile(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    async def validate_message(*_args, **_kwargs):
        return None

    async def enqueue_message(*_args, **_kwargs):
        return SimpleNamespace(id=1), work, "approved", confirmation_update_events

    class SessionContext:
        async def __aenter__(self):
            return final_db

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    def create_final_session_factory(*, bind, class_, expire_on_commit):
        session_factory_calls.append((bind, class_, expire_on_commit))
        return SessionContext

    async def build_final_confirmation_update_events(db, *, audit_record_id, include_tool_results):
        final_event_calls.append((db, audit_record_id, include_tool_results))
        return final_confirmation_update_events

    async def wait_for_result(_work_id):
        return {
            "choices": [],
            "session_events": [
                {"type": "existing", "event_id": "existing-event"},
                {"type": "stale", "event_id": "audit-confirmation:1:succeeded"},
            ],
        }

    monkeypatch.setattr(chat_web_module, "ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr(chat_web_module, "resolve_profile_for_session", get_profile)
    monkeypatch.setattr(chat_web_module.ChatDispatcher, "validate_initial_message_before_save", validate_message)
    monkeypatch.setattr(chat_web_module.session_reply_queue_manager, "submit_user_message", enqueue_message)
    monkeypatch.setattr(chat_web_module.session_reply_queue_manager, "wait_for_result", wait_for_result)
    monkeypatch.setattr(chat_web_module, "async_sessionmaker", create_final_session_factory)
    monkeypatch.setattr(chat_web_module, "build_confirmation_update_events", build_final_confirmation_update_events)

    response = await adapter.chat(
        db=request_db,
        message="reject",
        uid="user-1",
        session_id="session-1",
    )

    assert session_factory_calls == [(request_bind, type(request_db), False)]
    assert final_event_calls == [(final_db, 1, True)]
    assert [event["event_id"] for event in response["session_events"]] == [
        "existing-event",
        "audit-confirmation:1:succeeded",
        "audit-tool-results:1:succeeded",
    ]
    assert response["session_events"][1]["status"] == "succeeded"
    assert response["session_events"][2]["messages"][0]["content"] == '{"status":"succeeded"}'
    assert all("executing" not in event["event_id"] for event in response["session_events"])


@pytest.mark.asyncio
async def test_http_adapter_uses_resolved_work_identity_from_failure(monkeypatch):
    adapter = chat_web_module.WebChatAdapter()
    original_work = SimpleNamespace(id=9)
    resolved_event_id = "session-reply-work:resolved:error"

    async def ensure_writable(*_args, **_kwargs):
        return None

    async def get_profile(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    async def validate_message(*_args, **_kwargs):
        return None

    async def enqueue_message(*_args, **_kwargs):
        return SimpleNamespace(id=1), original_work, "queued", []

    async def wait_for_result(work_id):
        assert work_id == 9
        raise BaseBusinessException(
            message="等待对话模型首字响应超时（60.0 秒）",
            default_message="等待对话模型首字响应超时（60.0 秒）",
            data={"work_id": 7, "event_id": resolved_event_id},
        )

    monkeypatch.setattr(chat_web_module, "ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr(chat_web_module, "resolve_profile_for_session", get_profile)
    monkeypatch.setattr(chat_web_module.ChatDispatcher, "validate_initial_message_before_save", validate_message)
    monkeypatch.setattr(chat_web_module.session_reply_queue_manager, "submit_user_message", enqueue_message)
    monkeypatch.setattr(chat_web_module.session_reply_queue_manager, "wait_for_result", wait_for_result)

    response = await adapter.chat(
        db=SimpleNamespace(),
        message="test",
        uid="user-1",
        session_id="session-1",
    )

    assert response["work_id"] == 7
    assert response["event_id"] == resolved_event_id
    assert response["choices"][0]["message"]["content"] == "等待对话模型首字响应超时（60.0 秒）"


@pytest.mark.asyncio
async def test_wait_for_stream_yields_persisted_chunks_before_work_finishes(monkeypatch):
    manager = SessionReplyQueueManager()
    states = [
        SimpleNamespace(
            id=7,
            session_id="session-1",
            status=SessionReplyWorkStatus.RUNNING,
            execution_state={},
            error=None,
        ),
        SimpleNamespace(
            id=7,
            session_id="session-1",
            status=SessionReplyWorkStatus.SUCCEEDED,
            execution_state={
                "response": {
                    "history": [{"role": "assistant", "content": "你好"}],
                    "files": None,
                },
                "request_ids": ["request-1", "request-2"],
            },
            error=None,
        ),
    ]
    stream_events = [
        [
            SimpleNamespace(
                sequence_no=1,
                event={
                    "type": "content",
                    "content": "你",
                    "session_id": "session-1",
                    "work_id": 7,
                },
            )
        ],
        [
            SimpleNamespace(
                sequence_no=2,
                event={
                    "type": "content",
                    "content": "好",
                    "session_id": "session-1",
                    "work_id": 7,
                },
            )
        ],
    ]

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id: int):
        return states.pop(0)

    async def list_after_sequence(db, *, work_id: int, after_sequence_no: int):
        return stream_events.pop(0)

    async def no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_work_item_crud.resolve_merged_target",
        resolve_merged_target,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_stream_event_crud.list_after_sequence",
        list_after_sequence,
    )
    monkeypatch.setattr("app.core.session_reply_queue.manager.asyncio.sleep", no_sleep)

    yielded = [event async for event in manager.wait_for_stream(7)]

    assert [event["type"] for event in yielded] == ["content", "content", "done"]
    assert "".join(event["content"] for event in yielded if event["type"] == "content") == "你好"
    assert yielded[-1]["history"] == [{"role": "assistant", "content": "你好"}]
    assert yielded[-1]["request_ids"] == ["request-1", "request-2"]


@pytest.mark.asyncio
async def test_wait_for_stream_returns_identified_error_event(monkeypatch):
    manager = SessionReplyQueueManager()
    work = SimpleNamespace(
        id=7,
        session_id="session-1",
        status=SessionReplyWorkStatus.FAILED,
        execution_state={"request_ids": ["request-1", "request-2"]},
        error="timeout",
        result_message_id=9,
        dedupe_key="foreground-message:session-1:11",
        created_at=datetime(2026, 7, 16, 21, 0, 0),
    )

    class FakeSession:
        async def get(self, model, item_id):
            assert item_id == 9
            return SimpleNamespace(content="等待对话模型首字响应超时（60.0 秒）")

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id: int):
        return work

    async def list_after_sequence(db, *, work_id: int, after_sequence_no: int):
        return []

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_work_item_crud.resolve_merged_target",
        resolve_merged_target,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_stream_event_crud.list_after_sequence",
        list_after_sequence,
    )

    yielded = [event async for event in manager.wait_for_stream(7)]

    assert len(yielded) == 1
    assert yielded[0]["type"] == "error"
    assert yielded[0]["work_id"] == 7
    assert yielded[0]["message"] == "等待对话模型首字响应超时（60.0 秒）"
    assert yielded[0]["event_id"].startswith("session-reply-work:")
    assert yielded[0]["event_id"].endswith(":error")
    assert yielded[0]["request_ids"] == ["request-1", "request-2"]


@pytest.mark.asyncio
async def test_execute_foreground_persists_each_tool_event_with_original_response_id(monkeypatch):
    work = SimpleNamespace(
        id=7,
        sequence_no=1,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="foreground-message:session-1:11",
        created_at=datetime(2026, 7, 13, 21, 0, 0),
        execution_state={
            "stream_requested": True,
            "expose_tool_call_content": False,
            "request_ids": ["request-1"],
        },
    )
    published: list[tuple[int, dict]] = []
    dispatch_kwargs: dict = {}
    metadata_updates = []
    event_db_commits = []
    stream_event_calls = []

    class FakeDb:
        def __init__(self):
            self.flush_calls = 0

        async def refresh(self, instance):
            return None

        async def flush(self):
            self.flush_calls += 1

    class EventDb:
        async def commit(self):
            event_db_commits.append(self)

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def freeze_foreground_input(db, *, work, worker_id):
        return "运行工具", [], [11]

    async def get_latest_sequence(db, *, work_id):
        return 0

    async def publish(db, *, work_id, sequence_no, event, commit=True):
        assert commit is False
        published.append((sequence_no, event))

    async def update_llm_request_metadata(db, *, session_id, uid, metadata, commit=True):
        metadata_updates.append(
            {
                "db_type": type(db).__name__,
                "session_id": session_id,
                "uid": uid,
                "metadata": metadata,
                "commit": commit,
            }
        )
        return True

    async def send_session_stream_event(uid, session_id, event):
        stream_event_calls.append(
            {
                "uid": uid,
                "session_id": session_id,
                "event": event,
                "persisted_event_count": len(published),
            }
        )

    async def dispatch_stream(**kwargs):
        dispatch_kwargs.update(kwargs)
        yield {
            "type": "llm_request_metadata",
            "turn": 1,
            "response_id": "response-turn-1",
            "input_tokens": 100,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
        }
        yield {
            "type": "agent_loop_start",
            "session_id": "session-1",
        }
        yield {
            "type": "turn_end",
            "content": "我先检查",
            "turn": 1,
            "response_id": "response-turn-1",
        }
        yield {
            "type": "tool_start",
            "name": "execute_shell",
            "arguments": {"command": "echo 1"},
            "tool_call_id": "call-1",
            "response_id": "response-turn-1",
            "tool_call_index": 0,
            "tool_call_count": 2,
        }
        yield {
            "type": "tool_start",
            "name": "read_text_file",
            "arguments": {"file_path": "note.txt"},
            "tool_call_id": "call-2",
            "response_id": "response-turn-1",
            "tool_call_index": 1,
            "tool_call_count": 2,
        }
        work.execution_state["request_ids"].append("request-2")
        yield {
            "type": "tool_end",
            "name": "execute_shell",
            "result": '{"status":"success"}',
            "tool_call_id": "call-1",
            "response_id": "response-turn-1",
        }
        yield {
            "type": "llm_request_metadata",
            "turn": 2,
            "response_id": "response-turn-2",
            "input_tokens": 120,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
        }
        yield {
            "type": "agent_loop_start",
            "session_id": "session-1",
        }
        yield {
            "type": "content",
            "content": "完成",
            "turn": 2,
            "response_id": "response-turn-2",
        }
        yield {
            "type": "done",
            "response": {"history": [], "files": None},
            "response_id": "response-turn-2",
        }

    monkeypatch.setattr("app.core.session_reply_queue.executor.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_reply_queue_manager.freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_reply_stream_event_crud.get_latest_sequence",
        get_latest_sequence,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_reply_stream_event_crud.publish",
        publish,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_crud.update_llm_request_metadata",
        update_llm_request_metadata,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.send_session_stream_event",
        send_session_stream_event,
    )

    async def unexpected_non_stream_dispatch(**_kwargs):
        raise AssertionError("stream work must not use ChatDispatcher.dispatch")

    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch_stream", dispatch_stream)
    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch", unexpected_non_stream_dispatch)

    db = FakeDb()
    result = await _execute_foreground(db, work, "worker-1")

    assert result == {"history": [], "files": None, "response_id": "response-turn-2"}
    assert dispatch_kwargs["expose_tool_call_content"] is False
    assert [sequence_no for sequence_no, _event in published] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert [event["type"] for _sequence_no, event in published] == [
        "llm_request_metadata",
        "input_dequeued",
        "agent_loop_start",
        "turn_end",
        "tool_start",
        "tool_start",
        "tool_end",
        "llm_request_metadata",
        "input_dequeued",
        "agent_loop_start",
        "content",
    ]
    assert published[1][1]["request_ids"] == ["request-1"]
    assert published[0][1]["response_id"] == "response-turn-1"
    assert published[0][1]["work_sequence_no"] == 1
    assert published[0][1]["event_sequence_no"] == 1
    assert published[3][1]["response_id"] == "response-turn-1"
    assert [event["tool_call_id"] for _sequence_no, event in published[4:6]] == ["call-1", "call-2"]
    assert published[6][1]["tool_call_id"] == "call-1"
    assert published[8][1]["request_ids"] == ["request-2"]
    assert published[7][1]["response_id"] == "response-turn-2"
    assert published[7][1]["work_sequence_no"] == 1
    assert published[7][1]["event_sequence_no"] == 8
    assert published[10][1]["response_id"] == "response-turn-2"
    assert all(event["session_id"] == "session-1" for _sequence_no, event in published)
    assert all(event["work_id"] == 7 for _sequence_no, event in published)
    assert db.flush_calls == 0
    assert len(event_db_commits) == len(published)
    assert len(stream_event_calls) == 1
    assert stream_event_calls[0]["event"]["tool_names"] == ["execute_shell", "read_text_file"]
    assert stream_event_calls[0]["event"]["content"] == "我先检查"
    assert stream_event_calls[0]["persisted_event_count"] == 6
    assert metadata_updates == [
        {
            "db_type": "EventDb",
            "session_id": "session-1",
            "uid": "user-1",
            "metadata": {
                "type": "llm_request_metadata",
                "turn": 1,
                "response_id": "response-turn-1",
                "input_tokens": 100,
                "context_window_tokens": 4096,
                "max_output_tokens": 512,
                "session_id": "session-1",
                "work_id": 7,
                "work_sequence_no": 1,
                "event_sequence_no": 1,
            },
            "commit": False,
        },
        {
            "db_type": "EventDb",
            "session_id": "session-1",
            "uid": "user-1",
            "metadata": {
                "type": "llm_request_metadata",
                "turn": 2,
                "response_id": "response-turn-2",
                "input_tokens": 120,
                "context_window_tokens": 4096,
                "max_output_tokens": 512,
                "session_id": "session-1",
                "work_id": 7,
                "work_sequence_no": 1,
                "event_sequence_no": 8,
            },
            "commit": False,
        },
    ]


@pytest.mark.asyncio
async def test_execute_foreground_stream_reraises_llm_exception(monkeypatch):
    work = SimpleNamespace(
        id=9,
        sequence_no=1,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="foreground-message:session-1:13",
        created_at=datetime(2026, 7, 13, 21, 0, 0),
        execution_state={"stream_requested": True},
    )

    class FakeDb:
        async def refresh(self, instance):
            return None

    class EventDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def freeze_foreground_input(db, *, work, worker_id):
        return "测试", [], [13]

    async def get_latest_sequence(db, *, work_id):
        return 0

    async def dispatch_stream(**kwargs):
        assert kwargs["raise_errors"] is True
        raise LLMException(message=ERR_LLM_STREAM_TIMEOUT, timeout=120.0)
        yield

    monkeypatch.setattr("app.core.session_reply_queue.executor.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_reply_queue_manager.freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_reply_stream_event_crud.get_latest_sequence",
        get_latest_sequence,
    )
    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch_stream", dispatch_stream)

    with pytest.raises(LLMException) as exc_info:
        await _execute_foreground(FakeDb(), work, "worker-1")

    assert not isinstance(exc_info.value, RuntimeError)
    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert exc_info.value.kwargs["timeout"] == 120.0


@pytest.mark.asyncio
async def test_execute_foreground_persists_non_stream_llm_request_metadata(monkeypatch):
    work = SimpleNamespace(
        id=8,
        sequence_no=2,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="foreground-message:session-1:12",
        created_at=datetime(2026, 7, 13, 21, 0, 0),
        execution_state={
            "stream_requested": False,
            "request_ids": ["request-1"],
        },
    )
    metadata_updates = []

    class FakeDb:
        async def refresh(self, instance):
            return None

    class EventDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def freeze_foreground_input(db, *, work, worker_id):
        return "测试", [], [12]

    async def get_latest_sequence(db, *, work_id):
        return 0

    async def dispatch(**kwargs):
        return {
            "history": [],
            "files": None,
            "llm_request_metadata": {
                "type": "llm_request_metadata",
                "turn": 1,
                "response_id": "response-turn-1",
                "input_tokens": 222,
                "context_window_tokens": 4096,
                "max_output_tokens": 512,
                "work_id": 8,
                "work_sequence_no": 2,
            },
        }

    async def update_llm_request_metadata(db, *, session_id, uid, metadata, commit=True):
        metadata_updates.append(
            {
                "session_id": session_id,
                "uid": uid,
                "metadata": metadata,
                "commit": commit,
            }
        )
        return True

    async def unexpected_stream_dispatch(**_kwargs):
        raise AssertionError("non-stream work must not use ChatDispatcher.dispatch_stream")

    monkeypatch.setattr("app.core.session_reply_queue.executor.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_reply_queue_manager.freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_reply_stream_event_crud.get_latest_sequence",
        get_latest_sequence,
    )
    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch", dispatch)
    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch_stream", unexpected_stream_dispatch)
    monkeypatch.setattr(
        "app.core.session_reply_queue.executor.session_crud.update_llm_request_metadata",
        update_llm_request_metadata,
    )

    result = await _execute_foreground(FakeDb(), work, "worker-1")

    assert result["llm_request_metadata"]["input_tokens"] == 222
    assert metadata_updates == [
        {
            "session_id": "session-1",
            "uid": "user-1",
            "metadata": {
                "type": "llm_request_metadata",
                "turn": 1,
                "response_id": "response-turn-1",
                "input_tokens": 222,
                "context_window_tokens": 4096,
                "max_output_tokens": 512,
                "work_id": 8,
                "work_sequence_no": 2,
            },
            "commit": False,
        }
    ]


@pytest.mark.asyncio
async def test_publish_interactive_stream_dequeues_request_ids_once_across_agent_loop_boundaries(monkeypatch):
    work = SimpleNamespace(
        id=7,
        sequence_no=1,
        uid="user-1",
        session_id="session-1",
        execution_state={"request_ids": ["request-1"]},
    )
    persisted_events = []
    commits = []
    refresh_count = 0

    class EventDb:
        async def commit(self):
            commits.append(True)

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeDb:
        async def refresh(self, refreshed_work):
            nonlocal refresh_count
            refresh_count += 1
            if refresh_count == 1:
                refreshed_work.execution_state = {"request_ids": ["request-1", "request-2", "request-1"]}

    async def publish(_db, *, work_id, sequence_no, event, commit):
        assert commit is False
        persisted_events.append((work_id, sequence_no, dict(event)))

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        executor_module.session_reply_stream_event_crud,
        "publish",
        publish,
    )
    stream_state = executor_module._InteractiveWorkStreamEventState(
        work=work,
        next_sequence=1,
        dequeued_request_ids=set(),
        turn_end_content_by_response_id={},
        tool_names_by_response_id={},
    )
    db = FakeDb()

    await asyncio.wait_for(
        executor_module._publish_interactive_work_stream_event(
            db,
            stream_state,
            {"type": "agent_loop_start", "response_id": "response-1"},
        ),
        timeout=1,
    )
    work.execution_state["request_ids"].extend(["request-3", "request-2"])
    await asyncio.wait_for(
        executor_module._publish_interactive_work_stream_event(
            db,
            stream_state,
            {"type": "agent_loop_start", "response_id": "response-2"},
        ),
        timeout=1,
    )

    dequeued_events = [event for _work_id, _sequence_no, event in persisted_events if event["type"] == "input_dequeued"]
    assert [event["request_ids"] for event in dequeued_events] == [["request-1", "request-2"], ["request-3"]]
    assert [request_id for event in dequeued_events for request_id in event["request_ids"]] == ["request-1", "request-2", "request-3"]
    assert [event["type"] for _work_id, _sequence_no, event in persisted_events] == [
        "input_dequeued",
        "agent_loop_start",
        "input_dequeued",
        "agent_loop_start",
    ]
    assert [sequence_no for _work_id, sequence_no, _event in persisted_events] == [1, 2, 3, 4]
    assert len(commits) == 4


@pytest.mark.asyncio
async def test_wait_for_stream_switches_merged_target_without_replay_before_terminal_done(monkeypatch):
    manager = SessionReplyQueueManager()
    target_work = SimpleNamespace(
        id=8,
        session_id="session-1",
        status=SessionReplyWorkStatus.RUNNING,
        execution_state={"request_ids": ["request-1", "request-2", "request-1"]},
        error=None,
        result_message_id=91,
    )
    resolve_calls = []
    stream_queries = []
    late_unmerged_request_ids = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(_db, work_id):
        resolve_calls.append(work_id)
        if len(resolve_calls) == 1:
            return target_work
        target_work.status = SessionReplyWorkStatus.SUCCEEDED
        target_work.execution_state = {
            "request_ids": ["request-1", "request-2", "request-1"],
            "response": {
                "response_id": "response-final",
                "history": [{"role": "assistant", "content": "complete"}],
                "files": None,
            },
        }
        return target_work

    async def list_after_sequence(_db, *, work_id, after_sequence_no):
        stream_queries.append((work_id, after_sequence_no))
        if after_sequence_no == 0:
            return [
                SimpleNamespace(
                    sequence_no=1,
                    event={
                        "type": "input_dequeued",
                        "session_id": "session-1",
                        "work_id": 8,
                        "request_ids": ["request-1", "request-2"],
                    },
                ),
                SimpleNamespace(
                    sequence_no=2,
                    event={"type": "content", "content": "first", "response_id": "response-final"},
                ),
            ]
        late_unmerged_request_ids.append("request-3")
        return [
            SimpleNamespace(
                sequence_no=3,
                event={"type": "content", "content": " last", "response_id": "response-final"},
            ),
            SimpleNamespace(
                sequence_no=4,
                event={"type": "turn_end", "response_id": "response-final", "message_id": 91},
            ),
        ]

    async def no_sleep(_delay):
        return None

    async def collect_stream():
        return [event async for event in manager.wait_for_stream(7)]

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_work_item_crud.resolve_merged_target",
        resolve_merged_target,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager.session_reply_stream_event_crud.list_after_sequence",
        list_after_sequence,
    )
    monkeypatch.setattr("app.core.session_reply_queue.manager.asyncio.sleep", no_sleep)

    yielded = await asyncio.wait_for(collect_stream(), timeout=1)

    assert resolve_calls == [7, 8]
    assert stream_queries == [(8, 0), (8, 2)]
    assert [event["type"] for event in yielded] == [
        "input_dequeued",
        "content",
        "content",
        "turn_end",
        "done",
    ]
    assert "".join(event["content"] for event in yielded if event["type"] == "content") == "first last"
    assert yielded[-2]["response_id"] == yielded[-1]["response_id"] == "response-final"
    assert yielded[-1]["message_id"] == 91
    assert yielded[-1]["request_ids"] == ["request-1", "request-2"]
    assert yielded[0]["request_ids"] == ["request-1", "request-2"]
    assert late_unmerged_request_ids == ["request-3"]
    assert all("request-3" not in event.get("request_ids", []) for event in yielded)
