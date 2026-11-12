import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import app.core.dispatchers.interactive_generation as interactive_generation_module
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    END_SESSION_TOOL_NAME,
    FILE_TOOL_NAME,
    GOAL_EXECUTION_PHASE_FINALIZING,
    GOAL_EXECUTION_PHASE_RUNNING,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentToolCall, SubagentWork
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer


async def _enable_agent_work(runtime) -> None:
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
        profile.configs = configs
        await db.commit()


async def _context_summary_threshold(runtime) -> int:
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        channel = await db.get(ModelChannel, 1)
        assert profile is not None and channel is not None
        config = ProfileConfig.model_validate(profile.configs)
        model_entry = next(item for item in channel.model_ids if item.get("model_id") == runtime.chat_model_id)
    return calculate_context_summary_trigger_tokens(
        context_window_k=int(model_entry["context_window_k"]),
        max_tokens=int(model_entry["max_tokens"]),
        threshold_percent=config.other.context_summary_threshold_percent,
    )


async def _child_for_parent(db, parent_work_id: int) -> SessionReplyWorkItem:
    return (
        (
            await db.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                    SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                    SessionReplyWorkItem.source_id == str(parent_work_id),
                )
            )
        )
        .scalars()
        .one()
    )


async def _create_child(runtime, *, instruction: str, prefix: str) -> tuple[int, int, str]:
    create_call = InternalToolCall(
        id=f"{prefix}-create",
        name=AGENT_WORK_TOOL_NAME,
        arguments={
            "operation": "create",
            "name": f"child-{prefix}"[:32],
            "message": instruction,
        },
    )
    runtime.outputs.extend(
        [
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]),
        ]
    )
    accepted = await runtime.client.post(
        "/api/v1/chat/completions",
        json={
            "session_id": runtime.session_id,
            "request_id": f"{prefix}-parent-request",
            "message": f"Create the independent child for {prefix} and wait for the child result.",
        },
    )
    assert accepted.status_code == 200
    accepted_data = accepted.json()["data"]
    assert accepted_data["submission_status"] == "accepted"
    parent_work_id = accepted_data["work_id"]

    parent_done = await runtime.run_next(independent_only=False)
    assert parent_done is not None
    assert parent_done.id == parent_work_id
    assert parent_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

    async with runtime.factory() as db:
        child = await _child_for_parent(db, parent_work_id)
        assert child.id is not None
        assert isinstance(child.subagent_id, str) and child.subagent_id
        return parent_work_id, child.id, child.subagent_id


async def _session_context_snapshot(runtime) -> tuple[Any, ...]:
    async with runtime.factory() as db:
        session = await db.get(ChatSession, runtime.session_id)
        assert session is not None
        return (
            session.context_summary,
            session.context_summary_message_id,
        )


def _provider_items(payload: dict[str, Any], protocol: str) -> list[dict[str, Any]]:
    items = payload.get("messages" if protocol == "OPENAI" else "input")
    assert isinstance(items, list)
    return [item for item in items if isinstance(item, dict)]


def _content_texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]


def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any] | None:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return None
    try:
        arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
    except (TypeError, ValueError):
        return None
    return arguments if isinstance(arguments, dict) else None


def _provider_tool_calls(payload: dict[str, Any], protocol: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in _provider_items(payload, protocol):
        if protocol == "OPENAI":
            raw_calls = item.get("tool_calls")
            if not isinstance(raw_calls, list):
                continue
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    continue
                arguments = _parse_tool_arguments(function.get("arguments"))
                if arguments is None:
                    continue
                calls.append(
                    {
                        "id": raw_call.get("id"),
                        "name": function.get("name"),
                        "arguments": arguments,
                    }
                )
            continue
        if item.get("type") != "function_call":
            continue
        arguments = _parse_tool_arguments(item.get("arguments"))
        if arguments is None:
            continue
        calls.append(
            {
                "id": item.get("call_id") or item.get("id"),
                "name": item.get("name"),
                "arguments": arguments,
            }
        )
    return calls


def _provider_tool_call_matches(
    payloads: list[dict[str, Any]],
    *,
    protocol: str,
    expected_call: InternalToolCall,
) -> list[dict[str, Any]]:
    matches = []
    for payload in payloads:
        for call in _provider_tool_calls(payload, protocol):
            if call["name"] == expected_call.name and call["arguments"] == expected_call.arguments:
                matches.append(call)
    return matches


def _checkpoint_tool_calls(messages: list[InternalMessage], expected_call: InternalToolCall) -> list[InternalToolCall]:
    return [call for message in messages if message.role == MessageRole.ASSISTANT for call in message.tool_calls or [] if call.name == expected_call.name and call.arguments == expected_call.arguments]


def _checkpoint_tool_results(messages: list[InternalMessage], expected_call: InternalToolCall) -> list[InternalMessage]:
    actual_call_ids = {call.id for call in _checkpoint_tool_calls(messages, expected_call)}
    return [message for message in messages if message.role == MessageRole.TOOL and message.tool_call_id in actual_call_ids]


def _provider_notifications(payload: dict[str, Any], protocol: str) -> list[dict[str, Any]]:
    notifications: list[dict[str, Any]] = []
    for item in _provider_items(payload, protocol):
        if item.get("role") != MessageRole.USER.value:
            continue
        for text in _content_texts(item.get("content")):
            try:
                envelope = json.loads(text)
            except (TypeError, ValueError):
                continue
            if not isinstance(envelope, dict) or not isinstance(envelope.get("user_message"), str):
                continue
            try:
                notification = json.loads(envelope["user_message"])
            except (TypeError, ValueError):
                continue
            if isinstance(notification, dict) and notification.get("type") == "system_notification" and notification.get("reason") == "context_exhausted":
                notifications.append(notification)
    return notifications


def _checkpoint_notifications(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    notifications: list[dict[str, Any]] = []
    for item in checkpoint.get("messages", []):
        if not isinstance(item, dict) or item.get("role") != MessageRole.USER.value or not isinstance(item.get("content"), str):
            continue
        try:
            notification = json.loads(item["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(notification, dict) and notification.get("type") == "system_notification" and notification.get("reason") == "context_exhausted":
            notifications.append(notification)
    return notifications


def _assert_notification_contract(notification: dict[str, Any]) -> None:
    assert notification["type"] == "system_notification"
    assert notification["reason"] == "context_exhausted"
    assert notification["required_sections"] == ["completed_tasks", "incomplete_tasks"]
    instruction = notification.get("instruction")
    assert isinstance(instruction, str)
    assert "Do not compress or summarize the context." in instruction
    assert "Immediately stop using tools" in instruction
    assert "completed tasks" in instruction
    assert "incomplete tasks" in instruction
    assert "Do not omit the report or use end_session to skip the report." in instruction


def _assert_notification_counts(
    payloads: list[dict[str, Any]],
    *,
    protocol: str,
    expected_counts: list[int],
    checkpoint_notification: dict[str, Any] | None,
) -> None:
    actual_notifications = [_provider_notifications(payload, protocol) for payload in payloads]
    assert [len(items) for items in actual_notifications] == expected_counts
    if checkpoint_notification is not None:
        _assert_notification_contract(checkpoint_notification)
        assert all(items and items[0] == checkpoint_notification for items, expected in zip(actual_notifications, expected_counts, strict=True) if expected)


async def _load_checkpoint_messages(runtime, child_work_id: int) -> tuple[dict[str, Any], list[InternalMessage]]:
    async with runtime.factory() as db:
        checkpoint = await session_reply_work_item_crud.get_checkpoint(db, child_work_id)
    assert isinstance(checkpoint, dict)
    messages = [InternalMessage.model_validate(item) for item in checkpoint["messages"]]
    return checkpoint, messages


def _tool_result(
    messages: list[InternalMessage],
    expected_call: InternalToolCall,
) -> tuple[InternalToolCall, InternalMessage, dict[str, Any]]:
    matching_calls = _checkpoint_tool_calls(messages, expected_call)
    assert len(matching_calls) == 1
    actual_call = matching_calls[0]
    matching_results = [message for message in messages if message.role == MessageRole.TOOL and message.tool_call_id == actual_call.id]
    assert len(matching_results) == 1
    result_message = matching_results[0]
    payload = json.loads(result_message.content or "{}")
    assert isinstance(payload, dict)
    return actual_call, result_message, payload


async def _assert_child_report_and_deliver_to_parent(
    runtime,
    *,
    parent_work_id: int,
    child_work_id: int,
    report: str,
) -> None:
    async with runtime.factory() as db:
        child = await db.get(SessionReplyWorkItem, child_work_id)
        round_row = await db.get(SubagentWork, child_work_id)
        assert child is not None and child.status == SessionReplyWorkStatus.SUCCEEDED
        assert round_row is not None and round_row.result is not None
        assert round_row.result["completed"] is True
        assert round_row.result["status"] == "succeeded"
        assert round_row.result["result"]["content"] == report

    parent_provider_start = len(runtime.provider_payloads)
    runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="parent-final-after-subagent-report"))
    parent_done = await runtime.run_next(independent_only=False)
    assert parent_done is not None
    assert parent_done.id == parent_work_id
    assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
    parent_payloads = runtime.provider_payloads[parent_provider_start:]
    assert len(parent_payloads) == 1
    assert report in json.dumps(parent_payloads, ensure_ascii=False)

    async with runtime.factory() as db:
        parent = await db.get(SessionReplyWorkItem, parent_work_id)
        call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
        assert parent is not None and parent.status == SessionReplyWorkStatus.SUCCEEDED
        assert call.delivered is True
        assert call.tool_result_message_id is not None
        result_row = await db.get(Message, call.tool_result_message_id)
        assert result_row is not None
        result_message = InternalMessage.model_validate_json(result_row.content or "{}")
        result_payload = json.loads(result_message.content or "{}")
        assert result_message.tool_call_id == call.tool_call_id
        assert result_payload == {
            "subagent_id": call.subagent_id,
            "work_id": call.child_work_id,
            "status": "succeeded",
            "content": report,
        }


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("goal_mode", [False, True], ids=["ordinary-mode", "goal-mode"])
@pytest.mark.parametrize("threshold_delta", [-1, 0, 1], ids=["below", "at", "above"])
@pytest.mark.asyncio
async def test_subagent_context_budget_boundary_preserves_protocol_and_parent_delivery(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: str,
    goal_mode: bool,
    threshold_delta: int,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=goal_mode) as runtime:
        await _enable_agent_work(runtime)
        threshold = await _context_summary_threshold(runtime)
        target = (tmp_path / f"context-boundary-{protocol}-{goal_mode}-{threshold_delta}.txt").resolve()
        instruction = f"Write exactly 'boundary-written' to {target}, then report completed_tasks and incomplete_tasks."
        parent_work_id, child_work_id, _subagent_id = await _create_child(
            runtime,
            instruction=instruction,
            prefix=f"context-boundary-{protocol.lower()}-{goal_mode}-{threshold_delta}",
        )
        main_context_before = await _session_context_snapshot(runtime)
        exhausted = threshold_delta >= 0
        write_call = InternalToolCall(
            id="boundary-child-write",
            name=FILE_TOOL_NAME,
            arguments={
                "operation": "write",
                "path": str(target),
                "content": "boundary-written",
            },
        )
        if exhausted:
            report = "completed_tasks: none; incomplete_tasks: boundary write was not completed after context exhaustion"
        else:
            report = "completed_tasks: verified boundary write; incomplete_tasks: none"
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[write_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=report),
            ]
        )
        child_provider_start = len(runtime.provider_payloads)

        def estimate_request_context_tokens(messages, tools, **kwargs):
            return threshold + threshold_delta

        with monkeypatch.context() as patch:
            patch.setattr(interactive_generation_module, "estimate_request_context_tokens", estimate_request_context_tokens)
            child_done = await runtime.run_next(independent_only=True)

        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        child_payloads = runtime.provider_payloads[child_provider_start:]
        assert len(child_payloads) == 2
        checkpoint, checkpoint_messages = await _load_checkpoint_messages(runtime, child_work_id)
        checkpoint_notifications = _checkpoint_notifications(checkpoint)
        assert len(checkpoint_notifications) == (1 if exhausted else 0)
        assert any(message.role == MessageRole.USER and message.content == instruction for message in checkpoint_messages)
        if exhausted:
            assert checkpoint.get("subagent_context_exhausted") is True
            assert checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_FINALIZING
            checkpoint_notification = checkpoint_notifications[0]
            _assert_notification_contract(checkpoint_notification)
            provider_write_matches = _provider_tool_call_matches(
                child_payloads[1:],
                protocol=protocol,
                expected_call=write_call,
            )
            assert len(provider_write_matches) == 1
            assert isinstance(provider_write_matches[0]["id"], str) and provider_write_matches[0]["id"]
            assert not target.exists()
            assert _checkpoint_tool_calls(checkpoint_messages, write_call) == []
            assert _checkpoint_tool_results(checkpoint_messages, write_call) == []
        else:
            assert checkpoint.get("subagent_context_exhausted") is not True
            assert checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_RUNNING
            checkpoint_notification = None
            assert target.read_text(encoding="utf-8") == "boundary-written"
            actual_write_call, result_message, result_payload = _tool_result(checkpoint_messages, write_call)
            assert actual_write_call.name == write_call.name
            assert actual_write_call.arguments == write_call.arguments
            assert result_message.tool_execution_status == "succeeded"
            assert result_payload["status"] == "success"
            assert result_payload["operation"] == "write"
            assert result_payload["path"] == str(target)
        _assert_notification_counts(
            child_payloads,
            protocol=protocol,
            expected_counts=[1, 1] if exhausted else [0, 0],
            checkpoint_notification=checkpoint_notification,
        )
        await _assert_child_report_and_deliver_to_parent(
            runtime,
            parent_work_id=parent_work_id,
            child_work_id=child_work_id,
            report=report,
        )
        assert await _session_context_snapshot(runtime) == main_context_before


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.asyncio
async def test_subagent_context_exhaustion_rejects_later_tool_after_real_progress(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: str,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        await _enable_agent_work(runtime)
        threshold = await _context_summary_threshold(runtime)
        first_target = (tmp_path / f"context-progress-first-{protocol}.txt").resolve()
        second_target = (tmp_path / f"context-progress-second-{protocol}.txt").resolve()
        instruction = f"Write 'first-progress' to {first_target}, then try the second step and report both task sections."
        parent_work_id, child_work_id, _subagent_id = await _create_child(
            runtime,
            instruction=instruction,
            prefix=f"context-progress-{protocol.lower()}",
        )
        main_context_before = await _session_context_snapshot(runtime)
        first_call = InternalToolCall(
            id="context-progress-first-write",
            name=FILE_TOOL_NAME,
            arguments={
                "operation": "write",
                "path": str(first_target),
                "content": "first-progress",
            },
        )
        second_call = InternalToolCall(
            id="context-progress-second-write",
            name=FILE_TOOL_NAME,
            arguments={
                "operation": "write",
                "path": str(second_target),
                "content": "second-progress-must-not-be-written",
            },
        )
        report = "completed_tasks: first file write verified; incomplete_tasks: second file write rejected after exhaustion"
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[first_call]),
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[second_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=report),
            ]
        )
        child_provider_start = len(runtime.provider_payloads)

        def estimate_request_context_tokens(messages, tools, **kwargs):
            has_tool_result = any(message.role == MessageRole.TOOL for message in messages)
            return threshold if has_tool_result else threshold - 1

        with monkeypatch.context() as patch:
            patch.setattr(interactive_generation_module, "estimate_request_context_tokens", estimate_request_context_tokens)
            child_done = await runtime.run_next(independent_only=True)

        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        child_payloads = runtime.provider_payloads[child_provider_start:]
        assert len(child_payloads) == 3
        first_provider_matches = _provider_tool_call_matches(
            child_payloads[1:],
            protocol=protocol,
            expected_call=first_call,
        )
        second_provider_matches = _provider_tool_call_matches(
            child_payloads[1:],
            protocol=protocol,
            expected_call=second_call,
        )
        assert first_provider_matches
        assert second_provider_matches
        assert all(isinstance(call["id"], str) and call["id"] for call in [*first_provider_matches, *second_provider_matches])
        assert first_target.read_text(encoding="utf-8") == "first-progress"
        assert not second_target.exists()

        checkpoint, checkpoint_messages = await _load_checkpoint_messages(runtime, child_work_id)
        checkpoint_notifications = _checkpoint_notifications(checkpoint)
        assert len(checkpoint_notifications) == 1
        assert checkpoint.get("subagent_context_exhausted") is True
        assert checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_FINALIZING
        assert any(message.role == MessageRole.USER and message.content == instruction for message in checkpoint_messages)
        actual_first_call, first_result_message, first_result_payload = _tool_result(checkpoint_messages, first_call)
        assert actual_first_call.name == first_call.name
        assert actual_first_call.arguments == first_call.arguments
        assert first_result_message.tool_execution_status == "succeeded"
        assert first_result_payload["status"] == "success"
        assert first_result_payload["operation"] == "write"
        assert first_result_payload["path"] == str(first_target)
        assert _checkpoint_tool_calls(checkpoint_messages, second_call) == []
        assert _checkpoint_tool_results(checkpoint_messages, second_call) == []
        _assert_notification_counts(
            child_payloads,
            protocol=protocol,
            expected_counts=[0, 1, 1],
            checkpoint_notification=checkpoint_notifications[0],
        )
        await _assert_child_report_and_deliver_to_parent(
            runtime,
            parent_work_id=parent_work_id,
            child_work_id=child_work_id,
            report=report,
        )
        assert await _session_context_snapshot(runtime) == main_context_before


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("violation", ["file_write", "end_session_skip_reply"], ids=["file-write", "end-session-skip-reply"])
@pytest.mark.asyncio
async def test_exhausted_subagent_cancelled_request_recovers_without_lifting_final_only_restriction(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: str,
    violation: str,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=True) as runtime:
        await _enable_agent_work(runtime)
        threshold = await _context_summary_threshold(runtime)
        target = (tmp_path / f"context-cancel-{protocol}-{violation}.txt").resolve()
        instruction = f"Complete the assigned task and report completed_tasks and incomplete_tasks for {target}."
        parent_work_id, child_work_id, _subagent_id = await _create_child(
            runtime,
            instruction=instruction,
            prefix=f"context-cancel-{protocol.lower()}-{violation}",
        )
        main_context_before = await _session_context_snapshot(runtime)
        if violation == "file_write":
            violation_call = InternalToolCall(
                id="context-cancel-file-write",
                name=FILE_TOOL_NAME,
                arguments={
                    "operation": "write",
                    "path": str(target),
                    "content": "must-not-be-written",
                },
            )
        else:
            violation_call = InternalToolCall(
                id="context-cancel-skip-reply",
                name=END_SESSION_TOOL_NAME,
                arguments={"skip_reply": True},
            )
        report = "completed_tasks: none verified after cancellation; incomplete_tasks: final report delivered without forbidden action"
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[violation_call]))
        transformer = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        fixture_post_json = transformer._post_json
        cancelled = False
        estimate_value = threshold

        def estimate_request_context_tokens(messages, tools, **kwargs):
            return estimate_value

        async def cancel_at_final_request(_transformer, **kwargs):
            nonlocal cancelled
            if not cancelled and _provider_notifications(kwargs["payload"], protocol):
                runtime.provider_payloads.append(kwargs["payload"])
                cancelled = True
                raise asyncio.CancelledError("cancelled at exhausted subagent provider request")
            return await fixture_post_json(_transformer, **kwargs)

        child_provider_start = len(runtime.provider_payloads)
        with monkeypatch.context() as patch:
            patch.setattr(interactive_generation_module, "estimate_request_context_tokens", estimate_request_context_tokens)
            patch.setattr(transformer, "_post_json", cancel_at_final_request)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=True)
            assert cancelled is True
            assert len(runtime.provider_payloads[child_provider_start:]) == 1

            async with runtime.factory() as db:
                child = await db.get(SessionReplyWorkItem, child_work_id)
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, child_work_id)
                assert child is not None and child.status == SessionReplyWorkStatus.RUNNING
                assert isinstance(checkpoint, dict)
                checkpoint_notifications = _checkpoint_notifications(checkpoint)
                assert len(checkpoint_notifications) == 1
                assert checkpoint.get("subagent_context_exhausted") is True
                assert checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_FINALIZING
                tool_rows = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.work_id == child_work_id,
                                Message.role == MessageRole.TOOL,
                                Message.type == MessageType.TOOL_RESULT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert tool_rows == []
                child.lock_until = 0
                checkpoint["execution_phase"] = GOAL_EXECUTION_PHASE_RUNNING
                await session_reply_work_item_crud.save_checkpoint(db, child_work_id, checkpoint)
                await db.commit()

            await SessionReplyConsumer()._recover_expired()
            async with runtime.factory() as db:
                recovered_child = await db.get(SessionReplyWorkItem, child_work_id)
                recovered_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, child_work_id)
                assert recovered_child is not None and recovered_child.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert isinstance(recovered_checkpoint, dict)
                assert recovered_checkpoint.get("subagent_context_exhausted") is True
                assert recovered_checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_RUNNING
                assert len(_checkpoint_notifications(recovered_checkpoint)) == 1

            estimate_value = threshold - 1
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=report))
            recovered_done = await runtime.run_next(independent_only=True)

        assert recovered_done is not None
        assert recovered_done.id == child_work_id
        assert recovered_done.status == SessionReplyWorkStatus.SUCCEEDED
        child_payloads = runtime.provider_payloads[child_provider_start:]
        assert len(child_payloads) == 3
        provider_violation_matches = _provider_tool_call_matches(
            child_payloads[1:],
            protocol=protocol,
            expected_call=violation_call,
        )
        assert len(provider_violation_matches) == 1
        assert isinstance(provider_violation_matches[0]["id"], str) and provider_violation_matches[0]["id"]
        assert target.exists() is False
        _assert_notification_counts(
            child_payloads,
            protocol=protocol,
            expected_counts=[1, 1, 1],
            checkpoint_notification=checkpoint_notifications[0],
        )

        checkpoint, checkpoint_messages = await _load_checkpoint_messages(runtime, child_work_id)
        assert checkpoint.get("subagent_context_exhausted") is True
        assert checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_FINALIZING
        assert len(_checkpoint_notifications(checkpoint)) == 1
        assert any(message.role == MessageRole.USER and message.content == instruction for message in checkpoint_messages)
        assert _checkpoint_tool_calls(checkpoint_messages, violation_call) == []
        assert _checkpoint_tool_results(checkpoint_messages, violation_call) == []

        async with runtime.factory() as db:
            child = await db.get(SessionReplyWorkItem, child_work_id)
            round_row = await db.get(SubagentWork, child_work_id)
            assert child is not None and round_row is not None and round_row.result is not None
            response = child.execution_state.get("response") if isinstance(child.execution_state, dict) else None
            assert not (isinstance(response, dict) and response.get("skip_reply") is True)
            assert round_row.result["result"]["content"] == report
            assert round_row.result["result"]["skip_reply"] is False
            tool_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.work_id == child_work_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert tool_rows == []

        await _assert_child_report_and_deliver_to_parent(
            runtime,
            parent_work_id=parent_work_id,
            child_work_id=child_work_id,
            report=report,
        )
        assert await _session_context_snapshot(runtime) == main_context_before
