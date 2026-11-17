import asyncio
import json

import pytest

from app.core.audit.service import AuditRoundResult
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from tests.unit.chat.audited_reply_support import _run_audited_interactive_dispatch
from tests.unit.chat.reply_retry_context_support import _patch_session_crud as _patch_session_crud
from tests.unit.response_test_support import audit_confirmation_payload, pending_audit_tool_result


def _checkpoint_contains_cancelled_confirmation(checkpoint):
    def contains_cancelled_confirmation(value):
        if isinstance(value, dict):
            if value.get("confirmation_status") in {"superseded", "cancelled"}:
                return True
            if value.get("status") == "cancelled" and "confirmation_status" in value:
                return True
            return any(contains_cancelled_confirmation(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains_cancelled_confirmation(item) for item in value)
        if isinstance(value, str):
            try:
                parsed_value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return False
            return parsed_value != value and contains_cancelled_confirmation(parsed_value)
        return False

    return contains_cancelled_confirmation(checkpoint)


@pytest.mark.asyncio
async def test_pending_audit_cancels_unpersisted_confirmation_when_batch_arrives_during_audit(monkeypatch):
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    additional_messages_arrived = asyncio.Event()
    queued_batch = UserInputBatch(
        messages=(
            InternalMessage(id=32, role=MessageRole.USER, content="first append"),
            InternalMessage(id=30, role=MessageRole.USER, content="second append"),
            InternalMessage(id=31, role=MessageRole.USER, content="third append"),
        ),
        source_message_ids=(32, 30, 31),
    )
    audit_result = AuditRoundResult(
        audit_record_id=42,
        status=AuditRecordStatus.PENDING,
        tool_results=(pending_audit_tool_result("call-1"),),
        confirmation_payload=audit_confirmation_payload(42),
    )
    checkpoints = []
    generated_calls = []
    cancelled_audit_records = []
    tool_calls = []
    batch_delivery_count = 0
    fetch_count = 0
    none_fetch_count = 0

    async def wait_during_audit():
        audit_started.set()
        await asyncio.wait_for(release_audit.wait(), timeout=1)

    async def fetch_additional_messages():
        nonlocal batch_delivery_count, fetch_count, none_fetch_count
        fetch_count += 1
        if additional_messages_arrived.is_set() and batch_delivery_count == 0:
            batch_delivery_count += 1
            return queued_batch
        none_fetch_count += 1
        return None

    async def persist_pending_confirmation_bundle(*args, **kwargs):
        raise AssertionError("new messages must cancel the pending audit before confirmation persistence")

    async def persist_cancelled_pending_audit_results(
        db,
        *,
        audit_record_id,
        uid,
        session_id,
        profile_id,
        tool_results,
    ):
        cancelled_audit_records.append(audit_record_id)
        assert (uid, session_id, profile_id) == ("user-1", "session-1", 1)
        assert [tool_result.tool_call_id for tool_result in tool_results] == ["call-1"]
        return [
            InternalMessage(
                id=201,
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps(
                    {
                        "status": "cancelled",
                        "confirmation_status": "superseded",
                        "tool_name": "execute_shell",
                    }
                ),
            )
        ]

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        raise AssertionError("cancelled pending audit must not execute tools")

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_result=audit_result,
            audit_waiter=wait_during_audit,
            generated_calls_target=generated_calls,
            additional_user_messages_fetcher=fetch_additional_messages,
            persist_pending_confirmation_bundle_handler=persist_pending_confirmation_bundle,
            persist_cancelled_pending_audit_results_handler=persist_cancelled_pending_audit_results,
        )
    )

    await asyncio.wait_for(audit_started.wait(), timeout=1)
    additional_messages_arrived.set()
    release_audit.set()
    response, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert cancelled_audit_records == [42]
    assert tool_calls == []
    assert len(generated_calls) == 2
    next_request = generated_calls[1]["messages"]
    assert [tool_call.id for message in next_request if message.role == MessageRole.ASSISTANT and message.tool_calls for tool_call in message.tool_calls] == ["call-1"]
    cancelled_results = [message for message in next_request if message.role == MessageRole.TOOL and message.tool_call_id == "call-1"]
    assert len(cancelled_results) == 1
    assert json.loads(cancelled_results[0].content)["status"] == "cancelled"
    assert json.loads(cancelled_results[0].content)["confirmation_status"] == "superseded"
    appended_message_ids = [message.id for message in next_request if message.role == MessageRole.USER and message.id in queued_batch.source_message_ids]
    assert appended_message_ids == [32, 30, 31]
    assert all(appended_message_ids.count(message_id) == 1 for message_id in queued_batch.source_message_ids)
    assert fetch_count == 4
    assert none_fetch_count == 3
    assert batch_delivery_count == 1
    cancelled_checkpoint = next(checkpoint for checkpoint in checkpoints if _checkpoint_contains_cancelled_confirmation(checkpoint))
    assert cancelled_checkpoint["current_turn"] == 0
    assert cancelled_checkpoint["context_summary_fixed_upper_message_id"] == 30
    assert response["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_allowed_tool_executes_once_before_appended_batch_reaches_next_model_round(monkeypatch):
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    additional_messages_arrived = asyncio.Event()
    queued_batch = UserInputBatch(
        messages=(
            InternalMessage(id=52, role=MessageRole.USER, content="first append"),
            InternalMessage(id=50, role=MessageRole.USER, content="second append"),
            InternalMessage(id=51, role=MessageRole.USER, content="third append"),
        ),
        source_message_ids=(52, 50, 51),
    )
    generated_calls = []
    tool_calls = []
    batch_delivery_count = 0
    fetch_count = 0
    none_fetch_count = 0

    async def fetch_additional_messages():
        nonlocal batch_delivery_count, fetch_count, none_fetch_count
        fetch_count += 1
        if additional_messages_arrived.is_set() and batch_delivery_count == 0:
            batch_delivery_count += 1
            return queued_batch
        none_fetch_count += 1
        return None

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        tool_started.set()
        await asyncio.wait_for(release_tool.wait(), timeout=1)
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"status":"success"}',
        )

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            generated_calls_target=generated_calls,
            additional_user_messages_fetcher=fetch_additional_messages,
        )
    )

    await asyncio.wait_for(tool_started.wait(), timeout=1)
    additional_messages_arrived.set()
    release_tool.set()
    response, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert tool_calls == ["call-1"]
    assert len(generated_calls) == 2
    next_request = generated_calls[1]["messages"]
    completed_results = [message for message in next_request if message.role == MessageRole.TOOL and message.tool_call_id == "call-1"]
    assert len(completed_results) == 1
    assert json.loads(completed_results[0].content)["status"] == "success"
    appended_message_ids = [message.id for message in next_request if message.role == MessageRole.USER and message.id in queued_batch.source_message_ids]
    assert appended_message_ids == [52, 50, 51]
    assert all(appended_message_ids.count(message_id) == 1 for message_id in queued_batch.source_message_ids)
    assert fetch_count == 3
    assert none_fetch_count == 2
    assert batch_delivery_count == 1
    assert response["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_streamed_pending_audit_bundle_is_superseded_when_batch_arrives_during_persistence(monkeypatch):
    confirmation_payload = audit_confirmation_payload(42, risk=8, confirmation_mode="high_risk_override")
    audit_result = AuditRoundResult(
        audit_record_id=42,
        status=AuditRecordStatus.PENDING,
        tool_results=(pending_audit_tool_result("call-1"),),
        confirmation_payload=confirmation_payload,
    )
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    additional_messages_arrived = asyncio.Event()
    queued_batch = UserInputBatch(
        messages=(
            InternalMessage(id=62, role=MessageRole.USER, content="continue first"),
            InternalMessage(id=60, role=MessageRole.USER, content="continue second"),
            InternalMessage(id=61, role=MessageRole.USER, content="continue third"),
        ),
        source_message_ids=(62, 60, 61),
    )
    batch_delivery_count = 0
    fetch_count = 0
    none_fetch_count = 0
    lifecycle = []
    checkpoints = []
    generated_calls = []
    tool_calls = []

    async def fetch_additional_messages():
        nonlocal batch_delivery_count, fetch_count, none_fetch_count
        fetch_count += 1
        if additional_messages_arrived.is_set() and batch_delivery_count == 0:
            batch_delivery_count += 1
            return queued_batch
        none_fetch_count += 1
        return None

    async def persist_confirmation_bundle(db, *, tool_results, confirmation_payload, **kwargs):
        persistence_started.set()
        await asyncio.wait_for(release_persistence.wait(), timeout=1)
        lifecycle.append("persisted")
        stored_results = []
        for index, tool_result in enumerate(tool_results, start=1):
            stored_tool_result = tool_result.model_copy(deep=True)
            stored_tool_result.id = 200 + index
            stored_results.append(stored_tool_result)
        return stored_results, InternalMessage(
            id=300,
            role=MessageRole.ASSISTANT,
            content=json.dumps(confirmation_payload, ensure_ascii=False),
        )

    async def supersede_confirmation_bundle(db, *, audit_record_id, uid, session_id):
        lifecycle.append("superseded")
        assert (audit_record_id, uid, session_id) == (42, "user-1", "session-1")
        return [
            InternalMessage(
                id=201,
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps(
                    {
                        "status": "cancelled",
                        "confirmation_status": "superseded",
                        "tool_name": "execute_shell",
                    }
                ),
            )
        ]

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        tool_calls.append(tool_call.id)
        raise AssertionError("superseded pending audit must not execute tools")

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_result=audit_result,
            generated_calls_target=generated_calls,
            additional_user_messages_fetcher=fetch_additional_messages,
            persist_pending_confirmation_bundle_handler=persist_confirmation_bundle,
            supersede_pending_confirmation_bundle_handler=supersede_confirmation_bundle,
            stream_dispatch=True,
        )
    )

    await asyncio.wait_for(persistence_started.wait(), timeout=1)
    additional_messages_arrived.set()
    release_persistence.set()
    events, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert fetch_count == 5
    assert none_fetch_count == 4
    assert batch_delivery_count == 1
    assert lifecycle == ["persisted", "superseded"]
    assert len(generated_calls) == 2
    appended_message_ids = [message.id for message in generated_calls[1]["messages"] if message.role == MessageRole.USER and message.id in queued_batch.source_message_ids]
    assert appended_message_ids == [62, 60, 61]
    assert all(appended_message_ids.count(message_id) == 1 for message_id in queued_batch.source_message_ids)
    cancelled_result = next(message for message in generated_calls[1]["messages"] if message.role == MessageRole.TOOL)
    cancelled_payload = json.loads(cancelled_result.content)
    assert cancelled_payload["status"] == "cancelled"
    assert cancelled_payload["confirmation_status"] == "superseded"
    cancelled_checkpoint = next(checkpoint for checkpoint in checkpoints if _checkpoint_contains_cancelled_confirmation(checkpoint))
    assert cancelled_checkpoint["current_turn"] == 0
    assert cancelled_checkpoint["context_summary_fixed_upper_message_id"] == 60
    assert tool_calls == []
    assert [event["type"] for event in events] == [
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
    first_round_response_id = events[1]["response_id"]
    assert first_round_response_id == events[2]["response_id"] == events[3]["response_id"]
    assert first_round_response_id == events[4]["response_id"] == events[5]["response_id"] == events[6]["response_id"]
    final_round_response_id = events[7]["response_id"]
    assert final_round_response_id == events[8]["response_id"] == events[9]["response_id"]
    assert final_round_response_id == events[10]["response_id"] == events[11]["response_id"] == events[12]["response_id"]
    assert first_round_response_id != final_round_response_id
    assert events[-1]["response"]["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_batches_advance_confirmation_and_summary_boundaries_without_replay(monkeypatch):
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    confirmation_persistence_started = asyncio.Event()
    release_confirmation_persistence = asyncio.Event()
    batch_b = UserInputBatch(
        messages=(
            InternalMessage(id=102, role=MessageRole.USER, content="batch-b-first"),
            InternalMessage(id=100, role=MessageRole.USER, content="batch-b-second"),
        ),
        source_message_ids=(102, 100),
    )
    batch_c = UserInputBatch(
        messages=(
            InternalMessage(id=112, role=MessageRole.USER, content="batch-c-first"),
            InternalMessage(id=110, role=MessageRole.USER, content="batch-c-second"),
        ),
        source_message_ids=(112, 110),
    )
    batch_d = UserInputBatch(
        messages=(
            InternalMessage(id=122, role=MessageRole.USER, content="batch-d-first"),
            InternalMessage(id=120, role=MessageRole.USER, content="batch-d-second"),
        ),
        source_message_ids=(122, 120),
    )
    pending_results = [
        AuditRoundResult(
            audit_record_id=42,
            status=AuditRecordStatus.PENDING,
            tool_results=(pending_audit_tool_result("call-pending-a"),),
            confirmation_payload=audit_confirmation_payload(42),
        ),
        AuditRoundResult(
            audit_record_id=43,
            status=AuditRecordStatus.PENDING,
            tool_results=(pending_audit_tool_result("call-pending-b"),),
            confirmation_payload=audit_confirmation_payload(43),
        ),
        AuditRoundResult(
            audit_record_id=44,
            status=AuditRecordStatus.PASSED,
            tool_results=(),
            confirmation_payload=None,
        ),
    ]
    queued_batches: list[UserInputBatch] = []
    delivered_batches: list[UserInputBatch] = []
    checkpoints = []
    generated_calls = []
    cancellation_records = []
    executed_tool_calls = []
    audit_round_count = 0

    async def fetch_additional_messages():
        if not queued_batches:
            return None
        batch = queued_batches.pop(0)
        delivered_batches.append(batch)
        return batch

    async def wait_during_first_audit():
        nonlocal audit_round_count
        audit_round_count += 1
        if audit_round_count != 1:
            return
        audit_started.set()
        await asyncio.wait_for(release_audit.wait(), timeout=1)

    async def persist_cancelled_pending_audit_results(*_args, audit_record_id, tool_results, **_kwargs):
        cancellation_records.append(("before_persist", audit_record_id))
        return [
            InternalMessage(
                id=201,
                role=MessageRole.TOOL,
                tool_call_id=tool_results[0].tool_call_id,
                content=json.dumps({"status": "cancelled", "confirmation_status": "superseded"}),
            )
        ]

    async def persist_pending_confirmation_bundle(*_args, audit_record_id, tool_results, confirmation_payload, **_kwargs):
        assert audit_record_id == 43
        confirmation_persistence_started.set()
        await asyncio.wait_for(release_confirmation_persistence.wait(), timeout=1)
        return [
            InternalMessage(
                id=202,
                role=MessageRole.TOOL,
                tool_call_id=tool_results[0].tool_call_id,
                content=tool_results[0].content,
            )
        ], InternalMessage(
            id=301,
            role=MessageRole.ASSISTANT,
            content=json.dumps(confirmation_payload),
        )

    async def supersede_confirmation_bundle(*_args, audit_record_id, **_kwargs):
        cancellation_records.append(("after_persist", audit_record_id))
        return [
            InternalMessage(
                id=203,
                role=MessageRole.TOOL,
                tool_call_id="call-pending-b",
                content=json.dumps({"status": "cancelled", "confirmation_status": "superseded"}),
            )
        ]

    async def save_checkpoint(checkpoint):
        checkpoints.append(dict(checkpoint))

    async def process_tool(tool_call, *_args, **_kwargs):
        executed_tool_calls.append(tool_call.id)
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"status":"success"}',
        )

    async def enqueue_batch_d_after_intermediate_response(response_message):
        if response_message.content == "intermediate":
            queued_batches.append(batch_d)

    dispatch_task = asyncio.create_task(
        _run_audited_interactive_dispatch(
            monkeypatch,
            save_checkpoint,
            process_tool,
            audit_waiter=wait_during_first_audit,
            audit_results=pending_results,
            generated_calls_target=generated_calls,
            generate_hook=enqueue_batch_d_after_intermediate_response,
            additional_user_messages_fetcher=fetch_additional_messages,
            persist_pending_confirmation_bundle_handler=persist_pending_confirmation_bundle,
            persist_cancelled_pending_audit_results_handler=persist_cancelled_pending_audit_results,
            supersede_pending_confirmation_bundle_handler=supersede_confirmation_bundle,
            response_messages=[
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="call-pending-a", name="execute_shell", arguments={"command": "echo a"})],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="call-pending-b", name="execute_shell", arguments={"command": "echo b"})],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="call-allowed", name="execute_shell", arguments={"command": "echo allowed"})],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="intermediate"),
                InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
            ],
        )
    )

    await asyncio.wait_for(audit_started.wait(), timeout=1)
    queued_batches.append(batch_b)
    release_audit.set()
    await asyncio.wait_for(confirmation_persistence_started.wait(), timeout=1)
    queued_batches.append(batch_c)
    release_confirmation_persistence.set()
    response, unknown_calls = await asyncio.wait_for(dispatch_task, timeout=1)

    assert delivered_batches == [batch_b, batch_c, batch_d]
    assert cancellation_records == [("before_persist", 42), ("after_persist", 43)]
    assert executed_tool_calls == ["call-allowed"]
    batch_ids = [*batch_b.source_message_ids, *batch_c.source_message_ids, *batch_d.source_message_ids]
    request_batch_ids = [[message.id for message in call["messages"] if message.role == MessageRole.USER and message.id in batch_ids] for call in generated_calls]
    assert request_batch_ids == [
        [],
        list(batch_b.source_message_ids),
        [*batch_b.source_message_ids, *batch_c.source_message_ids],
        [*batch_b.source_message_ids, *batch_c.source_message_ids],
        [*batch_b.source_message_ids, *batch_c.source_message_ids, *batch_d.source_message_ids],
    ]
    assert all(len(message_ids) == len(set(message_ids)) for message_ids in request_batch_ids)
    checkpoint_upper_ids = [checkpoint["context_summary_fixed_upper_message_id"] for checkpoint in checkpoints if checkpoint["context_summary_fixed_upper_message_id"] != 1]
    assert checkpoint_upper_ids == sorted(checkpoint_upper_ids)
    assert list(dict.fromkeys(checkpoint_upper_ids)) == [100, 110, 120]
    assert response["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []
