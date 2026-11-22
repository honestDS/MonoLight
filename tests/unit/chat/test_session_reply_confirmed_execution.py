import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.confirmation.service import ToolRoundResult
from app.core.session_reply_queue import (
    executor_audit as executor_audit_module,
)
from app.core.session_reply_queue import (
    executor_confirmed as executor_confirmed_module,
)
from app.core.session_reply_queue import (
    executor_metadata as executor_metadata_module,
)
from app.core.session_reply_queue import (
    executor_replies as executor_replies_module,
)
from app.core.terminal.schemas import (
    ShellInteractiveHandoffResult,
    TerminalOutputBufferState,
    TerminalSessionStatus,
)
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkType
from tests.unit.chat import test_session_reply_queue_executor as executor_tests
from tests.unit.chat.test_session_reply_queue_executor import llm_response_payload
from tests.unit.response_test_support import (
    audit_confirmation_payload,
    pending_audit_tool_result,
)

_isolate_confirmed_tool_runtime_dependencies = executor_tests._isolate_confirmed_tool_runtime_dependencies
checkpoint_store = executor_tests.checkpoint_store
frozen_context_store = executor_tests.frozen_context_store


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "exception", "cancelled"])
async def test_confirmed_binding_failure_marks_new_audit_unknown(monkeypatch, failure):
    work = SimpleNamespace(id=7, source_id="42", execution_state={})
    finish_calls = []
    update_calls = []

    class FakeSession:
        async def commit(self):
            return None

    async def update_claimed(db, **kwargs):
        if failure == "false":
            return False
        if failure == "exception":
            raise RuntimeError("lease update failed")
        raise asyncio.CancelledError

    async def mark_execution_unknown(db, *, commit: bool, **kwargs):
        assert commit is True
        update_calls.append({**kwargs, "commit": commit})
        return False

    async def finish_execution_round(db, *, commit: bool, **kwargs):
        assert commit is True
        finish_calls.append({**kwargs, "commit": commit})
        return True

    async def finish_execution_round_if_complete(db, *, commit: bool, **kwargs):
        assert commit is True
        return None

    async def update_confirmation_tool_results(db, **kwargs):
        return 0

    async def update_confirmation(db, *, audit_record_id, commit: bool):
        assert audit_record_id == 99
        assert commit is True

    monkeypatch.setattr(executor_audit_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_audit_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)
    monkeypatch.setattr(executor_audit_module.audit_crud, "finish_execution_round", finish_execution_round)
    monkeypatch.setattr(executor_audit_module.audit_crud, "finish_execution_round_if_complete", finish_execution_round_if_complete)
    monkeypatch.setattr(executor_audit_module, "_update_confirmation_tool_results", update_confirmation_tool_results)
    monkeypatch.setattr(executor_audit_module, "update_confirmation_message_status", update_confirmation)

    expected_exception = asyncio.CancelledError if failure == "cancelled" else RuntimeError
    with pytest.raises(expected_exception) as exc_info:
        await executor_audit_module._persist_confirmed_work_audit_execution_binding(
            FakeSession(),
            work=work,
            worker_id="worker-1",
            audit_record_id=99,
            claim_token="new-token",
        )

    if failure == "exception":
        assert str(exc_info.value) == "lease update failed"
    assert update_calls[0]["audit_record_id"] == 99
    assert update_calls[0]["claim_token"] == "new-token"
    assert update_calls[0]["commit"] is True
    assert finish_calls[0]["audit_record_id"] == 99
    assert finish_calls[0]["claim_token"] == "new-token"
    assert finish_calls[0]["status"] == AuditRecordStatus.EXECUTION_UNKNOWN
    assert finish_calls[0]["commit"] is True


@pytest.mark.asyncio
async def test_confirmed_binding_cleanup_does_not_mask_update_exception(monkeypatch):
    async def update_claimed(db, **kwargs):
        raise RuntimeError("original update failure")

    async def mark_execution_unknown(db, *, commit: bool, **kwargs):
        assert commit is True
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(executor_audit_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_audit_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)

    with pytest.raises(RuntimeError, match="original update failure"):
        await executor_audit_module._persist_confirmed_work_audit_execution_binding(
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
        async def commit(self):
            return None

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

    async def list_active_audit_execution_record_ids(*args, **kwargs):
        return set()

    async def mark_execution_unknown(db, *, commit: bool, **kwargs):
        assert commit is True
        assert kwargs["audit_record_id"] == 99
        assert kwargs["claim_token"] == "new-token"
        return False

    async def finish_execution_round(db, *, commit: bool, **kwargs):
        assert commit is True
        finish_calls.append({**kwargs, "commit": commit})
        return True

    async def finish_execution_round_if_complete(db, *, commit: bool, **kwargs):
        assert commit is True
        return None

    async def update_confirmation_tool_results(db, **kwargs):
        return 0

    async def update_confirmation(db, *, audit_record_id, commit: bool):
        assert commit is True
        confirmation_calls.append(audit_record_id)

    monkeypatch.setattr(executor_audit_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_audit_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_audit_module.background_task_crud, "list_by_audit_record", list_background_tasks)
    monkeypatch.setattr(
        executor_audit_module.terminal_session_crud,
        "list_active_audit_execution_record_ids",
        list_active_audit_execution_record_ids,
    )
    monkeypatch.setattr(executor_audit_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)
    monkeypatch.setattr(executor_audit_module.audit_crud, "finish_execution_round", finish_execution_round)
    monkeypatch.setattr(executor_audit_module.audit_crud, "finish_execution_round_if_complete", finish_execution_round_if_complete)
    monkeypatch.setattr(executor_audit_module, "_update_confirmation_tool_results", update_confirmation_tool_results)
    monkeypatch.setattr(executor_audit_module, "update_confirmation_message_status", update_confirmation)

    await executor_audit_module.mark_work_audit_execution_unknown(7, "worker-1", "interrupted")

    assert finish_calls == [
        {
            "audit_record_id": 99,
            "claim_token": "new-token",
            "status": AuditRecordStatus.EXECUTION_UNKNOWN,
            "error_reason": "interrupted",
            "commit": True,
        }
    ]
    assert confirmation_calls == [99]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("background_execution_ids", "terminal_execution_ids", "expected_excluded_ids"),
    [
        ([101], {202}, {101, 202}),
        ([], {202}, {202}),
    ],
)
async def test_mark_work_audit_execution_unknown_preserves_active_handoffs(
    monkeypatch,
    background_execution_ids,
    terminal_execution_ids,
    expected_excluded_ids,
):
    work = SimpleNamespace(
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id="99",
        execution_state={"audit_claim_token": "new-token"},
    )
    unknown_except_calls = []
    mark_execution_unknown_calls = []
    reliable_unknown_calls = []
    finish_round_calls = []

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
        return [
            SimpleNamespace(
                status=executor_audit_module.BackgroundTaskStatus.RUNNING,
                audit_execution_record_id=execution_id,
            )
            for execution_id in background_execution_ids
        ]

    async def list_active_audit_execution_record_ids(*args, **kwargs):
        return terminal_execution_ids

    async def mark_running_executions_unknown_except(db, **kwargs):
        unknown_except_calls.append(kwargs)
        return True

    async def mark_execution_unknown(db, *, commit: bool, **kwargs):
        assert commit is True
        mark_execution_unknown_calls.append(kwargs)
        return True

    async def mark_unknown_reliably(db, **kwargs):
        reliable_unknown_calls.append(kwargs)
        return True

    async def finish_execution_round(db, *, commit: bool, **kwargs):
        assert commit is True
        finish_round_calls.append(kwargs)
        return True

    monkeypatch.setattr(executor_audit_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_audit_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_audit_module.background_task_crud, "list_by_audit_record", list_background_tasks)
    monkeypatch.setattr(
        executor_audit_module.terminal_session_crud,
        "list_active_audit_execution_record_ids",
        list_active_audit_execution_record_ids,
    )
    monkeypatch.setattr(
        executor_audit_module.audit_crud,
        "mark_running_executions_unknown_except",
        mark_running_executions_unknown_except,
    )
    monkeypatch.setattr(executor_audit_module, "_mark_audit_execution_unknown_reliably", mark_unknown_reliably)
    monkeypatch.setattr(executor_audit_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)
    monkeypatch.setattr(executor_audit_module.audit_crud, "finish_execution_round", finish_execution_round)

    await executor_audit_module.mark_work_audit_execution_unknown(7, "worker-1", "interrupted")

    assert unknown_except_calls == [
        {
            "audit_record_id": 99,
            "claim_token": "new-token",
            "excluded_execution_record_ids": expected_excluded_ids,
            "error_reason": "interrupted",
        }
    ]
    assert mark_execution_unknown_calls == []
    assert reliable_unknown_calls == []
    assert finish_round_calls == []


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
        decision_raw_message="同意",
        decision_message_id=4,
        execution_scope="foreground",
    )
    new_record = SimpleNamespace(operator_username="tester", execution_scope="foreground")
    details = [SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64, decision_source="model_audit", action_request=None)]
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
        def __init__(self):
            self.info = {}

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
        return ToolRoundResult(
            audit_record_id=99,
            status=AuditRecordStatus.PASSED,
            tool_results=(),
        )

    async def claim_passed(db, *, audit_record_id):
        assert audit_record_id == 99
        return new_record, "new-token"

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        work.source_id = kwargs["values"]["source_id"]
        work.execution_state = kwargs["values"]["execution_state"]
        return True

    async def get_tools(db, profile, **kwargs):
        return [], None

    async def create_execution(db, **kwargs):
        return None

    async def finish_round(db, **kwargs):
        return True

    async def dispatch_interactive_work(*args, **kwargs):
        return llm_response_payload("重审完成", history=[], files=[])

    async def cancel_reaudit(*args, **kwargs):
        return True

    async def update_confirmation(*args, **kwargs):
        return None

    async def get_pending(db, **kwargs):
        return {"original-1": pending_message}

    async def replace_result(db, **kwargs):
        replaced_results.append(kwargs)
        return kwargs["content"]

    monkeypatch.setattr(executor_confirmed_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_confirmed_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_confirmed_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_confirmed_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: True)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "cancel_execution_for_file_reaudit", cancel_reaudit)
    monkeypatch.setattr(executor_confirmed_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_confirmed_module, "prepare_tool_round", audit_round)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "claim_passed_for_execution", claim_passed)
    monkeypatch.setattr(executor_audit_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_confirmed_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_round", finish_round)
    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", dispatch_interactive_work)
    monkeypatch.setattr(executor_confirmed_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_confirmed_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_confirmed_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_confirmed_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("tool execution must not start"))

    await executor_confirmed_module._execute_confirmed_tools(FakeDb(), work, "worker-1")

    assert update_calls[0]["work_id"] == 7
    assert update_calls[0]["worker_id"] == "worker-1"
    assert update_calls[0]["values"]["source_id"] == "99"
    assert update_calls[0]["values"]["execution_state"]["audit_claim_token"] == "new-token"
    assert executor_audit_module.get_bound_audit_execution(work) == (99, "new-token")
    assert replaced_results[0]["original_tool_call_id"] == "original-1"


@pytest.mark.asyncio
async def test_confirmed_file_reaudit_pending_replaces_original_result_without_saving_tool_chain(
    monkeypatch,
    checkpoint_store,
    frozen_context_store,
):
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
        decision_raw_message="同意",
        decision_message_id=4,
        execution_scope="task",
    )
    details = [SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64, decision_source="model_audit", action_request=None)]
    work = SimpleNamespace(
        id=7,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id="42",
        execution_state={
            "audit_claim_token": "old-token",
            "decision_message_id": 4,
            "frozen_context_message_ids": [1, 2],
        },
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="confirmed-audit:42",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
    )
    checkpoint_store[work.id] = {
        "messages": [
            InternalMessage(
                id=1,
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="original-1", name="safe_tool", arguments={})],
            ).model_dump(mode="json", exclude_none=True),
            InternalMessage(
                id=2,
                role=MessageRole.TOOL,
                tool_call_id="original-1",
                content="old-pending-result",
            ).model_dump(mode="json", exclude_none=True),
        ]
    }
    frozen_context_store.update({message["id"]: message for message in checkpoint_store[work.id]["messages"]})
    pending_message = Message(
        id=2,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="original-1",
            content="old-pending-result",
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    replaced_results = []
    saved_messages = []
    notified_audit_record_ids = []
    memory_chains = []
    event_order = []
    confirmation_payload = audit_confirmation_payload(99)

    class FakeDb:
        async def get(self, model, item_id):
            return source_message

        def add(self, instance):
            return None

        async def flush(self):
            return None

        async def commit(self):
            event_order.append("commit")

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
        return ToolRoundResult(
            audit_record_id=99,
            status=AuditRecordStatus.PENDING,
            tool_results=(pending_audit_tool_result("original-1"),),
            confirmation_payload=confirmation_payload,
        )

    async def replace_result(db, **kwargs):
        event_order.append("replace")
        replaced_results.append(kwargs)
        pending_message.content = InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=kwargs["original_tool_call_id"],
            content=kwargs["content"],
        ).model_dump_json(exclude_none=True)
        return kwargs["content"]

    async def save_confirmation(*args, **kwargs):
        saved_messages.append((args, kwargs))
        return None

    async def update_confirmation(db, *, audit_record_id):
        return False

    async def notify_confirmation_results(db, *, audit_record_id):
        event_order.append("notify")
        notified_audit_record_ids.append(audit_record_id)

    def dump_history(messages):
        memory_chains.append([message.model_copy(deep=True) for message in messages])
        return []

    monkeypatch.setattr(executor_confirmed_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_confirmed_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_confirmed_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_confirmed_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_confirmed_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: True)
    monkeypatch.setattr(executor_confirmed_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "cancel_execution_for_file_reaudit", cancel_reaudit)
    monkeypatch.setattr(executor_confirmed_module, "prepare_tool_round", audit_round)
    monkeypatch.setattr(executor_confirmed_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_confirmed_module, "persist_pending_confirmation_bundle", save_confirmation)
    monkeypatch.setattr(executor_confirmed_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_confirmed_module, "notify_confirmation_tool_results", notify_confirmation_results)
    monkeypatch.setattr(executor_confirmed_module, "dump_background_proactive_history", dump_history)
    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("pending re-audit must not execute tools"))
    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", lambda *args, **kwargs: pytest.fail("pending re-audit must not continue interactively"))

    response = await executor_confirmed_module._execute_confirmed_tools(FakeDb(), work)

    assert json.loads(response["content"]) == confirmation_payload
    assert len(replaced_results) == 1
    assert replaced_results[0]["pending_message"].id == 2
    assert replaced_results[0]["original_tool_call_id"] == "original-1"
    assert replaced_results[0]["audit_record_id"] == 99
    assert notified_audit_record_ids == [99]
    assert event_order == ["replace", "commit", "notify"]
    assert len(saved_messages) == 1
    assert saved_messages[0][1]["confirmation_payload"] == confirmation_payload
    assert saved_messages[0][1]["tool_results"] == []
    assert any(message.role == MessageRole.TOOL and message.tool_call_id == "original-1" for message in memory_chains[0])
    refreshed_frozen_payload = await executor_replies_module.session_reply_work_item_crud.get_frozen_context(FakeDb(), work)
    refreshed_frozen = [InternalMessage.model_validate(item) for item in refreshed_frozen_payload]
    assert refreshed_frozen[1].role == MessageRole.TOOL
    assert refreshed_frozen[1].tool_call_id == "original-1"
    assert refreshed_frozen[1].content != "old-pending-result"
    refreshed_checkpoint = [InternalMessage.model_validate(item) for item in checkpoint_store[work.id]["messages"]]
    assert refreshed_checkpoint[1].tool_call_id == "original-1"
    assert refreshed_checkpoint[1].content == refreshed_frozen[1].content


def test_confirmed_wait_checkpoint_refreshes_result_and_keeps_decision():
    pending_message = Message(
        id=2,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="original-1",
            content="actual-result",
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    checkpoint = executor_confirmed_module._build_confirmed_wait_checkpoint(
        {
            "messages": [
                InternalMessage(
                    id=1,
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="original-1",
                            name="execute_shell",
                            arguments={"command": "echo 1"},
                        )
                    ],
                ).model_dump(mode="json", exclude_none=True),
                InternalMessage(
                    id=2,
                    role=MessageRole.TOOL,
                    tool_call_id="original-1",
                    content="pending",
                ).model_dump(mode="json", exclude_none=True),
            ],
            "turn_messages": [{"role": "assistant", "content": "stale"}],
        },
        pending_tool_results={"original-1": pending_message},
        decision_message_id=4,
        decision_raw_message="同意",
    )

    messages = [InternalMessage.model_validate(item) for item in checkpoint["messages"]]
    assert messages[1].tool_call_id == "original-1"
    assert messages[1].content == "actual-result"
    assert messages[-1].id == 4
    assert messages[-1].role == MessageRole.USER
    assert messages[-1].content == "同意"
    assert checkpoint["turn_messages"] == []


@pytest.mark.asyncio
async def test_confirmed_execution_shell_handoff_keeps_attempt_open_and_continues_reply(
    monkeypatch,
    frozen_context_store,
):
    source_internal = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="original-1",
                name="execute_shell",
                arguments={"command": "echo 1", "execution_mode": "interactive"},
            )
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
        decision_raw_message="同意",
        decision_message_id=4,
        execution_scope="task",
    )
    details = [
        SimpleNamespace(
            id=11,
            original_tool_call_id="original-1",
            turn_index=0,
            tool_name="execute_shell",
            arguments_hash="b" * 64,
            decision_source="model_audit",
            action_request=None,
        )
    ]
    pending_message = Message(
        id=2,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="original-1",
            content="pending",
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    work = SimpleNamespace(
        source_id="42",
        execution_state={
            "audit_claim_token": "claim-token",
            "decision_message_id": 4,
            "execution_scope": "task",
            "frozen_context_message_ids": [3, 1, 2],
        },
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="confirmed-audit:42",
        created_at=datetime(2026, 7, 20, 0, 0, 0),
    )
    frozen_context_store.update(
        {
            3: InternalMessage(
                id=3,
                role=MessageRole.USER,
                content="scheduled trigger context",
            ).model_dump(mode="json", exclude_none=True),
            1: InternalMessage(
                id=1,
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="original-1",
                        name="execute_shell",
                        arguments={"command": "echo 1", "execution_mode": "interactive"},
                    )
                ],
            ).model_dump(mode="json", exclude_none=True),
            2: InternalMessage(
                id=2,
                role=MessageRole.TOOL,
                tool_call_id="original-1",
                content="pending",
            ).model_dump(mode="json", exclude_none=True),
        }
    )
    created_attempts = []
    finish_attempt_calls = []
    finish_round_calls = []
    process_calls = []
    precheck_calls = []
    dispatch_calls = []
    replaced_results = []
    confirmation_status_calls = []
    notification_calls = []
    event_order = []

    class FakeDb:
        def __init__(self):
            self.info = {}

        async def get(self, model, item_id):
            assert item_id == 1
            return source_message

        async def commit(self):
            event_order.append("commit")

    async def get_record(db, audit_record_id):
        return record

    async def list_details(db, audit_record_id):
        return details

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def validate_profile(db, profile):
        return SimpleNamespace()

    async def get_tools(db, profile, **kwargs):
        return [], None

    async def create_execution(db, **kwargs):
        created_attempts.append(kwargs)
        return SimpleNamespace(id=101)

    async def process_tool(tool_call, db, profile, cfg, messages, username, session_id, turn, uid, **kwargs):
        process_calls.append((tool_call, messages, kwargs))
        handoff = ShellInteractiveHandoffResult(
            terminal_session_id="t" * 32,
            status=TerminalSessionStatus.STARTING,
            output_buffer=TerminalOutputBufferState(
                capacity_bytes=65_536,
                oldest_offset=0,
                next_offset=0,
                oldest_sequence=1,
                next_sequence=1,
            ),
            output_stream="merged_stdout_stderr",
        )
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=handoff.model_dump_json(),
        )

    async def replace_result(db, **kwargs):
        event_order.append("replace")
        replaced_results.append(kwargs)
        pending_message.content = InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=kwargs["original_tool_call_id"],
            content=kwargs["content"],
        ).model_dump_json(exclude_none=True)
        return kwargs["content"]

    async def get_pending(db, **kwargs):
        return {"original-1": pending_message}

    async def finish_attempt(db, **kwargs):
        finish_attempt_calls.append(kwargs)
        return True

    async def finish_round_if_complete(db, **kwargs):
        finish_round_calls.append(kwargs)
        return None

    async def update_confirmation(*args, **kwargs):
        confirmation_status_calls.append((args, kwargs))

    async def notify_confirmation_results(*args, **kwargs):
        event_order.append("notify")
        notification_calls.append(kwargs["audit_record_id"])

    async def dispatch_interactive_work(*args, **kwargs):
        dispatch_calls.append(kwargs)
        event_order.append("dispatch")
        return {
            **llm_response_payload(
                "完成",
                history=[{"role": "assistant", "content": "完成"}],
                files=[],
            ),
            "response_id": "response-final",
        }

    monkeypatch.setattr(executor_confirmed_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_confirmed_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_confirmed_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_confirmed_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_confirmed_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_confirmed_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_confirmed_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "create_execution_attempt", create_execution)

    def prevalidate_tool_round(*args, **kwargs):
        precheck_calls.append(kwargs)
        return {}

    monkeypatch.setattr(executor_confirmed_module, "prevalidate_tool_round", prevalidate_tool_round)
    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", process_tool)
    monkeypatch.setattr(executor_confirmed_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(executor_confirmed_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_confirmed_module, "notify_confirmation_tool_results", notify_confirmation_results)
    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", dispatch_interactive_work)

    response = await executor_confirmed_module._execute_confirmed_tools(FakeDb(), work, "worker-1")

    assert response["content"] == "完成"
    assert response["history"] == [{"role": "assistant", "content": "完成"}]
    assert response["files"] == []
    assert response["response_id"] == "response-final"
    assert len(created_attempts) == 1
    assert len(process_calls) == 1
    assert process_calls[0][0].name == "execute_shell"
    assert precheck_calls[0]["execution_scope"] == "task"
    assert process_calls[0][2]["execution_scope"] == "task"
    assert dispatch_calls[0]["execution_scope"] == "task"
    assert dispatch_calls[0]["allow_additional_user_messages"] is False
    frozen_messages = dispatch_calls[0]["frozen_context_messages"]
    assert frozen_messages[0].content == "scheduled trigger context"
    assert frozen_messages[1].tool_calls[0].id == "original-1"
    assert frozen_messages[2].role == MessageRole.TOOL
    assert frozen_messages[2].tool_call_id == "original-1"
    assert "terminal_session_id" in str(frozen_messages[2].content)
    assert finish_attempt_calls == []
    assert finish_round_calls == [{"audit_record_id": 42, "claim_token": "claim-token"}]
    assert replaced_results[0]["pending_message"].id == 2
    assert replaced_results[0]["original_tool_call_id"] == "original-1"
    assert confirmation_status_calls == []
    assert notification_calls == [42]
    assert event_order == ["replace", "commit", "notify", "dispatch"]


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
        decision_raw_message="同意",
        decision_message_id=4,
        execution_scope="foreground",
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
        return [SimpleNamespace(original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64, decision_source="model_audit", action_request=None)]

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

    monkeypatch.setattr(executor_confirmed_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_confirmed_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_confirmed_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_confirmed_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_confirmed_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_confirmed_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "mark_source_message_invalid", mark_invalid)
    monkeypatch.setattr(executor_confirmed_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_metadata_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("invalid pending results must not execute tools"))
    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", lambda *args, **kwargs: pytest.fail("invalid pending results must not continue interactively"))
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "create_execution_attempt", lambda *args, **kwargs: pytest.fail("invalid pending results must not create executions"))

    response = await executor_confirmed_module._execute_confirmed_tools(FakeDb(), work)

    assert response["content"] == "原调用记录无效"
    assert invalidated[0]["audit_record_id"] == 42


@pytest.mark.asyncio
async def test_confirmed_execution_rejects_mismatched_decision_message_boundary(monkeypatch):
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
        decision_raw_message="同意",
        decision_message_id=5,
        execution_scope="foreground",
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
    execution_attempt_calls = []
    process_calls = []
    interactive_dispatch_calls = []

    class FakeDb:
        def __init__(self):
            self.info = {}

        async def get(self, model, item_id):
            assert item_id == 1
            return source_message

    async def get_record(db, audit_record_id):
        assert audit_record_id == 42
        return record

    async def list_details(db, audit_record_id):
        return [SimpleNamespace(original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64, decision_source="model_audit", action_request=None)]

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def mark_invalid(db, **kwargs):
        invalidated.append(kwargs)
        return True

    async def update_confirmation(db, *, audit_record_id):
        assert audit_record_id == 42

    async def generate_reply(db, **kwargs):
        return InternalMessage(role=MessageRole.ASSISTANT, content="原调用记录无效"), [], []

    async def get_pending(db, **kwargs):
        pytest.fail("mismatched decision message must reject the source before pending lookup")

    async def create_execution(db, **kwargs):
        execution_attempt_calls.append(kwargs)

    async def process_tool(*args, **kwargs):
        process_calls.append((args, kwargs))

    async def dispatch_interactive_work(*args, **kwargs):
        interactive_dispatch_calls.append((args, kwargs))

    monkeypatch.setattr(executor_confirmed_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_confirmed_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "mark_source_message_invalid", mark_invalid)
    monkeypatch.setattr(executor_confirmed_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_metadata_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)
    monkeypatch.setattr(executor_confirmed_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", process_tool)
    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", dispatch_interactive_work)

    response = await executor_confirmed_module._execute_confirmed_tools(FakeDb(), work)

    assert response["content"] == "原调用记录无效"
    assert invalidated == [
        {
            "audit_record_id": 42,
            "claim_token": "claim-token",
            "error_reason": "原工具调用记录校验失败",
        }
    ]
    assert execution_attempt_calls == []
    assert process_calls == []
    assert interactive_dispatch_calls == []


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
        decision_raw_message="同意",
        decision_message_id=4,
        execution_scope="foreground",
    )
    details = [SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64, decision_source="model_audit", action_request=None)]
    work = SimpleNamespace(
        source_id="42",
        execution_state={"audit_claim_token": "claim-token", "decision_message_id": 4},
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
        def __init__(self):
            self.info = {}

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

    async def dispatch_interactive_work(*args, **kwargs):
        return llm_response_payload("工具预检失败", history=[], files=[])

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

    async def get_tools(db, profile, **kwargs):
        return [], None

    async def update_confirmation(*args, **kwargs):
        return None

    async def replace_result(db, **kwargs):
        return kwargs["content"]

    async def get_pending(db, **kwargs):
        return {"original-1": SimpleNamespace(id=2, content="pending")}

    monkeypatch.setattr(executor_confirmed_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_confirmed_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_confirmed_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_confirmed_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_round", finish_round)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", dispatch_interactive_work)
    monkeypatch.setattr(executor_confirmed_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_confirmed_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_confirmed_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_confirmed_module, "verify_persisted_tool_round", lambda **kwargs: True)
    monkeypatch.setattr(executor_confirmed_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_confirmed_module, "prevalidate_tool_round", prevalidate)
    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("tool execution must not start"))

    await executor_confirmed_module._execute_confirmed_tools(FakeDb(), work)

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
        decision_raw_message="同意",
        decision_message_id=4,
        execution_scope="foreground",
    )
    details = [
        SimpleNamespace(id=11, original_tool_call_id="original-1", turn_index=0, tool_name="safe_tool", arguments_hash="b" * 64, decision_source="model_audit", action_request=None),
        SimpleNamespace(id=12, original_tool_call_id="original-2", turn_index=1, tool_name="safe_tool", arguments_hash="c" * 64, decision_source="model_audit", action_request=None),
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
    cancelled_attempts = []
    round_calls = []
    complete_calls = []
    created_attempts = [SimpleNamespace(id=101), None]

    class FakeDb:
        def __init__(self):
            self.info = {}

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

    async def get_tools(db, profile, **kwargs):
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

    async def dispatch_interactive_work(*args, **kwargs):
        return llm_response_payload("工具执行未完成", history=[], files=[])

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

    monkeypatch.setattr(executor_confirmed_module.audit_crud, "get_record", get_record)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(executor_confirmed_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_confirmed_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(executor_confirmed_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_round", finish_round)
    monkeypatch.setattr(executor_confirmed_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", dispatch_interactive_work)
    monkeypatch.setattr(executor_confirmed_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(executor_confirmed_module, "get_pending_tool_results", get_pending)
    monkeypatch.setattr(executor_confirmed_module, "replace_pending_tool_result", replace_result)
    monkeypatch.setattr(executor_confirmed_module, "verify_persisted_tool_round", verify_round)
    monkeypatch.setattr(executor_confirmed_module, "_confirmed_file_snapshots_changed", lambda *args, **kwargs: False)
    monkeypatch.setattr(executor_confirmed_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", lambda *args, **kwargs: pytest.fail("tool execution must not start"))

    await executor_confirmed_module._execute_confirmed_tools(FakeDb(), work)

    assert record.status == AuditRecordStatus.FAILED
    assert len(cancelled_attempts) == 1
    assert cancelled_attempts[0]["status"] == AuditExecutionStatus.CANCELLED
    assert len(round_calls) == 1
    assert round_calls[0]["status"] == AuditRecordStatus.FAILED
    assert complete_calls == []
