import json
from copy import deepcopy
from datetime import datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from app.core.constants import (
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
    TOOL_WORKFLOW_FOREGROUND,
    TOOL_WORKFLOW_SCHEDULED_TASK,
)
from app.core.session_reply_queue import (
    executor as executor_module,
)
from app.core.session_reply_queue import (
    executor_audit as executor_audit_module,
)
from app.core.session_reply_queue import (
    executor_common as executor_common_module,
)
from app.core.session_reply_queue import (
    executor_confirmed as executor_confirmed_module,
)
from app.core.session_reply_queue import (
    executor_interactive as executor_interactive_module,
)
from app.core.session_reply_queue import (
    executor_lifecycle as executor_lifecycle_module,
)
from app.core.session_reply_queue import (
    executor_metadata as executor_metadata_module,
)
from app.core.session_reply_queue import (
    executor_replies as executor_replies_module,
)
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.provider_usage import ProviderUsageType
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.unit.response_test_support import (
    llm_response_payload as _raw_llm_response_payload,
)


@pytest.mark.asyncio
async def test_background_task_resume_dispatches_deferred_external_result_with_frozen_context(monkeypatch, checkpoint_store):
    frozen_message = InternalMessage(
        id=3,
        role=MessageRole.USER,
        content="任务快照",
    )
    task = SimpleNamespace(
        id=8,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        extra={"frozen_context_messages": [frozen_message.model_dump(mode="json", exclude_none=True)]},
    )
    external_result = {
        "task_id": task.id,
        "status": "succeeded",
        "result": {"content": "后台工具结果"},
    }
    work = SimpleNamespace(
        id=7,
        source_id=str(task.id),
        uid=task.uid,
        session_id=task.session_id,
        profile_id=task.profile_id,
        work_type=SessionReplyWorkType.BACKGROUND_TASK_RESUME,
        execution_state={"external_resume_context": [external_result]},
    )
    checkpoint = {
        "messages": [],
        "deferred_external_results": True,
        "reload_history": True,
    }
    checkpoint_store[work.id] = checkpoint
    captured = {}

    async def get_task(db, task_id):
        assert task_id == task.id
        return task

    async def get_profile(db, profile_id):
        assert profile_id == work.profile_id
        return SimpleNamespace(id=profile_id, uid=work.uid)

    async def update_claimed(db, **kwargs):
        return True

    async def dispatch_interactive_work(db, **kwargs):
        captured.update(kwargs)
        return {
            "content": "后台续接完成",
            "history": [{"role": "assistant", "content": "后台续接完成"}],
            "files": [],
        }

    monkeypatch.setattr(executor_replies_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_replies_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_replies_module, "_dispatch_interactive_work", dispatch_interactive_work)
    monkeypatch.setattr(
        executor_replies_module,
        "_generate_auxiliary_reply_with_request_metadata",
        lambda *args, **kwargs: pytest.fail("background task resume must not use auxiliary reply"),
    )

    response = await executor_replies_module._execute_external_resume(object(), work, "worker-1")

    assert response["content"] == "后台续接完成"
    assert json.loads(captured["message"]) == external_result
    assert captured["allow_additional_user_messages"] is False
    assert captured["execution_scope"] == "foreground"
    assert [message.content for message in captured["frozen_context_messages"]] == ["任务快照"]


def llm_response_payload(*args, files=None, **kwargs):
    if files is not None and any(not isinstance(item, dict) for item in files):
        payload = _raw_llm_response_payload(*args, **kwargs)
        payload["files"] = list(files)
        return payload
    return _raw_llm_response_payload(*args, files=files, **kwargs)


@pytest.fixture(autouse=True)
def _isolate_confirmed_tool_runtime_dependencies(monkeypatch):
    async def get_session(_db, session_id):
        return SimpleNamespace(
            session_id=session_id,
            llm_request_metadata={"context_window_tokens": 4096},
        )

    async def resolve_work_tool_workflow(_db, _work):
        return TOOL_WORKFLOW_FOREGROUND

    async def persist_todo_snapshot(_db, *, uid, session_id, tool_results):
        return None

    monkeypatch.setattr(
        executor_confirmed_module,
        "session_crud",
        SimpleNamespace(get_by_session_id=get_session),
    )
    monkeypatch.setattr(
        executor_confirmed_module,
        "resolve_work_tool_workflow",
        resolve_work_tool_workflow,
    )
    monkeypatch.setattr(
        executor_confirmed_module,
        "persist_session_todo_snapshot_on_tool_results",
        persist_todo_snapshot,
    )


@pytest.fixture(autouse=True)
def checkpoint_store(monkeypatch):
    checkpoints = {}

    async def get_checkpoint(_db, work_id):
        return checkpoints.get(work_id)

    async def save_checkpoint(_db, work_id, checkpoint):
        checkpoints[work_id] = checkpoint

    monkeypatch.setattr(executor_replies_module.session_reply_work_item_crud, "get_checkpoint", get_checkpoint)
    monkeypatch.setattr(executor_replies_module.session_reply_work_item_crud, "save_checkpoint", save_checkpoint)
    return checkpoints


@pytest.fixture(autouse=True)
def frozen_context_store(monkeypatch):
    messages_by_id = {}

    async def get_frozen_context(_db, work):
        message_ids = work.execution_state.get("frozen_context_message_ids")
        if message_ids is None:
            return None
        return [messages_by_id[message_id] for message_id in message_ids]

    async def save_frozen_context(_db, work_id, messages):
        next_message_id = max(messages_by_id, default=0) + 1
        message_ids = []
        for message in messages:
            messages_by_id[next_message_id] = message
            message_ids.append(next_message_id)
            next_message_id += 1
        return message_ids

    monkeypatch.setattr(executor_replies_module.session_reply_work_item_crud, "get_frozen_context", get_frozen_context)
    monkeypatch.setattr(executor_replies_module.session_reply_work_item_crud, "save_frozen_context", save_frozen_context)
    return messages_by_id


@pytest.mark.asyncio
async def test_foreground_executor_resumes_persisted_checkpoint(monkeypatch, checkpoint_store):
    checkpoint = {
        "messages": [
            {"role": "user", "content": "original"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tool-1", "name": "execute_shell", "arguments": {"command": "echo 1"}}],
            },
            {"role": "tool", "content": "1", "tool_call_id": "tool-1"},
        ],
        "turn_messages": [],
        "files_to_user": [],
        "current_turn": 1,
        "memory_recall_boundary_message_id": 1,
        "memory_recall_status": "completed",
    }
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
        input_message_ids=[1],
        execution_state={
            "stream_requested": False,
            "additional_system_prompt": "channel instruction",
        },
    )
    checkpoint_store[work.id] = checkpoint
    dispatch_kwargs = {}

    class FakeDb:
        def __init__(self):
            self.pending_state = None
            self.pending_checkpoint = None

        async def refresh(self, instance) -> None:
            return None

        async def commit(self) -> None:
            if self.pending_state is not None:
                work.execution_state = deepcopy(self.pending_state)
            if self.pending_checkpoint is not None:
                checkpoint_store[work.id] = deepcopy(self.pending_checkpoint)
            self.pending_state = None
            self.pending_checkpoint = None

        async def rollback(self) -> None:
            self.pending_state = None
            self.pending_checkpoint = None

    class EventDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def freeze_foreground_input(db, *, work, worker_id):
        return "original", [], [1]

    async def latest_sequence(db, *, work_id):
        return 0

    async def dispatch(**kwargs):
        dispatch_kwargs.update(kwargs)
        assert kwargs["persisted_initial_message"].assembled_attachment_part_count == 0
        assert await kwargs["context_summary_work_validity_checker"]() is True
        await kwargs["execution_checkpoint_callback"](
            {
                "messages": [{"role": "user", "content": "updated"}],
                "memory_recall_boundary_message_id": 1,
                "memory_recall_status": "completed",
            }
        )
        return llm_response_payload()

    async def get_active_claims(db, claims):
        assert isinstance(db, EventDb)
        assert claims == {7: "worker-1"}
        return {(7, "worker-1")}

    async def get_session(db, session_id):
        assert isinstance(db, EventDb)
        assert session_id == "session-1"
        return SimpleNamespace(
            uid="user-1",
            profile_id=1,
        )

    async def update_claimed(db, **kwargs):
        values = kwargs.get("values", {})
        if "execution_state" in values:
            db.pending_state = deepcopy(values["execution_state"])
        if kwargs.get("checkpoint") is not None:
            db.pending_checkpoint = deepcopy(kwargs["checkpoint"])
        return True

    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        executor_replies_module.session_reply_queue_manager,
        "freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(executor_interactive_module.session_reply_stream_event_crud, "get_latest_sequence", latest_sequence)
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(
        executor_interactive_module.session_reply_work_item_crud,
        "get_active_claims",
        get_active_claims,
    )
    monkeypatch.setattr(
        executor_interactive_module.session_crud,
        "get_by_session_id",
        get_session,
    )
    monkeypatch.setattr(executor_interactive_module.session_reply_work_item_crud, "update_claimed", update_claimed)

    db = FakeDb()
    response = await executor_replies_module._execute_foreground(db, work, "worker-1")

    assert response == llm_response_payload()
    assert dispatch_kwargs["execution_resume_state"] is checkpoint
    assert dispatch_kwargs["message"] == "original"
    assert dispatch_kwargs["additional_system_prompt"] == "channel instruction"
    saved_checkpoint = await executor_replies_module.session_reply_work_item_crud.get_checkpoint(db, work.id)
    assert saved_checkpoint["messages"][0]["content"] == "updated"
    assert saved_checkpoint["memory_recall_boundary_message_id"] == 1
    assert saved_checkpoint["memory_recall_status"] == "completed"
    assert work.execution_state["stream_requested"] is False
    assert work.execution_state["additional_system_prompt"] == "channel instruction"


@pytest.mark.asyncio
async def test_non_streaming_interactive_work_delegates_provider_usage_persistence(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=3,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
        input_message_ids=[1],
        execution_state={"stream_requested": False},
    )
    dispatch_kwargs = {}
    persistence_calls = []
    request_metadata = {
        "type": "llm_request_metadata",
        "channel_id": 11,
        "input_tokens": 100,
        "input_tokens_source": "provider",
        "cached_tokens": 25,
        "_provider_request_id": "provider-request-1",
        "_provider_input_tokens": 100,
        "_provider_cached_tokens": 25,
        "_provider_output_tokens": 0,
        "total_input_tokens": 1000,
        "total_cached_tokens": 250,
        "cache_hit_rate": 0.25,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
    }

    class EventDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def latest_sequence(db, *, work_id):
        assert isinstance(db, EventDb)
        assert work_id == 7
        return 0

    async def dispatch(**kwargs):
        dispatch_kwargs.update(kwargs)
        await kwargs["request_metadata_callback"](request_metadata)
        return llm_response_payload()

    async def persist_provider_usage(metadata, *, work, request_purpose, update_session_context):
        persistence_calls.append((dict(metadata), work, request_purpose, update_session_context))
        return None

    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_interactive_module.session_reply_stream_event_crud, "get_latest_sequence", latest_sequence)
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(
        executor_metadata_module,
        "_persist_session_reply_provider_usage_reliably",
        persist_provider_usage,
    )

    response = await executor_interactive_module._dispatch_interactive_work(
        object(),
        work=work,
        worker_id="worker-1",
        message="original",
        initial_message=InternalMessage(id=1, role=MessageRole.USER, content="original"),
        history_before_id=1,
        frozen_user_message_ids=[1],
        attachments=None,
        allow_additional_user_messages=False,
        execution_resume_state=None,
    )

    assert response == llm_response_payload()
    assert callable(dispatch_kwargs["request_metadata_callback"])
    assert len(persistence_calls) == 1
    metadata, persisted_work, request_purpose, update_session_context = persistence_calls[0]
    assert metadata == request_metadata
    assert persisted_work is work
    assert request_purpose == ProviderUsageType.MAIN_DIALOGUE
    assert update_session_context is True


@pytest.mark.asyncio
async def test_rejected_foreground_reply_uses_history_without_decision_user_input(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="11",
        dedupe_key="foreground-message:session-1:11",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
        input_message_ids=[11],
        execution_state={
            "audit_decision_response": True,
            "additional_system_prompt": "channel instruction",
        },
    )
    captured = {}
    metadata_updates = []
    request_metadata = {
        "type": "llm_request_metadata",
        "input_tokens": 123,
        "input_tokens_source": "provider",
        "context_window_tokens": 32768,
        "max_output_tokens": 2048,
    }

    class FakeDb:
        async def refresh(self, instance):
            return None

    async def freeze_foreground_input(db, *, work, worker_id):
        return "拒绝", [], [11]

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=1, uid="user-1")

    async def generate_reply(db, **kwargs):
        captured.update(kwargs)
        await kwargs["request_metadata_callback"](request_metadata)
        return InternalMessage(role=MessageRole.ASSISTANT, content="已取消"), [InternalMessage(role=MessageRole.ASSISTANT, content="已取消")], []

    async def update_request_metadata(db, **kwargs):
        metadata_updates.append(kwargs)

    monkeypatch.setattr(
        executor_replies_module.session_reply_queue_manager,
        "freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_metadata_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_metadata_module.session_crud, "update_llm_request_metadata", update_request_metadata)

    response = await executor_replies_module._execute_foreground(FakeDb(), work, "worker-1")

    assert response["choices"][0]["message"]["content"] == "已取消"
    assert response["history"][0]["role"] == MessageRole.ASSISTANT
    assert response["history"][0]["content"] == "已取消"
    expected_request_metadata = {
        **request_metadata,
        "request_purpose": "audit_auxiliary",
        "output_tokens": 0,
        "total_output_tokens": 0,
        "total_input_tokens": 0,
        "total_cached_tokens": 0,
        "cache_hit_rate": None,
        "work_id": 7,
        "work_sequence_no": 1,
    }
    assert response["llm_request_metadata"] == expected_request_metadata
    assert metadata_updates == []
    assert executor_common_module._event_for_work(work, response)["llm_request_metadata"] == expected_request_metadata
    assert captured["allow_tools"] is False
    assert captured["additional_system_prompt"] == "channel instruction"
    assert "extra_messages" not in captured
    assert "submission_context" not in captured
    assert captured["final_message_dedupe_key"] == executor_common_module._result_message_dedupe_key(work)


@pytest.mark.asyncio
async def test_foreground_executor_checkpoint_preserves_and_clears_active_audit_binding(monkeypatch):
    active_binding = {
        "audit_record_id": 42,
        "claim_token": "claim-token",
    }
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
        execution_state={SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: active_binding},
    )
    checkpoint_states = []

    class FakeDb:
        async def refresh(self, instance) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class EventDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def freeze_foreground_input(db, *, work, worker_id):
        return "original", [], [1]

    async def latest_sequence(db, *, work_id):
        return 0

    async def dispatch(**kwargs):
        await kwargs["execution_checkpoint_callback"]({"messages": []})
        checkpoint_states.append(deepcopy(work.execution_state))
        await kwargs["execution_checkpoint_callback"](
            {
                "messages": [],
                SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: None,
            }
        )
        checkpoint_states.append(deepcopy(work.execution_state))
        return llm_response_payload()

    async def update_claimed(db, **kwargs):
        return True

    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        executor_replies_module.session_reply_queue_manager,
        "freeze_foreground_input",
        freeze_foreground_input,
    )
    monkeypatch.setattr(executor_interactive_module.session_reply_stream_event_crud, "get_latest_sequence", latest_sequence)
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(executor_interactive_module.session_reply_work_item_crud, "update_claimed", update_claimed)

    await executor_replies_module._execute_foreground(FakeDb(), work, "worker-1")

    assert checkpoint_states[0][SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == active_binding
    assert SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in checkpoint_states[-1]


@pytest.mark.asyncio
async def test_executor_resumes_from_persisted_result_without_calling_llm(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
    )
    persisted_result = SimpleNamespace(
        id=9,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        content="saved response",
        created_at=work.created_at + timedelta(seconds=1),
    )
    sent_events = []
    update_calls = []
    terminal_calls = []
    llm_calls = []
    call_order = []

    class FakeSession:
        def __init__(self):
            self.info = {}

        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id: int):
        return work

    async def get_result(db, dedupe_key: str):
        return persisted_result

    async def execute_foreground(db, current_work, worker_id: str):
        llm_calls.append((current_work.id, worker_id))
        return {}

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        return True

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        call_order.append("terminal")
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        sent_events.append((uid, session_id, event))
        call_order.append("event")

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_lifecycle_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", send_event)

    await executor_module.execute_session_reply_work(work_id=7, worker_id="worker-1")

    assert llm_calls == []
    assert update_calls[0]["values"]["result_message_id"] == 9
    assert update_calls[0]["values"]["execution_state"]["response"]["choices"][0]["message"]["content"] == "saved response"
    assert update_calls[0]["values"]["execution_state"]["response"]["message_id"] == 9
    assert sent_events[0][2]["event_id"].startswith("session-reply-work:")
    assert sent_events[0][2]["event_id"].endswith(":event")
    assert sent_events[0][2]["event_id"] != "session-reply-work:7:event"
    assert sent_events[0][2]["message_id"] == 9
    assert terminal_calls[0]["status"] == SessionReplyWorkStatus.SUCCEEDED
    assert terminal_calls[0]["result_message_id"] == 9
    assert terminal_calls[0]["event_sent"] is True
    assert call_order.index("event") < call_order.index("terminal")


@pytest.mark.asyncio
async def test_executor_does_not_mark_terminal_when_event_delivery_fails(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
    )
    persisted_result = SimpleNamespace(
        id=9,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        content="saved response",
        created_at=work.created_at + timedelta(seconds=1),
    )
    terminal_calls = []

    class FakeSession:
        def __init__(self):
            self.info = {}

        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id: int):
        return work

    async def get_result(db, dedupe_key: str):
        return persisted_result

    async def update_claimed(db, **kwargs):
        return True

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        raise RuntimeError("event delivery failed")

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_lifecycle_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", send_event)

    with pytest.raises(RuntimeError, match="event delivery failed"):
        await executor_module.execute_session_reply_work(work_id=7, worker_id="worker-1")

    assert terminal_calls == []


@pytest.mark.asyncio
async def test_fail_executor_sends_event_before_marking_terminal(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
    )
    call_order = []
    terminal_calls = []
    sent_events = []

    class FakeSession:
        async def refresh(self, _object):
            return None

        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id: int):
        return work

    async def save_error_message(*args, **kwargs):
        return SimpleNamespace(id=9)

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        call_order.append("terminal")
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        sent_events.append((uid, session_id, event))
        call_order.append("event")

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_lifecycle_module, "save_message", save_error_message)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", send_event)

    await executor_module.fail_session_reply_work(
        work_id=7,
        worker_id="worker-1",
        error="internal error",
        user_error="user error",
    )

    assert sent_events[0][2]["type"] == "proactive_reply_error"
    assert sent_events[0][2]["message_id"] == 9
    assert call_order.index("event") < call_order.index("terminal")
    assert terminal_calls[0]["status"] == SessionReplyWorkStatus.FAILED
    assert terminal_calls[0]["error"] == "internal error"
    assert terminal_calls[0]["event_sent"] is True


@pytest.mark.asyncio
async def test_executor_does_not_query_legacy_result_prefix(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="current-session",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="35",
        dedupe_key="foreground-message:current:35",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
    )
    generated_result = SimpleNamespace(
        id=10,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        content="current response",
        created_at=work.created_at + timedelta(seconds=1),
    )
    llm_calls = []
    result_lookups = []
    update_calls = []
    terminal_calls = []
    sent_events = []

    class FakeSession:
        def __init__(self):
            self.info = {}

        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id: int):
        return work

    async def get_result(db, dedupe_key: str):
        result_lookups.append(dedupe_key)
        if llm_calls and dedupe_key == executor_common_module._result_message_dedupe_key(work):
            return generated_result
        return None

    async def execute_foreground(db, current_work, worker_id: str):
        llm_calls.append((current_work.id, worker_id))
        return llm_response_payload("current response")

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        return True

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        sent_events.append((uid, session_id, event))

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_lifecycle_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_lifecycle_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", send_event)

    await executor_module.execute_session_reply_work(work_id=7, worker_id="worker-1")

    assert llm_calls == [(7, "worker-1")]
    assert "session-reply-work:7:result" not in result_lookups
    assert update_calls[0]["values"]["result_message_id"] == 10
    assert update_calls[0]["values"]["execution_state"]["response"]["choices"][0]["message"]["content"] == "current response"
    assert sent_events[0][1] == "current-session"
    assert sent_events[0][2]["content"] == "current response"
    assert terminal_calls[0]["result_message_id"] == 10


def test_work_message_keys_do_not_depend_on_reusable_work_id():
    first = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:first:1",
        execution_state={"stream_requested": True},
    )
    second = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-2",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="2",
        dedupe_key="foreground-message:second:2",
        created_at=first.created_at + timedelta(days=1),
    )

    round_tripped = SessionReplyWorkItem(
        id=first.id,
        uid=first.uid,
        session_id=first.session_id,
        profile_id=first.profile_id,
        sequence_no=first.sequence_no,
        work_type=first.work_type,
        source_type=first.source_type,
        source_id=first.source_id,
        dedupe_key=first.dedupe_key,
        created_at=first.created_at.replace(tzinfo=None),
    )

    first_key = executor_common_module._result_message_dedupe_key(first)
    second_key = executor_common_module._result_message_dedupe_key(second)

    assert first_key != second_key
    assert first_key == executor_common_module._result_message_dedupe_key(round_tripped)
    assert len(first_key) <= 64
    assert len(second_key) <= 64
    first_event = executor_common_module._event_for_work(first, {"content": "first"})
    second_event = executor_common_module._event_for_work(second, {"content": "second"})

    assert first_event["event_id"] != second_event["event_id"]
    assert first_event["_stream_requested"] is True
    assert "_stream_requested" not in second_event
    assert executor_common_module._event_for_work(first, {"content": "failed"}, error=True)["event_id"] == executor_common_module.build_session_reply_work_event_id(
        first,
        error=True,
    )


def test_event_for_work_normalizes_large_assistant_files_content_without_mutating_response():
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
    )
    first_line = "前" * 900
    second_line = "后" * 900
    text = f"{first_line}\n{second_line}"
    assistant_files_content = json.dumps(
        {
            "type": "assistant_files",
            "text": text,
            "files": [{"path": "old.txt", "content": "untrusted"}],
        },
        ensure_ascii=False,
    )
    response = llm_response_payload(
        assistant_files_content,
        files=[
            {
                "id": "fresh-file-id",
                "name": "fresh.txt",
                "mime_type": "text/plain",
                "size": 10,
                "download_url": "/files/fresh-file-id/download",
                "previewable": True,
            }
        ],
    )

    event = executor_common_module._event_for_work(work, response)

    assert len(text.encode("utf-8")) > 3000
    assert all(len(line.encode("utf-8")) <= 3000 for line in text.split("\n"))
    assert event["content"] == text
    assert event["content"] != assistant_files_content
    assert "\n" in event["content"]
    assert event["files"] == response["files"]
    assert response["choices"][0]["message"]["content"] == assistant_files_content


def test_audit_execution_binding_supports_foreground_and_confirmed_work():
    """审计绑定应覆盖前台、后台、定时和已确认回复工作。"""
    foreground = SimpleNamespace(
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id="user-message",
        execution_state={
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 42,
                "claim_token": "foreground-token",
            }
        },
    )
    confirmed = SimpleNamespace(
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id="43",
        execution_state={"audit_claim_token": "confirmed-token"},
    )
    background = SimpleNamespace(
        work_type=SessionReplyWorkType.BACKGROUND_TASK_RESUME,
        source_id="background-task",
        execution_state={
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 44,
                "claim_token": "background-token",
            }
        },
    )
    scheduled = SimpleNamespace(
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_id="trigger-message",
        execution_state={
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 45,
                "claim_token": "scheduled-token",
            }
        },
    )

    assert executor_audit_module.get_bound_audit_execution(foreground) == (42, "foreground-token")
    assert executor_audit_module.get_bound_audit_execution(confirmed) == (43, "confirmed-token")
    assert executor_audit_module.get_bound_audit_execution(background) == (44, "background-token")
    assert executor_audit_module.get_bound_audit_execution(scheduled) == (45, "scheduled-token")
    assert executor_audit_module.work_has_active_audit_execution(foreground)
    assert executor_audit_module.work_has_active_audit_execution(background)
    assert executor_audit_module.work_has_active_audit_execution(scheduled)
    assert not executor_audit_module.work_has_active_audit_execution(confirmed)


def test_confirmed_work_prefers_active_audit_execution_binding_before_falling_back():
    """确认执行工作应优先恢复后续工具轮的活动审计绑定。"""
    work = SimpleNamespace(
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id="42",
        execution_state={
            "audit_claim_token": "original-token",
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 99,
                "claim_token": "active-token",
            },
        },
    )

    assert executor_audit_module.get_bound_audit_execution(work) == (99, "active-token")

    work.execution_state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY)

    assert executor_audit_module.get_bound_audit_execution(work) == (42, "original-token")


def test_confirmed_execution_rechecks_missing_append_target_before_running(tmp_path):
    target = tmp_path / "append.txt"
    snapshot = executor_audit_module.create_file_integrity_snapshot(target, working_directory=tmp_path).to_dict()
    details = [SimpleNamespace(file_snapshots=[snapshot])]

    assert not executor_audit_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))

    target.write_text("created after audit", encoding="utf-8")

    assert executor_audit_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))


def test_confirmed_execution_reaudits_legacy_file_snapshot_without_presence_state(tmp_path):
    target = tmp_path / "legacy.txt"
    target.write_text("existing", encoding="utf-8")
    legacy_snapshot = {
        "absolute_path": str(target),
        "resolved_path": str(target.resolve()),
        "size": target.stat().st_size,
        "sha256": "legacy-hash",
    }

    assert executor_audit_module._confirmed_file_snapshots_changed(
        [SimpleNamespace(file_snapshots=[legacy_snapshot])],
        working_directory=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_scheduled_reply_persists_and_clears_audit_binding(monkeypatch):
    """辅助回复在工具执行前应持久化活动审计绑定。"""
    work = SimpleNamespace(
        id=12,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="reply-summary:8",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
        execution_state={},
    )
    persisted_states = []

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def update_claimed(db, **kwargs):
        state = kwargs["values"]["execution_state"]
        work.execution_state = state
        persisted_states.append(dict(state))
        return True

    async def generate_reply(db, **kwargs):
        callback = kwargs["audit_execution_binding_callback"]
        await callback({"audit_record_id": 42, "claim_token": "claim-token"})
        await callback(None)
        return InternalMessage(role=MessageRole.ASSISTANT, content="定时总结"), [], []

    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_audit_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_metadata_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)

    ai_msg, _, _, _ = await executor_metadata_module._generate_auxiliary_reply_with_request_metadata(
        object(),
        work=work,
        request_purpose=ProviderUsageType.SCHEDULED_SUMMARY,
        uid=work.uid,
        session_id=work.session_id,
        profile=SimpleNamespace(id=3, uid="user-1"),
        call_context="session_reply_scheduled_summary",
        allow_tools=True,
        initial_trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        initial_fixed_upper_message_id=8,
        tool_workflow=TOOL_WORKFLOW_SCHEDULED_TASK,
        reply_source="scheduled_task",
        final_message_dedupe_key=executor_common_module._result_message_dedupe_key(work),
        audit_execution_binding_callback=partial(
            executor_replies_module._persist_work_audit_execution_binding_callback,
            db=object(),
            work=work,
            worker_id="worker-1",
        ),
    )

    assert ai_msg.content == "定时总结"
    assert persisted_states[0][SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
    }
    assert SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in persisted_states[-1]


@pytest.mark.asyncio
async def test_execute_background_summary_is_auxiliary_and_disables_tools(monkeypatch):
    work = SimpleNamespace(
        id=7,
        sequence_no=1,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="background-task-summary:8",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
        work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        execution_state={},
    )
    task = SimpleNamespace(
        id=8,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        extra={},
        result={"status": "succeeded"},
        tool_call_id="call-1",
        status="succeeded",
        tool_name="safe_tool",
        arguments={},
        error=None,
    )
    captured = {}

    async def get_task(db, task_id):
        assert task_id == 8
        return task

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def generate_auxiliary(db, **kwargs):
        captured.update(kwargs)
        return InternalMessage(role=MessageRole.ASSISTANT, content="background summary"), [], [], {"request_purpose": "background_summary"}

    monkeypatch.setattr(executor_replies_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_replies_module, "_generate_auxiliary_reply_with_request_metadata", generate_auxiliary)
    monkeypatch.setattr(executor_replies_module, "_dispatch_interactive_work", lambda *args, **kwargs: pytest.fail("background summary must not enter interactive tool loop"))

    response = await executor_replies_module._execute_background(object(), work, "worker-1")

    assert response["content"] == "background summary"
    assert response["llm_request_metadata"]["request_purpose"] == "background_summary"
    assert captured["request_purpose"] == ProviderUsageType.BACKGROUND_SUMMARY
    assert captured["allow_tools"] is False
    assert captured["submission_context"] == []


@pytest.mark.asyncio
async def test_agent_work_external_resume_routes_back_to_agent_work_executor(monkeypatch):
    work = SimpleNamespace(
        work_type=SessionReplyWorkType.AGENT_WORK,
        execution_state={"external_resume_pending": True},
    )
    captured = {}

    async def execute_agent_work(db, current_work, worker_id):
        captured["work"] = current_work
        captured["worker_id"] = worker_id
        return {"content": "resumed"}

    monkeypatch.setattr(executor_replies_module, "execute_agent_work", execute_agent_work)

    response = await executor_replies_module._execute_external_resume(object(), work, "worker-1")

    assert response == {"content": "resumed"}
    assert captured == {"work": work, "worker_id": "worker-1"}


@pytest.mark.asyncio
async def test_confirmed_external_resume_preserves_task_scope_and_frozen_context(monkeypatch, checkpoint_store, frozen_context_store):
    frozen_message = InternalMessage(
        id=3,
        role=MessageRole.USER,
        content="scheduled frozen context",
    )
    frozen_context_store[3] = frozen_message.model_dump(mode="json", exclude_none=True)
    work = SimpleNamespace(
        id=7,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        execution_state={
            "decision_message_id": 4,
            "execution_scope": "task",
            "frozen_context_message_ids": [3],
        },
        input_message_ids=[],
        uid="user-1",
        session_id="session-1",
        profile_id=3,
    )
    checkpoint_store[work.id] = {"messages": [frozen_message.model_dump(mode="json", exclude_none=True)]}
    captured = {}
    decision_message = SimpleNamespace(
        id=4,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        role=MessageRole.USER,
        type=MessageType.AUDIT_DECISION,
        content="同意",
        content_format=None,
        attachments=None,
        environment_prompt=None,
        guidance_prompt=None,
    )

    async def get_message(db, message_id):
        assert message_id == 4
        return decision_message

    async def dispatch_interactive_work(db, **kwargs):
        captured.update(kwargs)
        return {"content": "resumed"}

    monkeypatch.setattr(executor_replies_module.message_crud, "get", get_message)
    monkeypatch.setattr(
        executor_replies_module,
        "_dispatch_interactive_work",
        dispatch_interactive_work,
    )

    response = await executor_replies_module._execute_external_resume(
        object(),
        work,
        "worker-1",
    )

    assert response == {"content": "resumed"}
    assert captured["execution_scope"] == "task"
    assert captured["allow_additional_user_messages"] is False
    assert [message.content for message in captured["frozen_context_messages"]] == ["scheduled frozen context"]
