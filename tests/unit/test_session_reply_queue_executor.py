import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.constants import SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY
from app.core.session_reply_queue import executor as executor_module
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, MessageRole, MessageType
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)


@pytest.mark.asyncio
async def test_foreground_executor_resumes_dispatcher_checkpoint(monkeypatch):
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
            "dispatcher_checkpoint": checkpoint,
            "additional_system_prompt": "channel instruction",
        },
    )
    dispatch_kwargs = {}
    checkpoint_updates = []

    class FakeDb:
        async def refresh(self, instance) -> None:
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
        dispatch_kwargs.update(kwargs)
        assert await kwargs["context_summary_work_validity_checker"]() is True
        await kwargs["execution_checkpoint_callback"]({"messages": [{"role": "user", "content": "updated"}]})
        return {"choices": []}

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
        checkpoint_updates.append(kwargs)
        return True

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_queue_manager, "freeze_foreground_input", freeze_foreground_input)
    monkeypatch.setattr(executor_module.session_reply_stream_event_crud, "get_latest_sequence", latest_sequence)
    monkeypatch.setattr(executor_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(
        executor_module.session_reply_work_item_crud,
        "get_active_claims",
        get_active_claims,
    )
    monkeypatch.setattr(
        executor_module.session_crud,
        "get_by_session_id",
        get_session,
    )
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)

    response = await executor_module._execute_foreground(FakeDb(), work, "worker-1")

    assert response == {"choices": []}
    assert dispatch_kwargs["execution_resume_state"] == checkpoint
    assert dispatch_kwargs["message"] == "original"
    assert dispatch_kwargs["additional_system_prompt"] == "channel instruction"
    assert checkpoint_updates[0]["values"]["execution_state"]["stream_requested"] is False
    assert checkpoint_updates[0]["values"]["execution_state"]["dispatcher_checkpoint"]["messages"][0]["content"] == "updated"


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

    monkeypatch.setattr(executor_module.session_reply_queue_manager, "freeze_foreground_input", freeze_foreground_input)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module.session_crud, "update_llm_request_metadata", update_request_metadata)

    response = await executor_module._execute_foreground(FakeDb(), work, "worker-1")

    assert response["choices"][0]["message"]["content"] == "已取消"
    assert response["history"][0]["role"] == MessageRole.ASSISTANT
    assert response["history"][0]["content"] == "已取消"
    expected_request_metadata = {
        **request_metadata,
        "output_tokens": 0,
        "total_output_tokens": 0,
        "work_id": 7,
        "work_sequence_no": 1,
    }
    assert response["llm_request_metadata"] == expected_request_metadata
    assert metadata_updates == [
        {
            "session_id": "session-1",
            "uid": "user-1",
            "metadata": expected_request_metadata,
            "commit": False,
        }
    ]
    assert executor_module._event_for_work(work, response)["llm_request_metadata"] == expected_request_metadata
    assert captured["allow_tools"] is False
    assert captured["additional_system_prompt"] == "channel instruction"
    assert "extra_messages" not in captured
    assert "submission_context" not in captured
    assert captured["final_message_dedupe_key"] == executor_module._result_message_dedupe_key(work)


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
        await kwargs["execution_checkpoint_callback"](
            {
                "messages": [],
                SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: None,
            }
        )
        return {"choices": []}

    async def update_claimed(db, **kwargs):
        state = kwargs["values"]["execution_state"]
        checkpoint_states.append(state)
        work.execution_state = state
        return True

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_queue_manager, "freeze_foreground_input", freeze_foreground_input)
    monkeypatch.setattr(executor_module.session_reply_stream_event_crud, "get_latest_sequence", latest_sequence)
    monkeypatch.setattr(executor_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)

    await executor_module._execute_foreground(FakeDb(), work, "worker-1")

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

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_module, "send_session_event", send_event)

    await executor_module.execute_session_reply_work(work_id=7, worker_id="worker-1")

    assert llm_calls == []
    assert update_calls[0]["values"]["result_message_id"] == 9
    assert update_calls[0]["values"]["execution_state"]["response"]["choices"][0]["message"]["content"] == "saved response"
    assert sent_events[0][2]["event_id"].startswith("session-reply-work:")
    assert sent_events[0][2]["event_id"].endswith(":event")
    assert sent_events[0][2]["event_id"] != "session-reply-work:7:event"
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

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_module, "send_session_event", send_event)

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

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module, "save_message", save_error_message)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_module, "send_session_event", send_event)

    await executor_module.fail_session_reply_work(
        work_id=7,
        worker_id="worker-1",
        error="internal error",
        user_error="user error",
    )

    assert sent_events[0][2]["type"] == "proactive_reply_error"
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
        if llm_calls and dedupe_key == executor_module._result_message_dedupe_key(work):
            return generated_result
        return None

    async def execute_foreground(db, current_work, worker_id: str):
        llm_calls.append((current_work.id, worker_id))
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "current response"},
                    "finish_reason": True,
                }
            ]
        }

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        return True

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        sent_events.append((uid, session_id, event))

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_module, "send_session_event", send_event)

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

    first_key = executor_module._result_message_dedupe_key(first)
    second_key = executor_module._result_message_dedupe_key(second)

    assert first_key != second_key
    assert first_key == executor_module._result_message_dedupe_key(round_tripped)
    assert len(first_key) <= 64
    assert len(second_key) <= 64
    assert executor_module._event_for_work(first, {"content": "first"})["event_id"] != executor_module._event_for_work(second, {"content": "second"})["event_id"]
    assert executor_module._event_for_work(first, {"content": "failed"}, error=True)["event_id"] == executor_module.build_session_reply_work_event_id(first, error=True)


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
        work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        source_id="background-task",
        execution_state={
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 44,
                "claim_token": "background-token",
            }
        },
    )
    scheduled = SimpleNamespace(
        work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
        source_id="trigger-message",
        execution_state={
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 45,
                "claim_token": "scheduled-token",
            }
        },
    )

    assert executor_module.get_bound_audit_execution(foreground) == (42, "foreground-token")
    assert executor_module.get_bound_audit_execution(confirmed) == (43, "confirmed-token")
    assert executor_module.get_bound_audit_execution(background) == (44, "background-token")
    assert executor_module.get_bound_audit_execution(scheduled) == (45, "scheduled-token")
    assert executor_module.work_has_active_audit_execution(foreground)
    assert executor_module.work_has_active_audit_execution(background)
    assert executor_module.work_has_active_audit_execution(scheduled)
    assert not executor_module.work_has_active_audit_execution(confirmed)


def test_confirmed_execution_rechecks_missing_append_target_before_running(tmp_path):
    target = tmp_path / "append.txt"
    snapshot = executor_module.create_file_integrity_snapshot(target, working_directory=tmp_path).to_dict()
    details = [SimpleNamespace(file_snapshots=[snapshot])]

    assert not executor_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))

    target.write_text("created after audit", encoding="utf-8")

    assert executor_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))


def test_confirmed_execution_reaudits_legacy_file_snapshot_without_presence_state(tmp_path):
    target = tmp_path / "legacy.txt"
    target.write_text("existing", encoding="utf-8")
    legacy_snapshot = {
        "absolute_path": str(target),
        "resolved_path": str(target.resolve()),
        "size": target.stat().st_size,
        "sha256": "legacy-hash",
    }

    assert executor_module._confirmed_file_snapshots_changed(
        [SimpleNamespace(file_snapshots=[legacy_snapshot])],
        working_directory=str(tmp_path),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_type",
    [SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.SCHEDULED_TASK_SUMMARY],
)
async def test_background_reply_persists_and_clears_audit_binding(work_type, monkeypatch):
    """后台和定时总结在工具执行前后应持久化并清除审计绑定。"""
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
    task = SimpleNamespace(extra={}, result={"status": "succeeded"}, tool_call_id="call-1", status="succeeded", tool_name="safe_tool", error=None)
    persisted_states = []

    async def get_task(db, task_id):
        return task

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
        return InternalMessage(role=MessageRole.ASSISTANT, content="后台总结"), [], []

    monkeypatch.setattr(executor_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)

    if work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
        response = await executor_module._execute_background(object(), work, "worker-1")
    else:
        response = await executor_module._execute_scheduled(object(), work, "worker-1")

    assert response["content"] == "后台总结"
    assert persisted_states[0][SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
    }
    assert SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in persisted_states[-1]


@pytest.mark.asyncio
async def test_execute_background_persists_llm_request_metadata_with_work_identity(monkeypatch):
    work = SimpleNamespace(
        id=7,
        sequence_no=1,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="reply-summary:8",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
        execution_state={},
    )
    task = SimpleNamespace(extra={}, result={"status": "succeeded"}, tool_call_id="call-1", status="succeeded", tool_name="safe_tool", error=None)
    captured = {}
    metadata_updates = []
    request_metadata = {
        "type": "llm_request_metadata",
        "input_tokens": 123,
        "output_tokens": 7,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
    }

    second_request_metadata = {
        "type": "llm_request_metadata",
        "input_tokens": 456,
        "output_tokens": 11,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
    }

    class FakeDb:
        async def execute(self, *args, **kwargs):
            return None

    async def get_task(db, task_id):
        return task

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def generate_reply(db, **kwargs):
        captured.update(kwargs)
        await kwargs["request_metadata_callback"](request_metadata)
        await kwargs["request_metadata_callback"](second_request_metadata)
        return InternalMessage(role=MessageRole.ASSISTANT, content="后台总结"), [], []

    async def get_session(db, session_id):
        assert session_id == "session-1"
        return SimpleNamespace(
            llm_request_metadata={
                "total_output_tokens": 200,
            }
        )

    async def update_request_metadata(db, **kwargs):
        metadata_updates.append(kwargs)
        return True

    monkeypatch.setattr(executor_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(executor_module.session_crud, "update_llm_request_metadata", update_request_metadata)

    response = await executor_module._execute_background(FakeDb(), work, "worker-1")

    expected_request_metadata = {
        **second_request_metadata,
        "output_tokens": 18,
        "total_output_tokens": 218,
        "work_id": 7,
        "work_sequence_no": 1,
    }
    assert callable(captured["request_metadata_callback"])
    assert response["llm_request_metadata"] == expected_request_metadata
    assert response["llm_request_metadata"]["input_tokens"] == 456
    assert "total_input_tokens" not in response["llm_request_metadata"]
    assert metadata_updates[-1] == {
        "session_id": "session-1",
        "uid": "user-1",
        "metadata": expected_request_metadata,
        "commit": False,
    }
    assert metadata_updates[0]["metadata"] == {
        **request_metadata,
        "output_tokens": 7,
        "total_output_tokens": 207,
        "work_id": 7,
        "work_sequence_no": 1,
    }
    assert len(metadata_updates) == 2


@pytest.mark.asyncio
async def test_execute_scheduled_persists_llm_request_metadata_with_work_identity(monkeypatch):
    work = SimpleNamespace(
        id=8,
        sequence_no=2,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="reply-summary:8",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
        execution_state={},
    )
    captured = {}
    metadata_updates = []
    request_metadata = {
        "type": "llm_request_metadata",
        "input_tokens": 456,
        "output_tokens": 21,
        "context_window_tokens": 8192,
        "max_output_tokens": 1024,
    }

    class FakeDb:
        async def execute(self, *args, **kwargs):
            return None

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def generate_reply(db, **kwargs):
        captured.update(kwargs)
        await kwargs["request_metadata_callback"](request_metadata)
        return InternalMessage(role=MessageRole.ASSISTANT, content="定时总结"), [], []

    async def get_session(db, session_id):
        assert session_id == "session-1"
        return SimpleNamespace(
            llm_request_metadata={
                "total_output_tokens": 200,
            }
        )

    async def update_request_metadata(db, **kwargs):
        metadata_updates.append(kwargs)
        return True

    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(executor_module.session_crud, "update_llm_request_metadata", update_request_metadata)

    response = await executor_module._execute_scheduled(FakeDb(), work, "worker-1")

    expected_request_metadata = {
        **request_metadata,
        "output_tokens": 21,
        "total_output_tokens": 221,
        "work_id": 8,
        "work_sequence_no": 2,
    }
    assert callable(captured["request_metadata_callback"])
    assert response["llm_request_metadata"] == expected_request_metadata
    assert response["llm_request_metadata"]["input_tokens"] == 456
    assert "total_input_tokens" not in response["llm_request_metadata"]
    assert metadata_updates == [
        {
            "session_id": "session-1",
            "uid": "user-1",
            "metadata": expected_request_metadata,
            "commit": False,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "exception", "cancelled"])
async def test_confirmed_binding_failure_marks_new_audit_unknown(monkeypatch, failure):
    work = SimpleNamespace(id=7, source_id="42", execution_state={})
    finish_calls = []
    update_calls = []

    async def update_claimed(db, **kwargs):
        if failure == "false":
            return False
        if failure == "exception":
            raise RuntimeError("lease update failed")
        raise asyncio.CancelledError

    async def mark_execution_unknown(db, **kwargs):
        update_calls.append(kwargs)
        return False

    async def finish_execution_round(db, **kwargs):
        finish_calls.append(kwargs)
        return True

    async def update_confirmation(db, *, audit_record_id):
        assert audit_record_id == 99

    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round", finish_execution_round)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)

    expected_exception = asyncio.CancelledError if failure == "cancelled" else RuntimeError
    with pytest.raises(expected_exception) as exc_info:
        await executor_module._persist_confirmed_work_audit_execution_binding(
            object(),
            work=work,
            worker_id="worker-1",
            audit_record_id=99,
            claim_token="new-token",
        )

    if failure == "exception":
        assert str(exc_info.value) == "lease update failed"
    assert update_calls[0]["audit_record_id"] == 99
    assert update_calls[0]["claim_token"] == "new-token"
    assert finish_calls[0]["audit_record_id"] == 99
    assert finish_calls[0]["claim_token"] == "new-token"
    assert finish_calls[0]["status"] == AuditRecordStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
async def test_confirmed_binding_cleanup_does_not_mask_update_exception(monkeypatch):
    async def update_claimed(db, **kwargs):
        raise RuntimeError("original update failure")

    async def mark_execution_unknown(db, **kwargs):
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)

    with pytest.raises(RuntimeError, match="original update failure"):
        await executor_module._persist_confirmed_work_audit_execution_binding(
            object(),
            work=SimpleNamespace(id=7, source_id="42", execution_state={}),
            worker_id="worker-1",
            audit_record_id=99,
            claim_token="new-token",
        )


@pytest.mark.asyncio
async def test_mark_work_audit_execution_unknown_closes_bound_round_without_attempt(monkeypatch):
    work = SimpleNamespace(
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id="99",
        execution_state={"audit_claim_token": "new-token"},
    )
    finish_calls = []
    confirmation_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id):
        assert work_id == 7
        return work

    async def list_background_tasks(db, audit_record_id):
        assert audit_record_id == 99
        return []

    async def mark_execution_unknown(db, **kwargs):
        assert kwargs["audit_record_id"] == 99
        assert kwargs["claim_token"] == "new-token"
        return False

    async def finish_execution_round(db, **kwargs):
        finish_calls.append(kwargs)
        return True

    async def update_confirmation(db, *, audit_record_id):
        confirmation_calls.append(audit_record_id)

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.background_task_crud, "list_by_audit_record", list_background_tasks)
    monkeypatch.setattr(executor_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round", finish_execution_round)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)

    await executor_module.mark_work_audit_execution_unknown(7, "worker-1", "interrupted")

    assert finish_calls == [
        {
            "audit_record_id": 99,
            "claim_token": "new-token",
            "status": AuditRecordStatus.EXECUTION_UNKNOWN,
            "error_reason": "interrupted",
        }
    ]
    assert confirmation_calls == [99]


@pytest.mark.asyncio
async def test_confirmed_file_reaudit_persists_new_audit_binding(monkeypatch):
    source_internal = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="original-1", name="safe_tool", arguments={})],
    )
    source_message = SimpleNamespace(
        id=1,
        uid="user-1",
        session_id="session-1",
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps(source_internal.model_dump(mode="json"), ensure_ascii=False),
    )
    original_record = SimpleNamespace(
        status=AuditRecordStatus.EXECUTING,
        execution_claim_token="old-token",
        source_assistant_message_id=1,
        working_directory=".",
        operator_username="tester",
        source="confirmed_tool_execution",
        round_arguments_hash="a" * 64,
    )
    new_record = SimpleNamespace(operator_username="tester")
    details = [SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64)]
    work = SimpleNamespace(
        id=7,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id="42",
        execution_state={"audit_claim_token": "old-token", "decision_message_id": 4},
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="confirmed-audit:42",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
    )
    update_calls = []
    replaced_results = []
    pending_message = SimpleNamespace(id=2, content="pending")

    class FakeDb:
        async def get(self, model, item_id):
            assert item_id == 1
            return source_message

    async def get_record(db, audit_record_id):
        assert audit_record_id == 42
        return original_record

    async def list_details(db, audit_record_id):
        return details

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def validate_profile(db, profile):
        return SimpleNamespace(security=SimpleNamespace(audit_channel_id=1, audit_model_id="audit-model"))

    async def audit_round(*args, **kwargs):
        return SimpleNamespace(may_execute=True, audit_record_id=99)

    async def claim_passed(db, *, audit_record_id):
        assert audit_record_id == 99
        return new_record, "new-token"

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        work.source_id = kwargs["values"]["source_id"]
        work.execution_state = kwargs["values"]["execution_state"]
        return True

    async def get_tools(db, profile):
        return [], None

    async def create_execution(db, **kwargs):
        return None

    async def finish_round(db, **kwargs):
        return True

    async def generate_reply(db, **kwargs):
        return InternalMessage(role=MessageRole.ASSISTANT, content="重审完成"), [], []

    async def cancel_reaudit(*args, **kwargs):
        return True

    async def update_confirmation(*args, **kwargs):
        return None

    async def get_pending(db, **kwargs):
        return {"original-1": pending_message}

    async def replace_result(db, **kwargs):
        replaced_results.append(kwargs)
        return kwargs["content"]

    monkeypatch.setattr(executor_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: True)
    monkeypatch.setattr(executor_module.audit_crud, "cancel_execution_for_file_reaudit", cancel_reaudit)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_module, "audit_tool_round", audit_round)
    monkeypatch.setattr(executor_module.audit_crud, "claim_passed_for_execution", claim_passed)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round", finish_round)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(executor_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("tool execution must not start"))

    await executor_module._execute_confirmed_tools(FakeDb(), work, "worker-1")

    assert update_calls[0]["work_id"] == 7
    assert update_calls[0]["worker_id"] == "worker-1"
    assert update_calls[0]["values"]["source_id"] == "99"
    assert update_calls[0]["values"]["execution_state"]["audit_claim_token"] == "new-token"
    assert executor_module.get_bound_audit_execution(work) == (99, "new-token")
    assert replaced_results[0]["original_tool_call_id"] == "original-1"


@pytest.mark.asyncio
async def test_confirmed_file_reaudit_pending_replaces_original_result_without_saving_tool_chain(monkeypatch):
    source_internal = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="original-1", name="safe_tool", arguments={})],
    )
    source_message = SimpleNamespace(
        id=1,
        uid="user-1",
        session_id="session-1",
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps(source_internal.model_dump(mode="json"), ensure_ascii=False),
    )
    record = SimpleNamespace(
        status=AuditRecordStatus.EXECUTING,
        execution_claim_token="old-token",
        source_assistant_message_id=1,
        working_directory=".",
        operator_username="tester",
        source="confirmed_tool_execution",
        round_arguments_hash="a" * 64,
    )
    details = [SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64)]
    work = SimpleNamespace(
        id=7,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id="42",
        execution_state={"audit_claim_token": "old-token", "decision_message_id": 4},
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="confirmed-audit:42",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
    )
    pending_message = SimpleNamespace(id=2, content="pending")
    replaced_results = []
    saved_messages = []
    memory_chains = []

    class FakeDb:
        async def get(self, model, item_id):
            return source_message

    async def get_record(db, audit_record_id):
        return record

    async def list_details(db, audit_record_id):
        return details

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def validate_profile(db, profile):
        return SimpleNamespace(security=SimpleNamespace(audit_channel_id=1, audit_model_id="audit-model"))

    async def get_pending(db, **kwargs):
        return {"original-1": pending_message}

    async def cancel_reaudit(db, **kwargs):
        return True

    async def audit_round(*args, **kwargs):
        return SimpleNamespace(
            may_execute=False,
            audit_record_id=99,
            tool_results=(
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="original-1",
                    content=json.dumps({"status": "pending", "reason": "changed"}),
                ),
            ),
            confirmation_payload={"audit_record_id": 99, "status": "pending"},
        )

    async def replace_result(db, **kwargs):
        replaced_results.append(kwargs)
        return kwargs["content"]

    async def save_confirmation(*args, **kwargs):
        saved_messages.append((args, kwargs))
        return None

    async def update_confirmation(db, *, audit_record_id):
        return None

    def dump_history(messages):
        memory_chains.append([message.model_copy(deep=True) for message in messages])
        return []

    monkeypatch.setattr(executor_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: True)
    monkeypatch.setattr(executor_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_module.audit_crud, "cancel_execution_for_file_reaudit", cancel_reaudit)
    monkeypatch.setattr(executor_module, "audit_tool_round", audit_round)
    monkeypatch.setattr(executor_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_module, "save_message", save_confirmation)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_module, "dump_background_proactive_history", dump_history)
    monkeypatch.setattr(executor_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("pending re-audit must not execute tools"))

    response = await executor_module._execute_confirmed_tools(FakeDb(), work)

    assert json.loads(response["content"]) == {"audit_record_id": 99, "status": "pending"}
    assert len(replaced_results) == 1
    assert replaced_results[0]["pending_message"].id == 2
    assert replaced_results[0]["original_tool_call_id"] == "original-1"
    assert len(saved_messages) == 1
    assert saved_messages[0][0][4] == MessageType.AUDIT_CONFIRMATION
    assert any(message.role == MessageRole.TOOL and message.tool_call_id == "original-1" for message in memory_chains[0])


@pytest.mark.asyncio
async def test_confirmed_execution_replaces_original_results_and_keeps_fresh_memory_chain(monkeypatch):
    source_internal = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(id="original-1", name="safe_tool", arguments={"value": 1}),
            InternalToolCall(id="original-2", name="safe_tool", arguments={"value": 2}),
        ],
    )
    source_message = SimpleNamespace(
        id=1,
        uid="user-1",
        session_id="session-1",
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps(source_internal.model_dump(mode="json"), ensure_ascii=False),
    )
    pending_messages = {
        "original-1": SimpleNamespace(id=2, content="pending-1"),
        "original-2": SimpleNamespace(id=3, content="pending-2"),
    }
    record = SimpleNamespace(
        status=AuditRecordStatus.EXECUTING,
        execution_claim_token="claim-token",
        source_assistant_message_id=1,
        working_directory=".",
        operator_username="tester",
        round_arguments_hash="a" * 64,
    )
    details = [
        SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64),
        SimpleNamespace(id=12, original_tool_call_id="original-2", turn_index=1, tool_name="safe_tool", arguments_hash="c" * 64),
    ]
    work = SimpleNamespace(
        source_id="42",
        execution_state={"audit_claim_token": "claim-token", "decision_message_id": 4},
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="confirmed-audit:42",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
    )
    created_attempts = []
    process_calls = []
    replaced_results = []

    class FakeDb:
        async def get(self, model, item_id):
            assert item_id == 1
            return source_message

    async def get_record(db, audit_record_id):
        return record

    async def list_details(db, audit_record_id):
        return details

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def validate_profile(db, profile):
        return SimpleNamespace()

    async def get_tools(db, profile):
        return [], None

    async def create_execution(db, **kwargs):
        created_attempts.append(kwargs)
        return SimpleNamespace(id=100 + len(created_attempts))

    async def process_tool(tool_call, db, profile, cfg, messages, username, session_id, turn, uid, **kwargs):
        process_calls.append((tool_call.id, [message.model_copy(deep=True) for message in messages]))
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=json.dumps({"status": "success", "value": tool_call.arguments["value"]}),
        )

    async def replace_result(db, **kwargs):
        replaced_results.append(kwargs)
        return kwargs["content"]

    async def get_pending(db, **kwargs):
        return pending_messages

    async def finish_attempt(db, **kwargs):
        return True

    async def finish_round_if_complete(db, **kwargs):
        return AuditRecordStatus.SUCCEEDED

    async def update_confirmation(db, *, audit_record_id):
        return None

    async def generate_reply(db, **kwargs):
        return InternalMessage(role=MessageRole.ASSISTANT, content="完成"), [], []

    monkeypatch.setattr(executor_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_module, "create_file_integrity_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(executor_module, "process_single_tool", process_tool)
    monkeypatch.setattr(executor_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    response = await executor_module._execute_confirmed_tools(FakeDb(), work, "worker-1")

    assert response["content"] == "完成"
    assert len(created_attempts) == 2
    fresh_ids = [call["new_tool_call_id"] for call in created_attempts]
    assert len(set(fresh_ids)) == 2
    assert all(fresh_id.startswith("call_") for fresh_id in fresh_ids)
    assert [call_id for call_id, _messages in process_calls] == fresh_ids
    assert [item["pending_message"].id for item in replaced_results] == [2, 3]
    assert [item["original_tool_call_id"] for item in replaced_results] == ["original-1", "original-2"]
    assert [message.role for message in process_calls[1][1]] == [MessageRole.ASSISTANT, MessageRole.TOOL]
    assert process_calls[1][1][0].tool_calls[0].id == fresh_ids[0]
    assert process_calls[1][1][1].tool_call_id == fresh_ids[0]


@pytest.mark.asyncio
async def test_confirmed_execution_does_not_run_without_complete_pending_results(monkeypatch):
    source_internal = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="original-1", name="safe_tool", arguments={})],
    )
    source_message = SimpleNamespace(
        id=1,
        uid="user-1",
        session_id="session-1",
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps(source_internal.model_dump(mode="json"), ensure_ascii=False),
    )
    record = SimpleNamespace(
        status=AuditRecordStatus.EXECUTING,
        execution_claim_token="claim-token",
        source_assistant_message_id=1,
        working_directory=".",
        operator_username="tester",
        round_arguments_hash="a" * 64,
    )
    work = SimpleNamespace(
        source_id="42",
        execution_state={"audit_claim_token": "claim-token", "decision_message_id": 4},
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        created_at=datetime(2026, 7, 20, 0, 0, 0),
        dedupe_key="confirmed-audit:42",
    )
    invalidated = []

    class FakeDb:
        async def get(self, model, item_id):
            return source_message

    async def get_record(db, audit_record_id):
        return record

    async def list_details(db, audit_record_id):
        return [SimpleNamespace(original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64)]

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def mark_invalid(db, **kwargs):
        invalidated.append(kwargs)
        return True

    async def generate_reply(db, **kwargs):
        return InternalMessage(role=MessageRole.ASSISTANT, content="原调用记录无效"), [], []

    async def validate_profile(db, profile):
        return SimpleNamespace()

    async def get_pending(db, **kwargs):
        return None

    async def update_confirmation(db, *, audit_record_id):
        return None

    monkeypatch.setattr(executor_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_module.audit_crud, "mark_source_message_invalid", mark_invalid)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("invalid pending results must not execute tools"))
    monkeypatch.setattr(executor_module.audit_crud, "create_execution_attempt", lambda *args, **kwargs: pytest.fail("invalid pending results must not create executions"))

    response = await executor_module._execute_confirmed_tools(FakeDb(), work)

    assert response["content"] == "原调用记录无效"
    assert invalidated[0]["audit_record_id"] == 42


@pytest.mark.asyncio
async def test_confirmed_execution_precheck_failure_closes_round_as_failed(monkeypatch):
    source_internal = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[InternalToolCall(id="original-1", name="safe_tool", arguments={})])
    source_message = SimpleNamespace(
        id=1,
        uid="user-1",
        session_id="session-1",
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps(source_internal.model_dump(mode="json"), ensure_ascii=False),
    )
    record = SimpleNamespace(
        status=AuditRecordStatus.EXECUTING,
        execution_claim_token="claim-token",
        source_assistant_message_id=1,
        working_directory=".",
        operator_username="tester",
        round_arguments_hash="a" * 64,
    )
    details = [SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64)]
    work = SimpleNamespace(
        source_id="42",
        execution_state={"audit_claim_token": "claim-token"},
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="confirmed-audit:42",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
    )
    cancelled_attempts = []
    round_calls = []
    complete_calls = []

    class FakeDb:
        async def get(self, model, item_id):
            assert item_id == 1
            return source_message

    async def create_execution(db, **kwargs):
        return SimpleNamespace(id=101)

    async def finish_attempt(db, **kwargs):
        cancelled_attempts.append(kwargs)
        return True

    async def finish_round(db, **kwargs):
        round_calls.append(kwargs)
        record.status = kwargs["status"]
        return True

    async def finish_round_if_complete(db, **kwargs):
        complete_calls.append(kwargs)
        raise AssertionError("precheck failure must close the round as failed directly")

    async def generate_reply(db, **kwargs):
        return InternalMessage(role=MessageRole.ASSISTANT, content="工具预检失败"), [], []

    def prevalidate(calls, *args, **kwargs):
        return {calls[0].id: json.dumps({"status": "failed", "tool_name": calls[0].name, "error": "bad args"})}

    async def get_record(db, audit_record_id):
        return record

    async def list_details(db, audit_record_id):
        return details

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def validate_profile(db, profile):
        return SimpleNamespace()

    async def get_tools(db, profile):
        return [], None

    async def update_confirmation(*args, **kwargs):
        return None

    async def replace_result(db, **kwargs):
        return kwargs["content"]

    async def get_pending(db, **kwargs):
        return {"original-1": SimpleNamespace(id=2, content="pending")}

    monkeypatch.setattr(executor_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round", finish_round)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_module, "prevalidate_tool_round", prevalidate)
    monkeypatch.setattr(executor_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("tool execution must not start"))

    await executor_module._execute_confirmed_tools(FakeDb(), work)

    assert record.status == AuditRecordStatus.FAILED
    assert len(cancelled_attempts) == 1
    assert cancelled_attempts[0]["status"] == AuditExecutionStatus.CANCELLED
    assert len(round_calls) == 1
    assert round_calls[0]["status"] == AuditRecordStatus.FAILED
    assert complete_calls == []


@pytest.mark.asyncio
async def test_confirmed_execution_closes_round_when_execution_attempt_creation_is_partial(monkeypatch):
    """部分执行记录创建失败时必须将审计整轮关闭为失败。"""
    source_internal = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(id="original-1", name="safe_tool", arguments={}),
            InternalToolCall(id="original-2", name="safe_tool", arguments={}),
        ],
    )
    source_message = SimpleNamespace(
        id=1,
        uid="user-1",
        session_id="session-1",
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps(source_internal.model_dump(mode="json"), ensure_ascii=False),
    )
    record = SimpleNamespace(
        status=AuditRecordStatus.EXECUTING,
        execution_claim_token="claim-token",
        source_assistant_message_id=1,
        working_directory=".",
        operator_username="tester",
        round_arguments_hash="a" * 64,
    )
    details = [
        SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64),
        SimpleNamespace(id=12, original_tool_call_id="original-2", turn_index=1, tool_name="safe_tool", arguments_hash="c" * 64),
    ]
    work = SimpleNamespace(
        source_id="42",
        execution_state={"audit_claim_token": "claim-token"},
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="confirmed-audit:42",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
    )
    cancelled_attempts = []
    round_calls = []
    complete_calls = []
    created_attempts = [SimpleNamespace(id=101), None]

    class FakeDb:
        async def get(self, model, item_id):
            assert item_id == 1
            return source_message

    async def get_record(db, audit_record_id):
        return record

    async def list_details(db, audit_record_id):
        return details

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def validate_profile(db, profile):
        return SimpleNamespace()

    async def get_tools(db, profile):
        return [], None

    async def create_execution(db, **kwargs):
        return created_attempts.pop(0)

    async def finish_attempt(db, **kwargs):
        cancelled_attempts.append(kwargs)
        return True

    async def finish_round(db, **kwargs):
        round_calls.append(kwargs)
        record.status = kwargs["status"]
        return True

    async def finish_round_if_complete(db, **kwargs):
        complete_calls.append(kwargs)
        raise AssertionError("partial attempt creation must not use normal round completion")

    async def generate_reply(db, **kwargs):
        return InternalMessage(role=MessageRole.ASSISTANT, content="工具执行未完成"), [], []

    async def update_confirmation(db, *, audit_record_id):
        return None

    async def replace_result(db, **kwargs):
        return kwargs["content"]

    async def get_pending(db, **kwargs):
        return {
            "original-1": SimpleNamespace(id=2, content="pending-1"),
            "original-2": SimpleNamespace(id=3, content="pending-2"),
        }

    def verify_round(**kwargs):
        return True

    monkeypatch.setattr(executor_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round", finish_round)
    monkeypatch.setattr(executor_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_module, "verify_persisted_tool_round", verify_round)
    monkeypatch.setattr(executor_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(executor_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("tool execution must not start"))

    await executor_module._execute_confirmed_tools(FakeDb(), work)

    assert record.status == AuditRecordStatus.FAILED
    assert len(cancelled_attempts) == 1
    assert cancelled_attempts[0]["status"] == AuditExecutionStatus.CANCELLED
    assert len(round_calls) == 1
    assert round_calls[0]["status"] == AuditRecordStatus.FAILED
    assert complete_calls == []
