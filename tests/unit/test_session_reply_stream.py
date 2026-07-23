from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.adapters import chat_web as chat_web_module
from app.core.constants import ERR_LLM_STREAM_TOOL_CALL_AMBIGUOUS
from app.core.exceptions import BaseBusinessException, LLMException
from app.core.session_reply_queue.executor import _execute_foreground
from app.core.session_reply_queue.manager import (
    SessionReplyQueueManager,
    build_foreground_message_dedupe_key,
)
from app.core.utils.dispatcher.helpers import dump_output_history
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.session_reply_work_item import SessionReplyWorkStatus
from app.providers.llm import client as llm_client_module
from app.providers.llm.client import LLMClient


def test_llm_request_context_debug_log_contains_counts_and_estimated_tokens(monkeypatch):
    bound_fields = {}
    logged_messages = []

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
    monkeypatch.setattr(llm_client_module, "estimate_tokens", lambda _content: 321)

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
    assert response.model == "model-final"
    assert response.usage["total_tokens"] == 3


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

        def from_provider(self, _raw_response):
            return InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="provider-call", name="search", arguments={"query": "MonoLight"})],
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


def test_foreground_message_dedupe_key_scopes_reused_message_id_to_session():
    first = build_foreground_message_dedupe_key("session-1", 1)
    second = build_foreground_message_dedupe_key("session-2", 1)

    assert first != second
    assert first == build_foreground_message_dedupe_key("session-1", 1)
    assert first.startswith("foreground-message:")
    assert len(first) <= 160


def test_dump_output_history_can_hide_tool_call_content_without_mutating_messages():
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
        expose_tool_call_content=False,
    )

    assert visible_history[0]["content"] == "准备查询资料"
    assert "content" not in hidden_history[0]
    assert hidden_history[0]["tool_calls"][0]["name"] == "search"
    assert tool_message.content == "准备查询资料"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "stream_requested", "context_summary_events_requested", "expose_tool_call_content"),
    [
        ("http", False, False, True),
        ("ws", True, True, True),
        ("weixin-openclaw", False, False, False),
    ],
)
async def test_enqueue_foreground_controls_tool_call_content_by_source(
    monkeypatch,
    source,
    stream_requested,
    context_summary_events_requested,
    expose_tool_call_content,
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
        return None

    async def enqueue(*args, **kwargs):
        enqueue_calls.append(kwargs)
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
        source=source,
        request_id="request-1",
    )

    assert queued_work.execution_state["stream_requested"] is stream_requested
    assert queued_work.execution_state["context_summary_events_requested"] is context_summary_events_requested
    assert queued_work.execution_state["expose_tool_call_content"] is expose_tool_call_content
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
        return SimpleNamespace(id=1), SimpleNamespace(id=9), "queued"

    async def wait_for_stream(_work_id):
        yield {"type": "done", "response": {"history": [], "files": None}}

    monkeypatch.setattr(chat_web_module, "ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr(chat_web_module.profile_crud, "get_active", get_profile)
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
    assert events[0] == {
        "type": "input_queued",
        "session_id": "session-1",
        "request_id": "request-1",
        "work_id": 9,
        "submission_status": "queued",
    }
    assert events[-1]["type"] == "done"


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
        return SimpleNamespace(id=1), original_work, "queued"

    async def wait_for_result(work_id):
        assert work_id == 9
        raise BaseBusinessException(
            message="等待对话模型首字响应超时（60.0 秒）",
            default_message="等待对话模型首字响应超时（60.0 秒）",
            data={"work_id": 7, "event_id": resolved_event_id},
        )

    monkeypatch.setattr(chat_web_module, "ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr(chat_web_module.profile_crud, "get_active", get_profile)
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
        return "运行工具", [], [11]

    async def get_latest_sequence(db, *, work_id):
        return 0

    async def publish(db, *, work_id, sequence_no, event):
        published.append((sequence_no, event))

    async def dispatch_stream(**kwargs):
        dispatch_kwargs.update(kwargs)
        yield {
            "type": "agent_loop_start",
            "session_id": "session-1",
        }
        yield {
            "type": "tool_start",
            "name": "search",
            "arguments": {"query": "MonoLight"},
            "tool_call_id": "call-1",
            "response_id": "response-turn-1",
        }
        work.execution_state["request_ids"].append("request-2")
        yield {
            "type": "tool_end",
            "name": "search",
            "result": '{"status":"success"}',
            "tool_call_id": "call-1",
            "response_id": "response-turn-1",
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

    async def unexpected_non_stream_dispatch(**_kwargs):
        raise AssertionError("stream work must not use ChatDispatcher.dispatch")

    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch_stream", dispatch_stream)
    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch", unexpected_non_stream_dispatch)

    result = await _execute_foreground(FakeDb(), work, "worker-1")

    assert result == {"history": [], "files": None}
    assert dispatch_kwargs["expose_tool_call_content"] is False
    assert [sequence_no for sequence_no, _event in published] == [1, 2, 3, 4, 5, 6, 7]
    assert [event["type"] for _sequence_no, event in published] == [
        "input_dequeued",
        "agent_loop_start",
        "tool_start",
        "tool_end",
        "input_dequeued",
        "agent_loop_start",
        "content",
    ]
    assert published[0][1]["request_ids"] == ["request-1"]
    assert published[2][1]["response_id"] == "response-turn-1"
    assert published[3][1]["tool_call_id"] == "call-1"
    assert published[4][1]["request_ids"] == ["request-2"]
    assert published[6][1]["response_id"] == "response-turn-2"
    assert all(event["session_id"] == "session-1" for _sequence_no, event in published)
    assert all(event["work_id"] == 7 for _sequence_no, event in published)
