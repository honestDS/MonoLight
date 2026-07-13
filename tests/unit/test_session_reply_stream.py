from datetime import datetime
from types import SimpleNamespace

import pytest

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
    assert response.message.tool_calls[0].id == "call-1"
    assert response.message.tool_calls[0].name == "search"
    assert response.message.tool_calls[0].arguments == {"query": "MonoLight"}
    assert response.model == "model-final"
    assert response.usage["total_tokens"] == 3


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
    ("source", "stream_requested", "expose_tool_call_content"),
    [
        ("http", False, True),
        ("ws", True, True),
        ("weixin-openclaw", False, False),
    ],
)
async def test_enqueue_foreground_controls_tool_call_content_by_source(
    monkeypatch,
    source,
    stream_requested,
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
    )

    assert queued_work.execution_state["stream_requested"] is stream_requested
    assert queued_work.execution_state["expose_tool_call_content"] is expose_tool_call_content
    assert enqueue_calls[0]["dedupe_key"] == build_foreground_message_dedupe_key("session-1", 11)


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
                }
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

    async def dispatch(**kwargs):
        dispatch_kwargs.update(kwargs)
        callback = kwargs["stream_event_callback"]
        await callback(
            {
                "type": "tool_start",
                "name": "search",
                "arguments": {"query": "MonoLight"},
                "tool_call_id": "call-1",
                "response_id": "response-turn-1",
            }
        )
        await callback(
            {
                "type": "tool_end",
                "name": "search",
                "result": '{"status":"success"}',
                "tool_call_id": "call-1",
                "response_id": "response-turn-1",
            }
        )
        await callback(
            {
                "type": "content",
                "content": "完成",
                "turn": 2,
                "response_id": "response-turn-2",
            }
        )
        return {"history": [], "files": None}

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
    monkeypatch.setattr("app.core.session_reply_queue.executor.ChatDispatcher.dispatch", dispatch)

    result = await _execute_foreground(FakeDb(), work, "worker-1")

    assert result == {"history": [], "files": None}
    assert dispatch_kwargs["expose_tool_call_content"] is False
    assert [sequence_no for sequence_no, _event in published] == [1, 2, 3]
    assert [event["type"] for _sequence_no, event in published] == [
        "tool_start",
        "tool_end",
        "content",
    ]
    assert published[0][1]["response_id"] == "response-turn-1"
    assert published[1][1]["tool_call_id"] == "call-1"
    assert published[2][1]["response_id"] == "response-turn-2"
    assert all(event["session_id"] == "session-1" for _sequence_no, event in published)
    assert all(event["work_id"] == 7 for _sequence_no, event in published)
