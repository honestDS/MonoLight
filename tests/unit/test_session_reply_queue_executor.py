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
        execution_state={"stream_requested": False, "dispatcher_checkpoint": checkpoint},
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
    assert checkpoint_updates[0]["values"]["execution_state"]["stream_requested"] is False
    assert checkpoint_updates[0]["values"]["execution_state"]["dispatcher_checkpoint"]["messages"][0]["content"] == "updated"


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

    assert llm_calls == []
    assert update_calls[0]["values"]["result_message_id"] == 9
    assert update_calls[0]["values"]["execution_state"]["response"]["choices"][0]["message"]["content"] == "saved response"
    assert sent_events[0][2]["event_id"].startswith("session-reply-work:")
    assert sent_events[0][2]["event_id"].endswith(":event")
    assert sent_events[0][2]["event_id"] != "session-reply-work:7:event"
    assert terminal_calls[0]["status"] == SessionReplyWorkStatus.SUCCEEDED
    assert terminal_calls[0]["result_message_id"] == 9


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

    async def save_assistant(*args, **kwargs):
        return None

    async def save_tool_response(*args, **kwargs):
        return None

    async def generate_reply(db, **kwargs):
        return InternalMessage(role=MessageRole.ASSISTANT, content="工具执行未完成"), [], []

    async def update_confirmation(db, *, audit_record_id):
        return None

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
    monkeypatch.setattr(executor_module, "save_assistant_message", save_assistant)
    monkeypatch.setattr(executor_module, "save_tool_response", save_tool_response)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)
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
