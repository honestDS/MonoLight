import asyncio
import json

import pytest

from app.core.audit.service import AuditRoundResult
from app.core.constants import SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY
from app.core.dispatchers import interactive_tools as interactive_tools_module
from app.core.terminal.schemas import (
    ShellInteractiveHandoffResult,
    TerminalOutputBufferState,
    TerminalSessionStatus,
)
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from tests.unit.chat.audited_reply_support import _run_audited_interactive_dispatch
from tests.unit.chat.reply_retry_context_support import _patch_session_crud as _patch_session_crud
from tests.unit.response_test_support import audit_confirmation_payload, pending_audit_tool_result


@pytest.mark.asyncio
async def test_interactive_persists_handoff_binding_when_round_is_not_complete(monkeypatch):
    checkpoints = []
    finish_attempt_calls = []
    finish_round_if_complete_calls = []
    terminal_session_id = "h" * 32

    async def save_checkpoint(checkpoint):
        checkpoints.append(dict(checkpoint))

    async def process_tool(tool_call, *args, **kwargs):
        handoff = ShellInteractiveHandoffResult(
            terminal_session_id=terminal_session_id,
            status=TerminalSessionStatus.STARTING,
            output_buffer=TerminalOutputBufferState(
                capacity_bytes=1_048_576,
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

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        finish_attempt_calls_target=finish_attempt_calls,
        finish_round_if_complete_calls_target=finish_round_if_complete_calls,
        finish_round_if_complete_result=None,
        use_execution_round_if_complete=True,
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert finish_attempt_calls == []
    assert finish_round_if_complete_calls == [{"audit_record_id": 42, "claim_token": "claim-token"}]
    handoff_checkpoints = [checkpoint for checkpoint in checkpoints if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint]
    assert handoff_checkpoints[-1][SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
        "handoff_state": "persisted",
        "terminal_session_ids": [terminal_session_id],
    }
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_without_audit_configuration_executes_tool_without_audit_binding(monkeypatch):
    tool_calls = []
    checkpoints = []

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert tool_calls == ["call-1"]
    assert all(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in checkpoint for checkpoint in checkpoints)
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_persists_audit_binding_before_tool_and_clears_after_round(monkeypatch):
    current_state = {}
    persisted_states = []
    tool_states = []

    async def save_checkpoint(checkpoint):
        marker_present = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint
        marker = checkpoint.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
        current_state.update({"dispatcher_checkpoint": checkpoint})
        if marker_present:
            if marker is None:
                current_state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
            else:
                current_state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = marker
        persisted_states.append(dict(current_state))

    async def process_tool(tool_call, *args, **kwargs):
        tool_states.append(current_state.get(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY))
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    response, unknown_calls = await _run_audited_interactive_dispatch(monkeypatch, save_checkpoint, process_tool)

    assert response["choices"][0]["message"]["content"] == "finished"
    assert tool_states == [{"audit_record_id": 42, "claim_token": "claim-token"}]
    binding_checkpoints = [checkpoint for checkpoint in persisted_states if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint]
    assert binding_checkpoints[0][SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
    }
    assert SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY not in persisted_states[-1]
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_audits_each_successive_tool_round_before_execution(monkeypatch):
    checkpoints = []
    tool_names = []
    audit_calls = []

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_names.append(tool_call.name)
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        response_messages=[
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo 1"})],
            ),
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="call-2", name="file_tool", arguments={"operation": "write", "path": "note.txt", "content": "done"})],
            ),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ],
        audit_calls_target=audit_calls,
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert tool_names == ["execute_shell", "file_tool"]
    assert [[tool_call.name for tool_call in tool_calls] for tool_calls in audit_calls] == [
        ["execute_shell"],
        ["file_tool"],
    ]
    assert [checkpoint[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] for checkpoint in checkpoints if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint] == [
        {"audit_record_id": 42, "claim_token": "claim-token"},
        None,
        {"audit_record_id": 43, "claim_token": "claim-token"},
        None,
    ]
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_audit_binding_persistence_failure_prevents_tool_execution(monkeypatch):
    tool_calls = []
    unknown_calls = []

    async def save_checkpoint(checkpoint):
        if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint:
            raise RuntimeError("checkpoint storage failed")

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    with pytest.raises(interactive_tools_module.AuditExecutionStatePersistenceError):
        await _run_audited_interactive_dispatch(monkeypatch, save_checkpoint, process_tool, unknown_calls)

    assert tool_calls == []
    assert unknown_calls[0]["audit_record_id"] == 42
    assert unknown_calls[0]["claim_token"] == "claim-token"


@pytest.mark.asyncio
async def test_interactive_keeps_active_binding_when_audit_round_finish_fails(monkeypatch):
    current_state = {}
    generated_calls = []

    async def save_checkpoint(checkpoint):
        marker_present = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint
        marker = checkpoint.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
        current_state.update({"dispatcher_checkpoint": checkpoint})
        if marker_present:
            if marker is None:
                current_state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
            else:
                current_state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = marker

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    with pytest.raises(interactive_tools_module.AuditExecutionStatePersistenceError):
        await _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            finish_round_result=False,
            generated_calls_target=generated_calls,
        )

    assert len(generated_calls) == 1
    assert current_state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] == {
        "audit_record_id": 42,
        "claim_token": "claim-token",
    }


@pytest.mark.asyncio
async def test_streamed_pending_audit_publishes_tool_events_before_confirmation(monkeypatch):
    confirmation_payload = audit_confirmation_payload(42, risk=8, confirmation_mode="high_risk_override")
    audit_result = AuditRoundResult(
        audit_record_id=42,
        status=AuditRecordStatus.PENDING,
        tool_results=(pending_audit_tool_result("call-1"),),
        confirmation_payload=confirmation_payload,
    )

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(*args, **kwargs):
        raise AssertionError("pending audit must not execute tools")

    events, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=audit_result,
        stream_dispatch=True,
    )

    assert [event["type"] for event in events] == [
        "task_start",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
        "tool_end",
        "done",
    ]
    assert events[5]["tool_call_id"] == "call-1"
    assert events[5]["tool_call_index"] == 0
    assert events[5]["tool_call_count"] == 1
    assert json.loads(events[6]["result"])["status"] == "pending"
    assert json.loads(events[-1]["response"]["choices"][0]["message"]["content"]) == confirmation_payload
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_streamed_tool_events_include_batch_order_for_parallel_tools(monkeypatch):
    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"status":"success"}',
        )

    events, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_dispatch=True,
        response_messages=[
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content="我先检查",
                tool_calls=[
                    InternalToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo 1"},
                    ),
                    InternalToolCall(
                        id="call-2",
                        name="read_text_file",
                        arguments={"file_path": "note.txt"},
                    ),
                ],
            ),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ],
    )

    tool_start_events = [event for event in events if event["type"] == "tool_start"]
    assert [event["tool_call_index"] for event in tool_start_events] == [0, 1]
    assert [event["tool_call_count"] for event in tool_start_events] == [2, 2]
    assert tool_start_events[0]["response_id"] == tool_start_events[1]["response_id"]
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_pending_audit_publishes_tool_start_before_audit_finishes(monkeypatch):
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    published_events = []
    checkpoints = []
    audit_result = AuditRoundResult(
        audit_record_id=42,
        status=AuditRecordStatus.PENDING,
        tool_results=(pending_audit_tool_result("call-1"),),
        confirmation_payload=audit_confirmation_payload(42),
    )

    async def wait_during_audit():
        audit_started.set()
        await release_audit.wait()

    async def publish_event(event):
        published_events.append(event)

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(*args, **kwargs):
        raise AssertionError("pending audit must not execute tools")

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_result=audit_result,
            audit_waiter=wait_during_audit,
            stream_event_callback=publish_event,
        )
    )

    await asyncio.wait_for(audit_started.wait(), timeout=1)
    assert [event["type"] for event in published_events] == [
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
    ]

    release_audit.set()
    response, unknown_calls = await dispatch_task

    assert [event["type"] for event in published_events] == [
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
        "tool_end",
    ]
    assert json.loads(response["choices"][0]["message"]["content"])["type"] == "audit_confirmation"
    checkpoint_messages = [InternalMessage.model_validate(item) for item in checkpoints[-1]["messages"]]
    assert any(message.role == MessageRole.ASSISTANT and any(tool_call.id == "call-1" for tool_call in message.tool_calls or []) for message in checkpoint_messages)
    assert any(message.role == MessageRole.TOOL and message.tool_call_id == "call-1" for message in checkpoint_messages)
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_streamed_audit_claim_failure_closes_started_tool_event(monkeypatch):
    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(*args, **kwargs):
        raise AssertionError("claim failure must not execute tools")

    events, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        claim_execution_success=False,
        stream_dispatch=True,
    )

    event_types = [event["type"] for event in events]
    assert event_types == [
        "task_start",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "turn_end",
        "tool_start",
        "tool_end",
        "llm_request_metadata",
        "agent_loop_start",
        "agent_loop_output",
        "content",
        "turn_end",
        "done",
    ]
    assert events[1]["response_id"] == events[2]["response_id"]
    assert events[7]["response_id"] == events[8]["response_id"] == events[9]["response_id"]
    assert events[1]["response_id"] != events[7]["response_id"]
    assert events[4]["message_id"] == 11
    assert events[11]["message_id"] == 12
    assert json.loads(events[6]["result"])["status"] == "failed"
    assert events[6]["tool_call_id"] == events[5]["tool_call_id"] == "call-1"
    assert unknown_calls == []
