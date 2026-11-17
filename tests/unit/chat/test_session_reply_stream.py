import asyncio
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.adapters import chat_web as chat_web_module
from app.core.constants import ERR_LLM_STREAM_TIMEOUT
from app.core.dispatchers import stream as stream_module
from app.core.dispatchers.stream import StreamDispatcherMixin
from app.core.exceptions import BaseBusinessException, LLMException
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_replies as executor_replies_module
from app.core.session_reply_queue.executor_replies import _execute_foreground
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.core.session_reply_queue.manager_common import build_foreground_message_dedupe_key
from app.core.utils.dispatcher.helpers import dump_output_history
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.session_reply_work_item import SessionReplyWorkCategory, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.llm import client as llm_client_module


@pytest.fixture
def no_checkpoint(monkeypatch):
    async def get_checkpoint(_db, _work_id):
        return None

    monkeypatch.setattr(
        executor_replies_module.session_reply_work_item_crud,
        "get_checkpoint",
        get_checkpoint,
    )


def test_llm_request_context_debug_log_does_not_estimate_tokens(monkeypatch):
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

    def unexpected_estimate(*_args, **_kwargs):
        raise AssertionError("request logging must not perform local token estimation")

    monkeypatch.setattr(
        llm_client_module,
        "estimate_request_context_tokens",
        unexpected_estimate,
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
            "LLM request context: model={model_id}, protocol={protocol}, streaming={streaming}, messages={message_count}, roles={role_counts}, tools={tool_count}, request_context_tokens={request_context_tokens}, max_output_tokens={max_tokens}",
            {
                "model_id": "model-1",
                "protocol": "openai",
                "streaming": False,
                "message_count": 3,
                "role_counts": {"system": 1, "user": 1, "assistant": 1},
                "tool_count": 1,
                "request_context_tokens": None,
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
        "request_context_tokens": None,
        "max_output_tokens": 512,
    }


def test_llm_request_context_debug_log_reuses_provided_token_count(monkeypatch):
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

    assert bound_fields["request_context_tokens"] == 123
    assert logged_fields["request_context_tokens"] == 123


@pytest.mark.asyncio
async def test_dispatch_stream_raise_errors_controls_business_exception_surface(monkeypatch):
    async def dispatch_interactive(**_kwargs):
        raise LLMException(message=ERR_LLM_STREAM_TIMEOUT, timeout=120.0)

    monkeypatch.setattr(stream_module, "dispatch_interactive", dispatch_interactive)

    class FailingStreamDispatcher(StreamDispatcherMixin):
        @classmethod
        async def validate_initial_message_before_save(cls, *_args, **_kwargs):
            return None

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

    async def persist(db, *, message, commit=True):
        db.add(message)
        await db.flush()
        return message

    async def upsert_profile(*args, **kwargs):
        return SimpleNamespace(show_tool_calls=show_tool_calls)

    async def enqueue(*args, **kwargs):
        enqueue_calls.append(kwargs)
        return work, True

    async def activate_and_get_guidance_prompt(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_enqueue.session_crud.upsert_profile",
        upsert_profile,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_enqueue.message_crud.persist",
        persist,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_enqueue.session_reply_work_item_crud.enqueue",
        enqueue,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_enqueue.message_crud.activate_and_get_guidance_prompt",
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

    async def persist(db, *, message, commit=True):
        db.add(message)
        await db.flush()
        return message

    async def upsert_profile(*args, **kwargs):
        return SimpleNamespace(
            session_id=kwargs["session_id"],
            uid=kwargs["uid"],
            profile_id=kwargs["profile_id"],
            source=kwargs.get("source", "http"),
            show_tool_calls=True,
        )

    async def enqueue(*args, **kwargs):
        return work, True

    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_enqueue.session_crud.upsert_profile",
        upsert_profile,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_enqueue.message_crud.persist",
        persist,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_enqueue.session_reply_work_item_crud.enqueue",
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

    async def get_todo(*_args, **_kwargs):
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
    monkeypatch.setattr(chat_web_module.session_todo_crud, "get_by_session_id", get_todo)
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
            sequence_no=1,
            session_id="session-1",
            status=SessionReplyWorkStatus.RUNNING,
            execution_state={},
            error=None,
        ),
        SimpleNamespace(
            id=7,
            sequence_no=1,
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
        return states.pop(0) if len(states) > 1 else states[0]

    async def list_after_sequence(db, *, work_id: int, after_sequence_no: int):
        return stream_events.pop(0) if stream_events else []

    async def no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_result.session_reply_work_item_crud.resolve_merged_target",
        resolve_merged_target,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_result.session_reply_stream_event_crud.list_after_sequence",
        list_after_sequence,
    )
    monkeypatch.setattr("app.core.session_reply_queue.manager_result.asyncio.sleep", no_sleep)

    yielded = [event async for event in manager.wait_for_stream(7)]

    assert [event["type"] for event in yielded] == ["content", "content", "done"]
    assert "".join(event["content"] for event in yielded if event["type"] == "content") == "你好"
    assert yielded[-1]["history"] == [{"role": "assistant", "content": "你好"}]
    assert yielded[-1]["request_ids"] == ["request-1", "request-2"]
    assert yielded[-1]["work_sequence_no"] == 1


@pytest.mark.asyncio
async def test_wait_for_stream_returns_identified_error_event(monkeypatch):
    manager = SessionReplyQueueManager()
    work = SimpleNamespace(
        id=7,
        sequence_no=1,
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
        "app.core.session_reply_queue.manager_result.session_reply_work_item_crud.resolve_merged_target",
        resolve_merged_target,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_result.session_reply_stream_event_crud.list_after_sequence",
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
    assert yielded[0]["work_sequence_no"] == 1


@pytest.mark.asyncio
async def test_execute_foreground_persists_each_tool_event_with_original_response_id(monkeypatch, no_checkpoint):
    work = SimpleNamespace(
        id=7,
        work_category=SessionReplyWorkCategory.FOREGROUND,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        sequence_no=1,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        subagent_id=None,
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

    async def get_work_snapshot(_db, work_id):
        assert work_id == work.id
        return SimpleNamespace(**deepcopy(vars(work)))

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

    async def update_claimed(db, **kwargs):
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

    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        executor_replies_module.session_reply_queue_manager,
        "freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_reply_stream_event_crud,
        "get_latest_sequence",
        get_latest_sequence,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_reply_work_item_crud,
        "get",
        get_work_snapshot,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_reply_stream_event_crud,
        "publish",
        publish,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_crud,
        "update_llm_request_metadata",
        update_llm_request_metadata,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_reply_work_item_crud,
        "update_claimed",
        update_claimed,
    )
    monkeypatch.setattr(
        executor_interactive_module,
        "send_session_stream_event",
        send_session_stream_event,
    )

    async def unexpected_non_stream_dispatch(**_kwargs):
        raise AssertionError("stream work must not use ChatDispatcher.dispatch")

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch_stream", dispatch_stream)
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", unexpected_non_stream_dispatch)

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
async def test_execute_foreground_stream_reraises_llm_exception(monkeypatch, no_checkpoint):
    work = SimpleNamespace(
        id=9,
        work_category=SessionReplyWorkCategory.FOREGROUND,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        sequence_no=1,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        subagent_id=None,
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

    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        executor_replies_module.session_reply_queue_manager,
        "freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_reply_stream_event_crud,
        "get_latest_sequence",
        get_latest_sequence,
    )
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch_stream", dispatch_stream)

    with pytest.raises(LLMException) as exc_info:
        await _execute_foreground(FakeDb(), work, "worker-1")

    assert not isinstance(exc_info.value, RuntimeError)
    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert exc_info.value.kwargs["timeout"] == 120.0


@pytest.mark.asyncio
async def test_execute_foreground_persists_non_stream_llm_request_metadata(monkeypatch, no_checkpoint):
    work = SimpleNamespace(
        id=8,
        work_category=SessionReplyWorkCategory.FOREGROUND,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        sequence_no=2,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        subagent_id=None,
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

    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        executor_replies_module.session_reply_queue_manager,
        "freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_reply_stream_event_crud,
        "get_latest_sequence",
        get_latest_sequence,
    )
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch_stream", unexpected_stream_dispatch)
    monkeypatch.setattr(
        executor_interactive_module.session_crud,
        "update_llm_request_metadata",
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
async def test_wait_for_stream_switches_merged_target_without_replay_before_terminal_done(monkeypatch):
    manager = SessionReplyQueueManager()
    target_work = SimpleNamespace(
        id=8,
        sequence_no=2,
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
        if after_sequence_no == 4:
            return []
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
        "app.core.session_reply_queue.manager_result.session_reply_work_item_crud.resolve_merged_target",
        resolve_merged_target,
    )
    monkeypatch.setattr(
        "app.core.session_reply_queue.manager_result.session_reply_stream_event_crud.list_after_sequence",
        list_after_sequence,
    )
    monkeypatch.setattr("app.core.session_reply_queue.manager_result.asyncio.sleep", no_sleep)

    yielded = await asyncio.wait_for(collect_stream(), timeout=1)

    assert resolve_calls == [7, 8, 8]
    assert stream_queries == [(8, 0), (8, 2), (8, 4)]
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
    assert yielded[-1]["work_sequence_no"] == 2
    assert yielded[0]["request_ids"] == ["request-1", "request-2"]
    assert late_unmerged_request_ids == ["request-3"]
    assert all("request-3" not in event.get("request_ids", []) for event in yielded)
