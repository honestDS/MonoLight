import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.session_reply_queue.child_delivery import (
    deliver_child_work_results,
    resume_child_work_for_parent,
)
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentToolCall, SubagentWork


def _payload(row: Message) -> tuple[InternalMessage, dict]:
    message = InternalMessage.model_validate_json(row.content or "{}")
    value = json.loads(message.content or "{}")
    assert isinstance(value, dict)
    return message, value


async def _notifications(db, session_id: str) -> tuple[tuple, tuple]:
    messages = (await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all()
    events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all()
    return (
        tuple((row.id, row.role, row.type, row.dedupe_key, row.content) for row in messages),
        tuple((row.id, row.dedupe_key, deepcopy(row.event)) for row in events),
    )


async def _enable_agent_work(runtime) -> None:
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
        profile.configs = configs
        await db.commit()


async def _submit(runtime, *, request_id: str, message: str) -> int:
    response = await runtime.client.post(
        "/api/v1/chat/completions",
        json={
            "session_id": runtime.session_id,
            "request_id": request_id,
            "message": message,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["submission_status"] in {"accepted", "queued"}
    return data["work_id"]


@asynccontextmanager
async def _create_case(image_chat_runtime, *, protocol: str, child_count: int = 1):
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        await _enable_agent_work(runtime)
        create_calls = [
            InternalToolCall(
                id=f"explicit-wait-create-{index}",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "create",
                    "name": f"explicit-wait-child-{index}",
                    "message": f"explicit-wait-child-instruction-{index}",
                },
            )
            for index in range(child_count)
        ]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls))
        origin_work_id = await _submit(
            runtime,
            request_id="explicit-wait-origin-request",
            message="Create independent child tasks, then answer the parent immediately.",
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="origin-parent-final"))
        provider_start = len(runtime.provider_payloads)
        origin_done = await runtime.run_next(independent_only=False)
        assert origin_done is not None
        assert origin_done.id == origin_work_id
        assert origin_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == provider_start + 2
        assert not runtime.outputs

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, origin_work_id)
            assert origin is not None
            children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(origin_work_id),
                        )
                        .order_by(SessionReplyWorkItem.created_at, SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == origin_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(children) == child_count
            assert len(calls) == child_count
            calls_by_child = {call.child_work_id: call for call in calls}
            child_data = []
            for child in children:
                assert child.id is not None
                call = calls_by_child[child.id]
                assert child.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert call.arguments["operation"] == "create"
                assert call.delivered is False
                assert call.tool_result_message_id is not None
                assert child.subagent_id is not None
                child_data.append(
                    {
                        "work_id": child.id,
                        "subagent_id": child.subagent_id,
                        "create_result_id": call.tool_result_message_id,
                        "create_arguments": deepcopy(call.arguments),
                        "spec": deepcopy(child.execution_state["agent_work"]),
                    }
                )
            slots = origin.execution_state.get("child_work_waits")
            assert isinstance(slots, list)
            assert {slot.get("child_work_id") for slot in slots} == {item["work_id"] for item in child_data}

        yield SimpleNamespace(
            runtime=runtime,
            origin_work_id=origin_work_id,
            children=child_data,
        )


def _wait_call(child: dict, *, call_id: str, with_work_id: bool) -> InternalToolCall:
    arguments = {"operation": "wait", "subagent_id": child["subagent_id"]}
    if with_work_id:
        arguments["work_id"] = child["work_id"]
    return InternalToolCall(id=call_id, name=AGENT_WORK_TOOL_NAME, arguments=arguments)


async def _start_wait_reply(
    runtime,
    *,
    child_data: list[dict],
    with_work_id: bool,
) -> tuple[int, list[InternalToolCall], set[str], InternalMessage]:
    wait_calls = [
        _wait_call(
            child,
            call_id=f"explicit-wait-call-{index}-{with_work_id}",
            with_work_id=with_work_id,
        )
        for index, child in enumerate(child_data)
    ]
    fallback = InternalMessage(role=MessageRole.ASSISTANT, content="explicit-wait-fallback")
    wait_work_id = await _submit(
        runtime,
        request_id=f"explicit-wait-followup-{with_work_id}",
        message="Wait for the selected child results before answering.",
    )
    runtime.outputs.extend([InternalMessage(role=MessageRole.ASSISTANT, tool_calls=wait_calls), fallback])
    provider_start = len(runtime.provider_payloads)
    waiting = await runtime.run_next(independent_only=False)
    assert waiting is not None
    assert waiting.id == wait_work_id
    assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert len(runtime.provider_payloads) == provider_start + 1
    assert len(runtime.outputs) == 1
    assert runtime.outputs[0] is fallback
    async with runtime.factory() as db:
        persisted_wait_calls = (
            (
                await db.execute(
                    select(SubagentToolCall)
                    .where(
                        SubagentToolCall.parent_work_id == wait_work_id,
                        SubagentToolCall.arguments["operation"].as_string() == "wait",
                    )
                    .order_by(SubagentToolCall.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(persisted_wait_calls) == len(wait_calls)
        actual_wait_call_ids: set[str] = set()
        for wait_call in wait_calls:
            matches = [call for call in persisted_wait_calls if call.arguments == wait_call.arguments]
            assert len(matches) == 1
            actual_wait_call_ids.add(matches[0].tool_call_id)
        assert len(actual_wait_call_ids) == len(wait_calls)
    return wait_work_id, wait_calls, actual_wait_call_ids, fallback


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("with_work_id", [False, True], ids=["wait-latest", "wait-explicit-work"])
@pytest.mark.asyncio
async def test_create_finishes_immediately_then_explicit_wait_delivers_both_replies(
    image_chat_runtime,
    protocol: str,
    with_work_id: bool,
) -> None:
    async with _create_case(image_chat_runtime, protocol=protocol, child_count=2) as case:
        runtime = case.runtime
        origin_work_id = case.origin_work_id
        wait_work_id, wait_calls, wait_call_ids, fallback = await _start_wait_reply(
            runtime,
            child_data=case.children,
            with_work_id=with_work_id,
        )

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, origin_work_id)
            waiting = await db.get(SessionReplyWorkItem, wait_work_id)
            assert origin is not None and waiting is not None
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            slots = waiting.execution_state.get("child_work_waits")
            assert isinstance(slots, list)
            assert {slot.get("tool_call_id") for slot in slots} == wait_call_ids
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id.in_([origin_work_id, wait_work_id])))).scalars().all()
            origin_calls = [call for call in calls if call.parent_work_id == origin_work_id]
            wait_rows = [call for call in calls if call.parent_work_id == wait_work_id]
            assert len(origin_calls) == 2
            assert len(wait_rows) == 2
            assert all(call.arguments["operation"] == "create" and call.delivered is False for call in origin_calls)
            assert {call.tool_call_id for call in wait_rows} == wait_call_ids
            for call in wait_rows:
                matches = [item for item in wait_calls if item.arguments == call.arguments]
                assert len(matches) == 1
            assert all(call.delivered is False and call.tool_result_message_id is not None for call in wait_rows)

        provider_count = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count
        assert runtime.outputs[0] is fallback
        saved_fallback = runtime.outputs.popleft()
        assert saved_fallback is fallback

        # A new foreground message is processed independently while the wait remains registered.
        independent_work_id = await _submit(
            runtime,
            request_id="explicit-wait-independent-message",
            message="Handle this separate message while the child wait remains active.",
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="independent-message-final"))
        independent_provider_count = len(runtime.provider_payloads)
        independent_done = await runtime.run_next(independent_only=False)
        assert independent_done is not None
        assert independent_done.id == independent_work_id
        assert independent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == independent_provider_count + 1

        for child in case.children:
            summary = f"explicit-wait-child-result-{child['work_id']}"
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=summary))
            child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        # Both result deliveries can be accumulated before either parent asks the model again.
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, content="origin-parent-after-wait"),
                InternalMessage(role=MessageRole.ASSISTANT, content="wait-parent-after-wait"),
            ]
        )

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, origin_work_id)
            waiting = await db.get(SessionReplyWorkItem, wait_work_id)
            assert origin is not None and waiting is not None
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id.in_([origin_work_id, wait_work_id])))).scalars().all()
            assert len(calls) == 4
            assert all(call.delivered is True for call in calls)
            assert len({call.tool_result_message_id for call in calls}) == 4
            for call in calls:
                assert call.tool_result_message_id is not None
                result_row = await db.get(Message, call.tool_result_message_id)
                assert result_row is not None
                stored_result, result_payload = _payload(result_row)
                assert stored_result.tool_call_id == call.tool_call_id
                assert result_payload["child_work_id"] == call.child_work_id
            for child in case.children:
                child_row = await db.get(SessionReplyWorkItem, child["work_id"])
                assert child_row is not None and child_row.status == SessionReplyWorkStatus.SUCCEEDED
                assert child_row.execution_state["agent_work"] == child["spec"]
            origin_calls = [call for call in calls if call.parent_work_id == origin_work_id]
            wait_rows = [call for call in calls if call.parent_work_id == wait_work_id]
            assert {call.child_work_id for call in origin_calls} == {item["work_id"] for item in case.children}
            assert {call.child_work_id for call in wait_rows} == {item["work_id"] for item in case.children}
            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT))).scalars().all()
            assert len(markers) == 4
            marker_snapshot = [(row.id, row.dedupe_key, row.content) for row in markers]

        resumed_ids = set()
        for _ in range(2):
            resumed = await runtime.run_next(independent_only=False)
            assert resumed is not None
            assert resumed.id in {origin_work_id, wait_work_id}
            assert resumed.status == SessionReplyWorkStatus.SUCCEEDED
            resumed_ids.add(resumed.id)
        assert resumed_ids == {origin_work_id, wait_work_id}

        async with runtime.factory() as db:
            notification_snapshot = await _notifications(db, runtime.session_id)

        async with runtime.factory() as db:
            assert await deliver_child_work_results(db, child_work_id=case.children[0]["work_id"]) == 0
            assert await resume_child_work_for_parent(db, parent_work_id=origin_work_id) is False
            assert await resume_child_work_for_parent(db, parent_work_id=wait_work_id) is False
            assert await reconcile_child_work(db) == 0
        async with runtime.factory() as db:
            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT))).scalars().all()
            assert [(row.id, row.dedupe_key, row.content) for row in markers] == marker_snapshot
            assert await _notifications(db, runtime.session_id) == notification_snapshot


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("operation", ["query", "list"])
@pytest.mark.asyncio
async def test_create_query_and_list_never_implicitly_suspend(
    image_chat_runtime,
    protocol: str,
    operation: str,
) -> None:
    async with _create_case(image_chat_runtime, protocol=protocol, child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        followup_work_id = await _submit(
            runtime,
            request_id=f"query-list-followup-{operation}-{protocol}",
            message="Inspect the child progress and answer immediately.",
        )
        if operation == "query":
            arguments = {
                "operation": "query",
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
            }
        else:
            arguments = {"operation": "list"}
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id=f"query-list-call-{operation}", name=AGENT_WORK_TOOL_NAME, arguments=arguments)],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content=f"{operation}-parent-final"),
            ]
        )
        provider_start = len(runtime.provider_payloads)
        done = await runtime.run_next(independent_only=False)
        assert done is not None
        assert done.id == followup_work_id
        assert done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == provider_start + 2
        assert not runtime.outputs

        async with runtime.factory() as db:
            followup = await db.get(SessionReplyWorkItem, followup_work_id)
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            child_row = await db.get(SessionReplyWorkItem, child["work_id"])
            assert followup is not None and origin is not None and child_row is not None
            assert followup.status == SessionReplyWorkStatus.SUCCEEDED
            assert not followup.execution_state.get("child_work_waits")
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert child_row.status == SessionReplyWorkStatus.READY_FOR_LLM
            followup_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == followup_work_id))).scalars().all()
            assert followup_calls == []
            origin_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().one()
            assert origin_call.arguments["operation"] == "create"
            assert origin_call.delivered is False
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_start + 2


@pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "cancelled"], ids=["success", "failure", "cancelled"])
@pytest.mark.asyncio
async def test_wait_returns_saved_terminal_result_immediately(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        child_summary = f"explicit-wait-terminal-{terminal_status}-child-summary"

        if terminal_status == "succeeded":
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_summary))
        elif terminal_status == "failed":
            from app.transformers.openai import OpenAIResponsesTransformer

            async with runtime.factory() as db:
                child_row = await db.get(SessionReplyWorkItem, child["work_id"])
                assert child_row is not None
                child_row.max_attempts = 1
                await db.commit()

            async def fail_child_provider(_transformer, **_kwargs):
                raise RuntimeError("explicit-wait-child-provider-failure")

            with monkeypatch.context() as patch:
                patch.setattr(OpenAIResponsesTransformer, "_post_json", fail_child_provider)
                child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.FAILED
        else:
            cancel_work_id = await _submit(
                runtime,
                request_id="explicit-wait-terminal-cancel-request",
                message="Cancel the selected child task, then answer normally.",
            )
            cancel_call = InternalToolCall(
                id="explicit-wait-terminal-cancel-call",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "cancel",
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                },
            )
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[cancel_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, content="explicit-wait-terminal-cancel-final"),
                ]
            )
            cancel_done = await runtime.run_next(independent_only=False)
            assert cancel_done is not None
            assert cancel_done.id == cancel_work_id
            assert cancel_done.status == SessionReplyWorkStatus.SUCCEEDED

        if terminal_status == "succeeded":
            child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            child_row = await db.get(SessionReplyWorkItem, child["work_id"])
            round_row = await db.get(SubagentWork, child["work_id"])
            assert child_row is not None and round_row is not None
            assert child_row.status == SessionReplyWorkStatus(terminal_status)
            assert round_row.result is not None
            assert round_row.result["completed"] is True
            assert round_row.result["status"] == terminal_status
            if terminal_status == "succeeded":
                assert round_row.result["result"]["content"] == child_summary

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="explicit-wait-terminal-origin-final"))
        origin_done = await runtime.run_next(independent_only=False)
        assert origin_done is not None
        assert origin_done.id == case.origin_work_id
        assert origin_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            create_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().all()
            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT))).scalars().all()
            stream_events = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == case.origin_work_id).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all()
            assert origin is not None and origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert not origin.execution_state.get("child_work_waits")
            assert len(create_calls) == 1
            create_call = create_calls[0]
            assert create_call.arguments["operation"] == "create"
            assert create_call.delivered is True
            assert create_call.tool_result_message_id is not None
            create_delivery = (create_call.id, create_call.tool_result_message_id, create_call.delivered)
            marker_snapshot = tuple((marker.id, marker.dedupe_key, marker.content) for marker in markers)
            origin_stream_snapshot = tuple((event.id, event.sequence_no, deepcopy(event.event)) for event in stream_events)
            origin_sequence_no = origin.sequence_no

        expected_result_status = "success" if terminal_status == "succeeded" else "failed"

        async def run_immediate_wait(request_id: str, call_id: str, final_content: str) -> tuple[int, dict]:
            wait_work_id = await _submit(
                runtime,
                request_id=request_id,
                message="Wait for the selected child result, then answer normally.",
            )
            wait_call = _wait_call(child, call_id=call_id, with_work_id=True)
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[wait_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, content=final_content),
                ]
            )
            provider_start = len(runtime.provider_payloads)
            wait_done = await runtime.run_next(independent_only=False)
            assert wait_done is not None
            assert wait_done.id == wait_work_id
            assert wait_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads) == provider_start + 2

            async with runtime.factory() as db:
                wait_rows = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == wait_work_id))).scalars().all()
                wait_matches = [row for row in wait_rows if row.arguments == wait_call.arguments]
                assert len(wait_matches) == 1
                actual_call_id = wait_matches[0].tool_call_id

            wait_results = []
            for payload in runtime.provider_payloads[provider_start:]:
                for item in payload.get("input", []):
                    if item.get("type") != "function_call_output" or item.get("call_id") != actual_call_id:
                        continue
                    wait_results.append(json.loads(item["output"]))
            assert len(wait_results) == 1
            wait_result = wait_results[0]
            assert wait_result["operation"] == "wait"
            assert wait_result["status"] == expected_result_status
            assert wait_result["work"]["status"] == terminal_status
            assert wait_result["work"]["completed"] is True
            if terminal_status == "succeeded":
                assert wait_result["work"]["result"]["content"] == child_summary

            async with runtime.factory() as db:
                waiting = await db.get(SessionReplyWorkItem, wait_work_id)
                wait_rows = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == wait_work_id))).scalars().all()
                wait_stream_events = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == wait_work_id).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all()
                assert waiting is not None and waiting.status == SessionReplyWorkStatus.SUCCEEDED
                assert not waiting.execution_state.get("child_work_waits")
                assert len(wait_rows) == 1
                assert wait_rows[0].arguments == wait_call.arguments
                assert wait_rows[0].delivered is True
                assert all(event.event.get("type") != "work_suspended" for event in wait_stream_events)
                result_rows = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == runtime.session_id,
                                Message.type == MessageType.TOOL_RESULT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                matching_results = []
                for result_row in result_rows:
                    stored_result, stored_payload = _payload(result_row)
                    if stored_result.tool_call_id == wait_rows[0].tool_call_id:
                        matching_results.append((result_row.id, stored_payload))
                assert len(matching_results) == 1
                stored_result_id, stored_payload = matching_results[0]
                assert stored_result_id == wait_rows[0].tool_result_message_id
                assert stored_payload == wait_result
            return wait_work_id, wait_result

        wait_work_id, wait_result = await run_immediate_wait(
            "explicit-wait-terminal-first-wait",
            "explicit-wait-terminal-first-call",
            "explicit-wait-terminal-first-final",
        )
        _repeat_work_id, repeat_result = await run_immediate_wait(
            "explicit-wait-terminal-repeat-wait",
            "explicit-wait-terminal-repeat-call",
            "explicit-wait-terminal-repeat-final",
        )
        assert _repeat_work_id != wait_work_id
        assert repeat_result == wait_result

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            child_row = await db.get(SessionReplyWorkItem, child["work_id"])
            round_row = await db.get(SubagentWork, child["work_id"])
            origin_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().all()
            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT))).scalars().all()
            stream_events = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == case.origin_work_id).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all()
            assert origin is not None and origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert origin.sequence_no == origin_sequence_no
            assert not origin.execution_state.get("child_work_waits")
            assert child_row is not None and child_row.status == SessionReplyWorkStatus(terminal_status)
            assert round_row is not None and round_row.result is not None and round_row.result["completed"] is True
            assert len(origin_calls) == 1
            assert (origin_calls[0].id, origin_calls[0].tool_result_message_id, origin_calls[0].delivered) == create_delivery
            assert tuple((marker.id, marker.dedupe_key, marker.content) for marker in markers) == marker_snapshot
            assert tuple((event.id, event.sequence_no, deepcopy(event.event)) for event in stream_events) == origin_stream_snapshot


@pytest.mark.parametrize(
    "save_interruption",
    ["before-save", "after-save", "after-result-staged"],
    ids=["cancel-before-tool-result-save", "cancel-after-tool-result-save", "cancel-after-result-staged"],
)
@pytest.mark.asyncio
async def test_completed_wait_result_recovers_across_tool_result_save_interruption(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    save_interruption: str,
) -> None:
    from importlib import import_module

    import app.core.dispatchers.interactive_tools as interactive_tools

    save_tool_response_module = import_module("app.core.utils.dispatcher.save_tool_response")

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        child_summary = "completed-wait-recovery-child-summary"

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_summary))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None and child_done.id == child["work_id"] and child_done.status == SessionReplyWorkStatus.SUCCEEDED

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="completed-wait-recovery-origin-final"))
        origin_done = await runtime.run_next(independent_only=False)
        assert origin_done is not None and origin_done.id == case.origin_work_id and origin_done.status == SessionReplyWorkStatus.SUCCEEDED

        wait_work_id = await _submit(
            runtime,
            request_id=f"completed-wait-recovery-{save_interruption}",
            message="Wait for the already completed child result, then answer.",
        )
        wait_call = _wait_call(child, call_id=f"completed-wait-recovery-call-{save_interruption}", with_work_id=True)
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[wait_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content="completed-wait-recovery-parent-final"),
            ]
        )
        provider_start = len(runtime.provider_payloads)
        real_save = interactive_tools.save_tool_response

        async def get_wait_result(db):
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == wait_work_id, SubagentToolCall.arguments == wait_call.arguments))).scalars().one()
            result_rows = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.TOOL_RESULT))).scalars().all()
            matches = []
            for result_row in result_rows:
                stored_result, payload = _payload(result_row)
                if stored_result.tool_call_id == call.tool_call_id:
                    matches.append((result_row, payload))
            assert len(matches) <= 1
            if not matches:
                assert call.tool_result_message_id is None
                return call, None, None
            result_row, payload = matches[0]
            assert call.tool_result_message_id == result_row.id
            return call, result_row, payload

        async def interrupt_save(*args, **kwargs):
            db = kwargs.get("db") or args[0]
            claim_info = db.info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
            tool_res = args[4]
            payload = json.loads(tool_res.content or "{}")
            if claim_info is None or claim_info[0] != wait_work_id or payload.get("status") != "success" or payload.get("operation") != "wait" or payload.get("work", {}).get("work_id") != child["work_id"]:
                return await real_save(*args, **kwargs)
            if save_interruption == "before-save":
                raise asyncio.CancelledError
            await real_save(*args, **kwargs)
            raise asyncio.CancelledError

        real_save_message = save_tool_response_module.save_message

        async def interrupt_after_result_staged(*args, **kwargs):
            stored = await real_save_message(*args, **kwargs)
            db = kwargs.get("db") or args[0]
            claim_info = db.info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
            if claim_info is not None and claim_info[0] == wait_work_id and kwargs.get("commit") is False:
                raise asyncio.CancelledError
            return stored

        with monkeypatch.context() as patch:
            if save_interruption == "after-result-staged":
                patch.setattr(save_tool_response_module, "save_message", interrupt_after_result_staged)
            else:
                patch.setattr(interactive_tools, "save_tool_response", interrupt_save)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=False)

        async with runtime.factory() as db:
            waiting = await db.get(SessionReplyWorkItem, wait_work_id)
            assert waiting is not None and waiting.status == SessionReplyWorkStatus.RUNNING
            wait_row, result_row, _ = await get_wait_result(db)
            wait_tool_call_id = wait_row.tool_call_id
            if save_interruption in {"before-save", "after-result-staged"}:
                assert result_row is None
                assert wait_row.tool_result_message_id is None
                assert wait_row.delivered is False
            else:
                assert result_row is not None

            waiting.lock_until = 0
            await db.commit()

        await SessionReplyConsumer()._recover_expired()

        recovered = None
        for _ in range(2):
            recovered = await runtime.run_next(independent_only=False)
            assert recovered is not None
            assert recovered.id == wait_work_id
            assert recovered.status in {
                SessionReplyWorkStatus.READY_FOR_LLM,
                SessionReplyWorkStatus.SUCCEEDED,
            }
            if recovered.status == SessionReplyWorkStatus.SUCCEEDED:
                break
        assert recovered is not None
        assert recovered.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == provider_start + 2

        wait_results = [json.loads(item["output"]) for payload in runtime.provider_payloads[provider_start:] for item in payload.get("input", []) if item.get("type") == "function_call_output" and item.get("call_id") == wait_tool_call_id]
        assert len(wait_results) == 1
        wait_result = wait_results[0]
        assert wait_result["status"] == "success"
        assert wait_result["operation"] == "wait"
        assert wait_result["work"]["status"] == "succeeded"
        assert wait_result["work"]["completed"] is True
        assert wait_result["work"]["result"]["content"] == child_summary

        async with runtime.factory() as db:
            waiting = await db.get(SessionReplyWorkItem, wait_work_id)
            wait_row, result_row, stored_payload = await get_wait_result(db)
            assert waiting is not None and waiting.status == SessionReplyWorkStatus.SUCCEEDED
            assert not waiting.execution_state.get("child_work_waits")
            assert wait_row.delivered is True and result_row is not None
            assert stored_payload == wait_result


@pytest.mark.parametrize("interruption", ["wait-result-saved", "suspend-before-commit"])
@pytest.mark.asyncio
async def test_explicit_wait_recovery_does_not_request_or_duplicate_calls(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    from app.core.crud.session.external_work import external_work_crud

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        wait_work_id = await _submit(
            runtime,
            request_id=f"explicit-wait-recovery-{interruption}",
            message="Wait for the child result, then answer.",
        )
        child = case.children[0]
        wait_call = _wait_call(child, call_id=f"recovery-wait-{interruption}", with_work_id=True)
        fallback = InternalMessage(role=MessageRole.ASSISTANT, content="recovery-fallback")
        runtime.outputs.extend([InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[wait_call]), fallback])

        if interruption == "wait-result-saved":
            import app.core.dispatchers.interactive_tools as interactive_tools

            real_save = interactive_tools.save_tool_response

            async def interrupt_after_wait_saved(*args, **kwargs):
                stored = await real_save(*args, **kwargs)
                db = kwargs.get("db") or args[0]
                claim_info = db.info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
                if claim_info is not None and claim_info[0] == wait_work_id:
                    _stored, payload = _payload(await db.get(Message, stored.id))
                    if payload.get("operation") == "wait":
                        raise asyncio.CancelledError
                return stored

            patch_target = (interactive_tools, "save_tool_response", interrupt_after_wait_saved)
        else:
            real_suspend = external_work_crud.suspend_claimed

            async def interrupt_suspend(db, *args, **kwargs):
                work = kwargs.get("work")
                if work is not None and work.id == wait_work_id:
                    if interruption == "suspend-before-commit":
                        staged = dict(kwargs)
                        staged["commit"] = False
                        await real_suspend(db, *args, **staged)
                    raise asyncio.CancelledError
                return await real_suspend(db, *args, **kwargs)

            patch_target = (external_work_crud, "suspend_claimed", interrupt_suspend)

        with monkeypatch.context() as patch:
            patch.setattr(*patch_target)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=False)

        async with runtime.factory() as db:
            waiting = await db.get(SessionReplyWorkItem, wait_work_id)
            assert waiting is not None
            assert waiting.status == SessionReplyWorkStatus.RUNNING
            waiting.lock_until = 0
            await db.commit()

        provider_count = len(runtime.provider_payloads)
        await SessionReplyConsumer()._recover_expired()
        assert len(runtime.provider_payloads) == provider_count
        recovered = await runtime.run_next(independent_only=False)
        assert recovered is not None
        assert recovered.id == wait_work_id
        assert recovered.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads) == provider_count
        assert len(runtime.outputs) == 1
        assert runtime.outputs[0] is fallback

        async with runtime.factory() as db:
            waiting = await db.get(SessionReplyWorkItem, wait_work_id)
            assert waiting is not None
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id.in_([case.origin_work_id, wait_work_id])))).scalars().all()
            assert len(calls) == 2
            create_call = next(call for call in calls if call.parent_work_id == case.origin_work_id)
            wait_matches = [call for call in calls if call.parent_work_id == wait_work_id and call.arguments == wait_call.arguments]
            assert len(wait_matches) == 1
            wait_row = wait_matches[0]
            assert create_call.tool_result_message_id == child["create_result_id"]
            assert create_call.delivered is False
            assert wait_row.arguments == wait_call.arguments
            assert wait_row.tool_result_message_id is not None
            assert wait_row.delivered is False
            slots = waiting.execution_state.get("child_work_waits")
            assert isinstance(slots, list)
            assert {slot.get("tool_call_id") for slot in slots} == {wait_row.tool_call_id}
            wait_result = await db.get(Message, wait_row.tool_result_message_id)
            assert wait_result is not None
            stored_wait, wait_payload = _payload(wait_result)
            assert stored_wait.tool_call_id == wait_row.tool_call_id
            assert wait_payload["operation"] == "wait"
            assert wait_payload["status"] == "queued"

        saved_fallback = runtime.outputs.popleft()
        runtime.outputs.appendleft(InternalMessage(role=MessageRole.ASSISTANT, content="recovery-child-final"))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None
        assert child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        runtime.outputs.appendleft(saved_fallback)

        async with runtime.factory() as db:
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id.in_([case.origin_work_id, wait_work_id])))).scalars().all()
            assert len(calls) == 2
            assert all(call.delivered is True for call in calls)
            assert len({call.tool_result_message_id for call in calls}) == 2
            wait_matches = [call for call in calls if call.parent_work_id == wait_work_id and call.arguments == wait_call.arguments]
            assert len(wait_matches) == 1
            wait_result = await db.get(Message, wait_matches[0].tool_result_message_id)
            assert wait_result is not None
            stored_wait, wait_payload = _payload(wait_result)
            assert stored_wait.tool_call_id == wait_matches[0].tool_call_id
            assert wait_payload["operation"] == "wait"
            assert wait_payload["status"] == "success"
