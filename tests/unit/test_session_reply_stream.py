from types import SimpleNamespace

import pytest

from app.core.session_reply_queue.executor import _execute_foreground
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.models.message import InternalMessage, MessageRole
from app.models.session_reply_work_item import SessionReplyWorkStatus
from app.providers.llm.client import LLMClient


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
        execution_state={"stream_requested": True},
    )
    published: list[tuple[int, dict]] = []

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
