import asyncio
import base64
import json
from contextlib import aclosing, asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from sqlalchemy import select

import app.core.background_tasks.runner as background_runner_module
import app.core.dispatchers.interactive_runtime as interactive_runtime_module
import app.core.session_reply_queue.child_work as child_work_module
from app.api.v1 import chat as chat_api
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    END_SESSION_TOOL_NAME,
    ERR_SUBAGENT_CANCELLED,
    ERR_SUBAGENT_RESULT_NOT_READY,
    SEND_FILE_TO_USER_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.todo import session_todo_crud
from app.core.i18n import t
from app.core.paths import get_user_temp_dir
from app.core.prompts import SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT
from app.core.session_reply_queue.child_delivery import deliver_child_work_results, resume_child_work_for_parent
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.tools.send_file_to_user import _encode_token
from app.core.utils.dispatcher.process_single_tool import get_tool_execution_status
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalGeneratedImage, InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork


def _payload(row: Message) -> tuple[InternalMessage, dict]:
    message = InternalMessage.model_validate_json(row.content or "{}")
    value = json.loads(message.content or "{}")
    assert isinstance(value, dict)
    return message, value


async def _persisted_main_tool_call_id(db, *, session_id: str, uid: str, profile_id: int, tool_call: InternalToolCall) -> str:
    assistant_rows = (
        (
            await db.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.uid == uid,
                    Message.profile_id == profile_id,
                    Message.work_id.is_(None),
                    Message.role == MessageRole.ASSISTANT,
                    Message.type == MessageType.TOOL_CALL,
                )
            )
        )
        .scalars()
        .all()
    )
    persisted_calls = []
    for assistant_row in assistant_rows:
        assistant_message = InternalMessage.model_validate_json(assistant_row.content or "{}")
        persisted_calls.extend(call for call in assistant_message.tool_calls or [] if call.name == tool_call.name and call.arguments == tool_call.arguments)
    assert len(persisted_calls) == 1
    persisted_call = persisted_calls[0]
    assert persisted_call.name == tool_call.name
    assert persisted_call.arguments == tool_call.arguments
    assert isinstance(persisted_call.id, str) and persisted_call.id
    return persisted_call.id


async def _notification_snapshot(db, *, session_id: str) -> tuple[tuple, tuple]:
    markers = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.type == MessageType.SUBAGENT_RESULT,
                )
                .order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )
    events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all()
    return (
        tuple((marker.id, marker.dedupe_key, marker.content) for marker in markers),
        tuple((event.id, event.dedupe_key, deepcopy(event.event)) for event in events),
    )


async def _board_snapshot(db, *, subagent_id: str, child_work_id: int) -> tuple[tuple, ...]:
    posts = (
        (
            await db.execute(
                select(SubagentBoardPost)
                .where(
                    SubagentBoardPost.subagent_id == subagent_id,
                    SubagentBoardPost.child_work_id == child_work_id,
                )
                .order_by(SubagentBoardPost.id)
            )
        )
        .scalars()
        .all()
    )
    return tuple((post.id, post.subagent_id, post.child_work_id, post.stage, post.content) for post in posts)


def _provider_agent_work_operations(payload: dict) -> list[str] | None:
    for tool in payload.get("tools", []):
        if not isinstance(tool, dict):
            continue
        definition = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if definition.get("name") != AGENT_WORK_TOOL_NAME:
            continue
        parameters = definition.get("parameters")
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        operation = properties.get("operation") if isinstance(properties, dict) else None
        operations = operation.get("enum") if isinstance(operation, dict) else None
        if isinstance(operations, list) and all(isinstance(value, str) for value in operations):
            return operations
    return None


def _provider_function_call_output_payloads(payload: dict, *, name: str, arguments: dict) -> list[dict]:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return []

    function_calls = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call" or item.get("name") != name:
            continue
        raw_arguments = item.get("arguments")
        if isinstance(raw_arguments, str):
            try:
                parsed_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                continue
        elif isinstance(raw_arguments, dict):
            parsed_arguments = raw_arguments
        else:
            continue
        if parsed_arguments == arguments:
            function_calls.append(item)
    assert len(function_calls) == 1

    call_id = function_calls[0].get("call_id") or function_calls[0].get("id")
    assert isinstance(call_id, str) and call_id
    output_payloads = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output" or item.get("call_id") != call_id:
            continue
        output = item.get("output")
        if not isinstance(output, str):
            continue
        try:
            output_payload, _ = json.JSONDecoder().raw_decode(output)
        except json.JSONDecodeError:
            continue
        if isinstance(output_payload, dict):
            output_payloads.append(output_payload)
    return output_payloads


async def _enable_agent_work(runtime) -> None:
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
        profile.configs = configs
        await db.commit()


async def _submit(runtime, *, request_id: str, message: str, expected_status: str = "accepted") -> int:
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
    assert data["submission_status"] == expected_status
    return data["work_id"]


async def _next_done_event(stream, *, work_id: int, content: str, events: list[dict] | None = None) -> None:
    async with asyncio.timeout(5):
        while True:
            event = await anext(stream)
            if events is not None:
                events.append(event)
            if event.get("type") != "done":
                continue
            assert event["work_id"] == work_id
            assert event["response"]["choices"][0]["message"]["content"] == content
            return


@asynccontextmanager
async def _create_case(
    image_chat_runtime,
    *,
    protocol: str,
    child_count: int = 1,
    stream_delivery: bool = False,
    show_tool_calls: bool = False,
    extra_tool_calls: list[InternalToolCall] | None = None,
):
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        await _enable_agent_work(runtime)
        create_calls = [
            InternalToolCall(
                id=f"inspection-create-{index}",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "create",
                    "name": f"inspection-child-{index}",
                    "message": f"inspection-child-private-secret-{index}",
                },
            )
            for index in range(child_count)
        ]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[*(extra_tool_calls or []), *create_calls]))
        if show_tool_calls:
            async with runtime.factory() as db:
                session = await db.get(ChatSession, runtime.session_id)
                assert session is not None
                session.show_tool_calls = True
                await db.commit()
        origin_work_id = await _submit(
            runtime,
            request_id="inspection-origin-request",
            message="Create independent child tasks, then wait for their results before answering the parent.",
        )
        if stream_delivery:
            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, origin_work_id)
                assert origin is not None
                execution_state = deepcopy(origin.execution_state)
                execution_state["stream_requested"] = True
                origin.execution_state = execution_state
                await db.commit()
        provider_start = len(runtime.provider_payloads)
        origin_done = await runtime.run_next(independent_only=False)
        assert origin_done is not None
        assert origin_done.id == origin_work_id
        assert origin_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads) == provider_start + 1
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
            assert origin.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert origin.result_message_id is None
            assert origin.locked_by is None
            assert origin.lock_until is None
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


@pytest.mark.asyncio
async def test_websocket_subagent_resume_does_not_replay_delivered_parent_stream_events(image_chat_runtime) -> None:
    child_final = "inspection-child-final-for-websocket-resume"
    parent_final = "inspection-parent-final-for-websocket-resume"

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.resume_completes: asyncio.Queue[dict] = asyncio.Queue()

        async def send_json(self, event: dict) -> None:
            self.sent.append(dict(event))
            if event.get("type") == "resume_complete":
                await self.resume_completes.put(dict(event))

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", stream_delivery=True, show_tool_calls=True) as case:
        runtime = case.runtime
        child = case.children[0]
        socket = _Socket()
        state = chat_api._WebSocketChatState(
            current_session_id=runtime.session_id,
            current_owner_uid=runtime.uid,
        )
        try:
            chat_api._wake_websocket_stream(socket, state)
            await asyncio.wait_for(socket.resume_completes.get(), timeout=5)

            first_events = list(socket.sent)
            first_parent_events = [event for event in first_events if event.get("work_id") == case.origin_work_id]
            assert any(event.get("type") == "turn_end" for event in first_parent_events)
            assert any(event.get("type") == "work_suspended" for event in first_parent_events)
            first_parent_sequences = [event["event_sequence_no"] for event in first_parent_events if "event_sequence_no" in event]
            assert first_parent_sequences
            assert first_parent_sequences == sorted(first_parent_sequences)

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert parent_done is not None
            assert parent_done.id == case.origin_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED

            second_start = len(socket.sent)
            chat_api._wake_websocket_stream(socket, state)
            await asyncio.wait_for(socket.resume_completes.get(), timeout=5)
            second_events = socket.sent[second_start:]
            second_parent_events = [event for event in second_events if event.get("work_id") == case.origin_work_id]
            second_parent_sequences = [event["event_sequence_no"] for event in second_parent_events if "event_sequence_no" in event]

            second_turn_end = next(event for event in second_parent_events if event.get("type") == "turn_end")
            second_done = next(event for event in second_parent_events if event.get("type") == "done")
            assert second_turn_end["content"] == parent_final
            assert second_done["response"]["choices"][0]["message"]["content"] == parent_final
            assert second_parent_sequences == sorted(second_parent_sequences)
            assert min(second_parent_sequences) > max(first_parent_sequences)
        finally:
            await asyncio.wait_for(chat_api._cancel_websocket_stream_task(state), timeout=5)


@pytest.mark.parametrize("board_ready", [False, True], ids=["parent-waiting", "board-ready-unclaimed"])
@pytest.mark.asyncio
async def test_user_message_preempts_waiting_or_ready_subagent_parent_reply(
    image_chat_runtime,
    board_ready: bool,
) -> None:
    user_final = "inspection-user-reply-priority"
    parent_final = "inspection-parent-reply-after-user-priority"
    child_final = "inspection-child-reply-priority"

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1, stream_delivery=board_ready) as case:
        runtime = case.runtime
        child = case.children[0]
        consumed_events: list[dict] = []
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            suspended_sequence_no = parent.sequence_no

        assert suspended_sequence_no is not None

        async def _finish_child() -> None:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            async with asyncio.timeout(5):
                child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with aclosing(
            session_reply_queue_manager.wait_for_session_stream(
                uid=runtime.uid,
                session_id=runtime.session_id,
                after_work_sequence_no=suspended_sequence_no,
                interactive_only=True,
            )
        ) as session_stream:
            if board_ready:
                await _finish_child()

                async with asyncio.timeout(5):
                    while True:
                        event = await anext(session_stream)
                        consumed_events.append(event)
                        if event.get("type") != "work_suspended":
                            continue
                        assert event["work_id"] == case.origin_work_id
                        assert event["work_sequence_no"] == suspended_sequence_no
                        break

            async with asyncio.timeout(5):
                user_work_id = await _submit(
                    runtime,
                    request_id=f"subagent-user-priority-{board_ready}",
                    message="Answer this new user message independently and immediately.",
                    expected_status="accepted",
                )
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))
            async with asyncio.timeout(5):
                user_done = await runtime.run_next(independent_only=False)
            assert user_done is not None
            assert user_done.id == user_work_id
            assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
            await _next_done_event(session_stream, work_id=user_work_id, content=user_final, events=consumed_events)

            if not board_ready:
                await _finish_child()

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            async with asyncio.timeout(5):
                parent_done = await runtime.run_next(independent_only=False)
            assert parent_done is not None
            assert parent_done.id == case.origin_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            await _next_done_event(session_stream, work_id=case.origin_work_id, content=parent_final, events=consumed_events)
            async with asyncio.timeout(5):
                with pytest.raises(StopAsyncIteration):
                    await anext(session_stream)
            assert not runtime.outputs

            if board_ready:
                parent_stream_events = [event for event in consumed_events if event.get("work_id") == case.origin_work_id]
                parent_event_sequences = [event["event_sequence_no"] for event in parent_stream_events if "event_sequence_no" in event]
                assert parent_event_sequences == sorted(parent_event_sequences)
                assert len(parent_event_sequences) == len(set(parent_event_sequences))

                parent_turn_end_indices = [index for index, event in enumerate(consumed_events) if event.get("work_id") == case.origin_work_id and event.get("type") == "turn_end"]
                assert parent_turn_end_indices
                assert any(event.get("work_id") == case.origin_work_id and event.get("type") == "turn_end" and event.get("content") == parent_final for event in consumed_events)

                done_events = [event for event in consumed_events if event.get("type") == "done"]
                assert [event.get("work_id") for event in done_events] == [user_work_id, case.origin_work_id]
                user_done, parent_done_event = done_events
                assert user_done["request_ids"] == [f"subagent-user-priority-{board_ready}"]
                assert parent_done_event["request_ids"] == ["inspection-origin-request"]
                assert user_done["request_ids"] != parent_done_event["request_ids"]
                user_done_index = consumed_events.index(user_done)
                assert user_done_index < parent_turn_end_indices[-1] < consumed_events.index(parent_done_event)


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("with_work_id", [False, True], ids=["wait-latest", "wait-explicit-work"])
@pytest.mark.asyncio
async def test_removed_wait_is_rejected_without_suspending_foreground_reply(
    image_chat_runtime,
    protocol: str,
    with_work_id: bool,
) -> None:
    async with _create_case(image_chat_runtime, protocol=protocol, child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        wait_arguments = {"operation": "wait", "subagent_id": child["subagent_id"]}
        if with_work_id:
            wait_arguments["work_id"] = child["work_id"]
        wait_call = InternalToolCall(
            id=f"inspection-removed-wait-{with_work_id}",
            name=AGENT_WORK_TOOL_NAME,
            arguments=wait_arguments,
        )
        followup_work_id = await _submit(
            runtime,
            request_id=f"inspection-removed-wait-{protocol}-{with_work_id}",
            message="Inspect the selected child and answer this foreground message immediately.",
        )
        final_content = "foreground-reply-after-removed-wait"
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[wait_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=final_content),
            ]
        )
        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            child_row = await db.get(SessionReplyWorkItem, child["work_id"])
            subagent = await db.get(Subagent, child["subagent_id"])
            round_row = await db.get(SubagentWork, child["work_id"])
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == child["subagent_id"]).order_by(SubagentToolCall.id))).scalars().all()
            assert origin is not None and child_row is not None and subagent is not None and round_row is not None
            origin_snapshot = origin.model_dump(mode="json")
            child_snapshot = child_row.model_dump(mode="json")
            subagent_snapshot = subagent.model_dump(mode="json")
            round_snapshot = round_row.model_dump(mode="json")
            call_snapshots = [call.model_dump(mode="json") for call in calls]

        provider_start = len(runtime.provider_payloads)
        done = await runtime.run_next(independent_only=False)
        assert done is not None
        assert done.id == followup_work_id
        assert done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == provider_start + 2
        assert not runtime.outputs
        for payload in runtime.provider_payloads[provider_start:]:
            operations = _provider_agent_work_operations(payload)
            assert operations is not None
            assert "wait" not in operations

        async with runtime.factory() as db:
            followup = await db.get(SessionReplyWorkItem, followup_work_id)
            origin_after = await db.get(SessionReplyWorkItem, case.origin_work_id)
            child_after = await db.get(SessionReplyWorkItem, child["work_id"])
            subagent_after = await db.get(Subagent, child["subagent_id"])
            round_after = await db.get(SubagentWork, child["work_id"])
            calls_after = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == child["subagent_id"]).order_by(SubagentToolCall.id))).scalars().all()
            assert followup is not None and origin_after is not None and child_after is not None and subagent_after is not None and round_after is not None
            assert followup.status == SessionReplyWorkStatus.SUCCEEDED
            assert not followup.execution_state.get("child_work_waits")
            assert origin_after.model_dump(mode="json") == origin_snapshot
            assert child_after.model_dump(mode="json") == child_snapshot
            assert subagent_after.model_dump(mode="json") == subagent_snapshot
            assert round_after.model_dump(mode="json") == round_snapshot
            assert [call.model_dump(mode="json") for call in calls_after] == call_snapshots
            actual_wait_call_id = await _persisted_main_tool_call_id(
                db,
                session_id=runtime.session_id,
                uid=runtime.uid,
                profile_id=runtime.profile.id,
                tool_call=wait_call,
            )

            final_message = await db.get(Message, followup.result_message_id)
            assert final_message is not None
            assert final_message.content == final_content

            rejected_results = []
            result_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            for result_row in result_rows:
                result, payload = _payload(result_row)
                if result.tool_call_id == actual_wait_call_id:
                    rejected_results.append((result, payload))
            assert len(rejected_results) == 1
            rejected_result, rejected_payload = rejected_results[0]
            assert get_tool_execution_status(rejected_result) == "failed"
            assert rejected_payload["status"] == "failed"
            rejected_dump = json.dumps(rejected_payload, ensure_ascii=False)
            assert child["subagent_id"] not in rejected_dump
            assert child["create_arguments"]["message"] not in rejected_dump
            followup_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == followup_work_id))).scalars().all()
            assert followup_calls == []


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("operation", ["query_result", "list", "board_read"])
@pytest.mark.asyncio
async def test_inspection_operations_do_not_suspend_an_unrelated_foreground_reply(
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
        if operation == "query_result":
            arguments = {
                "operation": "query_result",
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
            }
        elif operation == "board_read":
            arguments = {
                "operation": "board_read",
                "subagent_id": child["subagent_id"],
            }
        else:
            arguments = {"operation": "list"}
        inspection_call = InternalToolCall(id=f"query-list-call-{operation}", name=AGENT_WORK_TOOL_NAME, arguments=arguments)
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[inspection_call],
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
            assert origin.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert child_row.status == SessionReplyWorkStatus.READY_FOR_LLM
            followup_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == followup_work_id))).scalars().all()
            assert followup_calls == []
            origin_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().one()
            assert origin_call.arguments["operation"] == "create"
            assert origin_call.delivered is False
            actual_inspection_call_id = await _persisted_main_tool_call_id(
                db,
                session_id=runtime.session_id,
                uid=runtime.uid,
                profile_id=runtime.profile.id,
                tool_call=inspection_call,
            )
            result_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            inspection_results = []
            for result_row in result_rows:
                result, payload = _payload(result_row)
                if result.tool_call_id == actual_inspection_call_id:
                    inspection_results.append(payload)
            assert len(inspection_results) == 1
            inspection_result = inspection_results[0]
            if operation == "query_result":
                assert inspection_result == {"error": t(ERR_SUBAGENT_RESULT_NOT_READY)}
            elif operation == "list":
                assert inspection_result == {
                    "subagents": [
                        {
                            "subagent_id": child["subagent_id"],
                            "work_id": child["work_id"],
                            "name": child["create_arguments"]["name"],
                            "status": "ready_for_llm",
                        }
                    ]
                }
            else:
                assert inspection_result == {"posts": []}
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_start + 2


@pytest.mark.parametrize("sibling_state", ["none", "waiting", "ready"], ids=["no-sibling", "sibling-waiting", "sibling-ready"])
@pytest.mark.asyncio
async def test_send_message_takes_over_unclaimed_child_report(image_chat_runtime, sibling_state: str) -> None:
    old_child_final = "old-child-result"
    sibling_final = "sibling-child-result"
    new_child_final = "new-child-result"
    new_reply_final = "new-reply-result"
    new_answer = "4 * 2 = 8"
    old_sibling_reply = "old-reply-reports-sibling"
    board_stage = "old-child-check"
    board_content = "The old child recorded its first verified check before finishing."
    user_message = "What is 4 * 2, and continue the same child with the next dependent task."
    send_message_instruction = "Continue the same child with the next dependent task."
    child_count = 1 if sibling_state == "none" else 2

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=child_count, show_tool_calls=True) as case:
        runtime = case.runtime
        old_child = case.children[0]
        sibling = case.children[1] if child_count == 2 else None

        board_call = InternalToolCall(
            id=f"send-message-old-board-post-{sibling_state}",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "board_post",
                "stage": board_stage,
                "content": board_content,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[board_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=old_child_final),
            ]
        )
        old_child_done = await runtime.run_next(independent_only=True)
        assert old_child_done is not None
        assert old_child_done.id == old_child["work_id"]
        assert old_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        if sibling_state == "ready":
            assert sibling is not None
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=sibling_final))
            sibling_done = await runtime.run_next(independent_only=True)
            assert sibling_done is not None
            assert sibling_done.id == sibling["work_id"]
            assert sibling_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            old_child_row = await db.get(SessionReplyWorkItem, old_child["work_id"])
            assert origin is not None and old_child_row is not None
            assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert old_child_row.status == SessionReplyWorkStatus.SUCCEEDED
            if sibling is not None:
                sibling_row = await db.get(SessionReplyWorkItem, sibling["work_id"])
                assert sibling_row is not None
                assert sibling_row.status == (SessionReplyWorkStatus.SUCCEEDED if sibling_state == "ready" else SessionReplyWorkStatus.READY_FOR_LLM)
            old_board_before_takeover = await _board_snapshot(
                db,
                subagent_id=old_child["subagent_id"],
                child_work_id=old_child["work_id"],
            )
            assert any(post[3:] == (board_stage, board_content) for post in old_board_before_takeover)
            assert any(post[3] == "succeeded" and old_child_final in post[4] for post in old_board_before_takeover)

        new_work_id = await _submit(
            runtime,
            request_id=f"send-message-takeover-{sibling_state}",
            message=user_message,
            expected_status="accepted",
        )
        assert new_work_id != case.origin_work_id
        async with runtime.factory() as db:
            new_work = await db.get(SessionReplyWorkItem, new_work_id)
            assert new_work is not None
            execution_state = deepcopy(new_work.execution_state)
            execution_state["stream_requested"] = True
            new_work.execution_state = execution_state
            await db.commit()

        send_arguments = {
            "operation": "send_message",
            "subagent_id": old_child["subagent_id"],
            "message": send_message_instruction,
        }
        send_call = InternalToolCall(
            id=f"send-message-takeover-call-{sibling_state}",
            name=AGENT_WORK_TOOL_NAME,
            arguments=send_arguments,
        )
        provider_before_send = len(runtime.provider_payloads)
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content=new_answer,
                tool_calls=[send_call],
            )
        )
        new_waiting = await runtime.run_next(independent_only=False)
        assert new_waiting is not None
        assert new_waiting.id == new_work_id
        assert new_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads) == provider_before_send + 1

        async with runtime.factory() as db:
            stream_rows = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == new_work_id).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all()
        stream_events = [row.event for row in stream_rows]
        answer_turns = [event for event in stream_events if event.get("type") == "turn_end" and event.get("content") == new_answer]
        send_tool_events = [event for event in stream_events if event.get("type") == "tool_start" and event.get("name") == AGENT_WORK_TOOL_NAME]
        assert len(answer_turns) == 1
        assert len(send_tool_events) == 1
        assert answer_turns[0].get("response_id") == send_tool_events[0].get("response_id")

        old_status_response = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
        assert old_status_response.status_code == 200
        old_status_data = old_status_response.json()["data"]
        if sibling_state == "none":
            assert old_status_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
            assert old_status_data["result_message_id"] is None
            assert old_status_data["response"]["skip_reply"] is True
        elif sibling_state == "waiting":
            assert old_status_data["status"] == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value
            assert old_status_data["response"] is None
            provider_after_send = len(runtime.provider_payloads)
            assert await runtime.run_next(independent_only=False) is None
            assert len(runtime.provider_payloads) == provider_after_send
        else:
            assert old_status_data["status"] == SessionReplyWorkStatus.READY_FOR_LLM.value
            assert old_status_data["response"] is None

        async with runtime.factory() as db:
            send_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == new_work_id))).scalars().one()
            assert send_call_row.arguments == send_arguments
            assert send_call_row.tool_result_message_id is not None
            assert send_call_row.child_work_id != old_child["work_id"]
            new_child = await db.get(SessionReplyWorkItem, send_call_row.child_work_id)
            assert new_child is not None
            assert new_child.status == SessionReplyWorkStatus.READY_FOR_LLM
            new_child_id = send_call_row.child_work_id
            if sibling_state == "ready":
                assert sibling is not None
                checkpoint_metadata = await session_reply_work_item_crud.get_checkpoint_metadata(db, case.origin_work_id)
                assert checkpoint_metadata is not None
                pending_ids = checkpoint_metadata.get("pending_resume_message_ids")
                assert isinstance(pending_ids, list)
                pending_work_ids = []
                pending_result_contents = []
                for pending_id in pending_ids:
                    pending_message = await db.get(Message, pending_id)
                    assert pending_message is not None
                    pending_payload = json.loads(pending_message.content or "{}")
                    if pending_payload.get("type") == "subagent_board_update":
                        work_id = pending_payload.get("work_id")
                        if isinstance(work_id, int) and not isinstance(work_id, bool):
                            pending_work_ids.append(work_id)
                        continue
                    if pending_payload.get("type") != "external_work_resume":
                        continue
                    for result in pending_payload.get("results", []):
                        if not isinstance(result, dict):
                            continue
                        work_id = result.get("work_id")
                        if isinstance(work_id, int) and not isinstance(work_id, bool):
                            pending_work_ids.append(work_id)
                        result_content = result.get("content")
                        if isinstance(result_content, str):
                            pending_result_contents.append(result_content)
                assert pending_work_ids == [sibling["work_id"]]
                assert sibling_final in pending_result_contents

        if sibling_state == "waiting":
            assert sibling is not None
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=sibling_final))
            sibling_done = await runtime.run_next(independent_only=True)
            assert sibling_done is not None
            assert sibling_done.id == sibling["work_id"]
            assert sibling_done.status == SessionReplyWorkStatus.SUCCEEDED

        if sibling is not None:
            old_report_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=old_sibling_reply))
            old_report_done = await runtime.run_next(independent_only=False)
            assert old_report_done is not None
            assert old_report_done.id == case.origin_work_id
            assert old_report_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert sibling_final in json.dumps(runtime.provider_payloads[old_report_provider_start:], ensure_ascii=False)

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_child_final))
        new_child_done = await runtime.run_next(independent_only=True)
        assert new_child_done is not None
        assert new_child_done.id == new_child_id
        assert new_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        new_reply_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_reply_final))
        new_reply_done = await runtime.run_next(independent_only=False)
        assert new_reply_done is not None
        assert new_reply_done.id == new_work_id
        assert new_reply_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == new_reply_provider_start + 1
        assert new_child_final in json.dumps(runtime.provider_payloads[new_reply_provider_start:], ensure_ascii=False)

        async with runtime.factory() as db:
            old_create_call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == case.origin_work_id,
                            SubagentToolCall.child_work_id == old_child["work_id"],
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert old_create_call.arguments["operation"] == "create"
            assert old_create_call.tool_result_message_id == old_child["create_result_id"]
            old_create_result = await db.get(Message, old_create_call.tool_result_message_id)
            assert old_create_result is not None
            old_create_message, old_create_payload = _payload(old_create_result)
            assert old_create_message.tool_call_id == old_create_call.tool_call_id
            assert old_create_message.tool_execution_status == "succeeded"
            assert old_create_payload == {
                "subagent_id": old_child["subagent_id"],
                "work_id": old_child["work_id"],
                "status": "succeeded",
                "content": old_child_final,
            }

            send_result = await db.get(Message, send_call_row.tool_result_message_id)
            assert send_result is not None
            send_result_message, send_result_payload = _payload(send_result)
            assert send_result_message.tool_call_id == send_call_row.tool_call_id
            assert send_result_message.tool_execution_status == "succeeded"
            assert send_result_payload == {
                "subagent_id": old_child["subagent_id"],
                "work_id": new_child_id,
                "status": "succeeded",
                "content": new_child_final,
            }

            assert (
                await _board_snapshot(
                    db,
                    subagent_id=old_child["subagent_id"],
                    child_work_id=old_child["work_id"],
                )
                == old_board_before_takeover
            )
            if sibling_state == "none":
                session_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id))).scalars().all()
                old_done_events = [event.event for event in session_events if event.event.get("type") == "done" and event.event.get("work_id") == case.origin_work_id]
                assert len(old_done_events) == 1
                assert old_done_events[0]["skip_reply"] is True

        provider_before_empty = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_before_empty
        if sibling_state == "none":
            async with asyncio.timeout(5):
                origin_stream_events = [event async for event in session_reply_queue_manager.wait_for_stream(case.origin_work_id)]
            origin_done_events = [event for event in origin_stream_events if event.get("type") == "done"]
            assert len(origin_done_events) == 1
            origin_done = origin_done_events[0]
            assert origin_done["work_id"] == case.origin_work_id
            assert origin_done["response"]["skip_reply"] is True
            assert "message_id" not in origin_done
            assert not origin_done["response"].get("choices")


@pytest.mark.parametrize("child_finished", [False, True], ids=["child-pending", "child-finished"])
@pytest.mark.asyncio
async def test_send_message_preserves_prepared_file_delivery(image_chat_runtime, tmp_path, child_finished: bool) -> None:
    file_name = "prepared-report.txt"
    file_bytes = b"prepared file delivery"
    file_path = tmp_path / file_name
    file_path.write_bytes(file_bytes)
    file_call = InternalToolCall(
        id=f"prepared-file-call-{child_finished}",
        name="send_file_to_user",
        arguments={"files": [{"path": str(file_path), "display_name": file_name, "mime_type": "text/plain"}]},
    )
    child_final = "original-child-result"
    new_child_final = "new-child-result"
    old_final = "original-reply-delivered-file"
    new_parent_final = "new-parent-reply"

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", extra_tool_calls=[file_call]) as case:
        runtime = case.runtime
        child = case.children[0]

        if child_finished:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None and child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert origin is not None
                assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM

        new_work_id = await _submit(
            runtime,
            request_id=f"prepared-file-followup-{child_finished}",
            message="Continue the same child from another parent reply.",
        )
        send_arguments = {
            "operation": "send_message",
            "subagent_id": child["subagent_id"],
            "message": "Continue and report the child result.",
        }
        send_call = InternalToolCall(
            id=f"prepared-file-send-call-{child_finished}",
            name=AGENT_WORK_TOOL_NAME,
            arguments=send_arguments,
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call]))
        new_waiting = await runtime.run_next(independent_only=False)
        assert new_waiting is not None and new_waiting.id == new_work_id
        assert new_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            send_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == new_work_id))).scalars().one()
            new_child_id = send_call_row.child_work_id
            if child_finished:
                assert new_child_id != child["work_id"]
            else:
                assert new_child_id == child["work_id"]
            new_child = await db.get(SessionReplyWorkItem, new_child_id)
            assert new_child is not None and new_child.status == SessionReplyWorkStatus.READY_FOR_LLM

        old_status = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
        assert old_status.status_code == 200
        old_data = old_status.json()["data"]
        assert old_data["status"] == SessionReplyWorkStatus.READY_FOR_LLM.value
        assert old_data["response"] is None

        old_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=old_final))
        old_done = await runtime.run_next(independent_only=False)
        assert old_done is not None and old_done.id == case.origin_work_id
        assert old_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == old_provider_start + 1
        assert SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT.splitlines()[0] not in json.dumps(runtime.provider_payloads[old_provider_start:], ensure_ascii=False)

        old_status = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
        assert old_status.status_code == 200
        old_data = old_status.json()["data"]
        assert old_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert old_data["result_message_id"] is not None
        response = old_data["response"]
        assert isinstance(response, dict) and response.get("skip_reply") is not True
        response_files = response["files"]
        assert len(response_files) == 1 and response_files[0]["name"] == file_name
        assert json.loads(response["choices"][0]["message"]["content"]) == {
            "type": "assistant_files",
            "text": old_final,
            "files": response_files,
        }
        download = await runtime.client.get(response_files[0]["download_url"])
        assert download.status_code == 200 and download.content == file_bytes
        old_provider_end = len(runtime.provider_payloads)

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_child_final))
        new_child_done = await runtime.run_next(independent_only=True)
        assert new_child_done is not None and new_child_done.id == new_child_id
        assert new_child_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == old_provider_end + 1
        old_status_after_child = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
        assert old_status_after_child.status_code == 200
        assert old_status_after_child.json()["data"]["status"] == SessionReplyWorkStatus.SUCCEEDED.value

        new_parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_parent_final))
        new_parent_done = await runtime.run_next(independent_only=False)
        assert new_parent_done is not None and new_parent_done.id == new_work_id
        assert new_parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == new_parent_provider_start + 1
        new_parent_dump = json.dumps(runtime.provider_payloads[new_parent_provider_start:], ensure_ascii=False)
        assert new_child_final in new_parent_dump
        assert new_child_final not in json.dumps(runtime.provider_payloads[old_provider_start:old_provider_end], ensure_ascii=False)

        async with runtime.factory() as db:
            old_create_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().one()
            assert old_create_call.arguments["operation"] == "create"
            assert old_create_call.delivered is True
            assert old_create_call.tool_result_message_id == child["create_result_id"]
            old_create_result = await db.get(Message, old_create_call.tool_result_message_id)
            assert old_create_result is not None
            _, old_create_payload = _payload(old_create_result)
            assert old_create_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": new_child_final if not child_finished else child_final,
            }

            send_result = await db.get(Message, send_call_row.tool_result_message_id)
            assert send_result is not None
            _, send_payload = _payload(send_result)
            assert send_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": new_child_id,
                "status": "succeeded",
                "content": new_child_final,
            }

        assert await runtime.run_next(independent_only=False) is None


@pytest.mark.parametrize(
    ("protocol", "long_report"),
    [
        pytest.param("OPENAI", False, id="openai"),
        pytest.param("OPENAI_RESPONSES", False, id="openai-responses"),
        pytest.param("OPENAI_RESPONSES", True, id="openai-responses-long-report"),
    ],
)
@pytest.mark.asyncio
async def test_subagent_completion_persists_succeeded_board_post_and_reconcile_is_idempotent(
    image_chat_runtime,
    protocol: str,
    long_report: bool,
) -> None:
    child_final = f"inspection-child-completed-{protocol}"
    if long_report:
        child_final += "长" * 22_000
        assert len(child_final.encode("utf-8")) > 65_535
    parent_final = f"inspection-parent-completed-{protocol}"

    async with _create_case(image_chat_runtime, protocol=protocol, child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None
        assert child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert origin is not None
            assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            posts = (
                (
                    await db.execute(
                        select(SubagentBoardPost)
                        .where(
                            SubagentBoardPost.subagent_id == child["subagent_id"],
                            SubagentBoardPost.child_work_id == child["work_id"],
                        )
                        .order_by(SubagentBoardPost.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(posts) == 1
            assert posts[0].stage == "succeeded"
            if long_report:
                assert len(posts[0].content.encode("utf-8")) <= 65_535
                assert json.loads(posts[0].content) == {"full_result_operation": "query_result"}
                round_row = await db.get(SubagentWork, child["work_id"])
                assert round_row is not None
                assert round_row.result is not None
                assert round_row.result["completed"] is True
                assert round_row.result["status"] == "succeeded"
                assert round_row.result["work_id"] == child["work_id"]
                assert round_row.result["result"]["content"] == child_final
            else:
                assert posts[0].content == child_final

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == case.origin_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert origin is not None
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert origin.result_message_id is not None
            result_message = await db.get(Message, origin.result_message_id)
            assert result_message is not None
            assert result_message.type == MessageType.TEXT
            assert result_message.content == parent_final

            notification_before = await _notification_snapshot(db, session_id=runtime.session_id)
            board_before = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
            assert await reconcile_child_work(db) == 0
            assert await reconcile_child_work(db) == 0
            notification_after = await _notification_snapshot(db, session_id=runtime.session_id)
            board_after = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
            assert notification_after == notification_before
            assert board_after == board_before


@pytest.mark.asyncio
async def test_board_post_wakes_parent_for_silent_review_and_resuspends_until_child_completion(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = "investigating"
    stage_content = "The child verified the first independent check and is continuing with the remaining work."
    child_final = "inspection-child-finished-after-review"
    parent_final = "inspection-parent-final-after-review"
    release_post = asyncio.Event()
    posted = asyncio.Event()
    child_task: asyncio.Task | None = None

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        original_post = child_work_module.post_subagent_board

        async def coordinate_board_post(*args, **kwargs):
            result = await original_post(*args, **kwargs)
            if kwargs.get("content") == stage_content:
                posted.set()
                await release_post.wait()
            return result

        monkeypatch.setattr(child_work_module, "post_subagent_board", coordinate_board_post)
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="inspection-board-post",
                        name=AGENT_WORK_TOOL_NAME,
                        arguments={
                            "operation": "board_post",
                            "stage": stage,
                            "content": stage_content,
                        },
                    )
                ],
            )
        )
        try:
            child_task = asyncio.create_task(runtime.run_next(independent_only=True))
            await asyncio.wait_for(posted.wait(), timeout=5)

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                child_row = await db.get(SessionReplyWorkItem, child["work_id"])
                assert origin is not None and child_row is not None
                assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert child_row.status == SessionReplyWorkStatus.RUNNING
                assert child_row.locked_by is not None
                board_posts = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert len(board_posts) == 1
                assert board_posts[0][1:] == (child["subagent_id"], child["work_id"], stage, stage_content)

            board_read_call = InternalToolCall(
                id="inspection-board-read-after-post",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "board_read", "subagent_id": child["subagent_id"]},
            )
            end_session_call = InternalToolCall(
                id="inspection-review-end-session",
                name=END_SESSION_TOOL_NAME,
                arguments={"skip_reply": True},
            )
            parent_provider_start = len(runtime.provider_payloads)
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[board_read_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[end_session_call]),
                ]
            )
            waiting = await runtime.run_next(independent_only=False)
            assert waiting is not None
            assert waiting.id == case.origin_work_id
            assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert not runtime.outputs

            parent_requests = runtime.provider_payloads[parent_provider_start:]
            assert len(parent_requests) == 2
            assert stage_content not in json.dumps(parent_requests[0], ensure_ascii=False)
            assert stage_content in json.dumps(parent_requests[1], ensure_ascii=False)
            platform_constraints = []
            for payload in parent_requests:
                input_items = payload.get("input")
                assert isinstance(input_items, list)
                for item in input_items:
                    if not isinstance(item, dict) or item.get("role") != MessageRole.USER.value:
                        continue
                    content = item.get("content")
                    if not isinstance(content, str):
                        continue
                    try:
                        platform_item = json.loads(content)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(platform_item, dict):
                        continue
                    constraint = platform_item.get("platform_constraints")
                    if isinstance(constraint, str):
                        platform_constraints.append(constraint)
            assert any(SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT.strip() in constraint for constraint in platform_constraints)

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert origin is not None
                assert origin.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
                assert origin.result_message_id is None
                assert origin.locked_by is None
                assert origin.lock_until is None
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert checkpoint.get("reply_skipped") is not True
                parent_text_messages = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == runtime.session_id,
                                Message.uid == runtime.uid,
                                Message.profile_id == runtime.profile.id,
                                Message.work_id.is_(None),
                                Message.role == MessageRole.ASSISTANT,
                                Message.type == MessageType.TEXT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert parent_text_messages == []
                events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id).order_by(SessionEvent.id))).scalars().all()
                assert not any(event.event.get("type") == "done" and event.event.get("work_id") == case.origin_work_id for event in events)
                assert origin.event_sent is False
                board_posts = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert len(board_posts) == 1
                assert board_posts[0][1:] == (child["subagent_id"], child["work_id"], stage, stage_content)

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            release_post.set()
            child_done = await child_task
            child_task = None
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                child_row = await db.get(SessionReplyWorkItem, child["work_id"])
                assert origin is not None and child_row is not None
                assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert child_row.status == SessionReplyWorkStatus.SUCCEEDED

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await runtime.run_next(independent_only=False)
            assert parent_done is not None
            assert parent_done.id == case.origin_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        finally:
            release_post.set()
            if child_task is not None:
                if not child_task.done():
                    child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_send_message_board_review_query_result_does_not_rewake_parent(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = "send-message-progress"
    stage_content = "The active child recorded progress before returning its complete result."
    child_final = "send-message-child-final-result"
    parent_final = "send-message-parent-final-result"
    send_message_instruction = "Continue the active child and report its complete result."
    release_post = asyncio.Event()
    posted = asyncio.Event()
    child_task: asyncio.Task | None = None

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        query_arguments = {
            "operation": "query_result",
            "subagent_id": child["subagent_id"],
            "work_id": child["work_id"],
        }

        original_post = child_work_module.post_subagent_board

        async def coordinate_board_post(*args, **kwargs):
            result = await original_post(*args, **kwargs)
            if kwargs.get("content") == stage_content:
                posted.set()
                await release_post.wait()
            return result

        original_query_subagent_result = child_work_module.query_subagent_result
        query_wrapper_calls = 0

        async def coordinate_query(
            db,
            *,
            uid,
            session_id,
            profile_id,
            subagent_id,
            work_id=None,
        ):
            nonlocal child_task, query_wrapper_calls
            query_wrapper_calls += 1
            assert query_wrapper_calls == 1
            assert subagent_id == child["subagent_id"]
            assert work_id == child["work_id"]
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            release_post.set()
            assert child_task is not None
            child_done = await asyncio.wait_for(child_task, timeout=10)
            child_task = None
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            query_result = await original_query_subagent_result(
                db,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                subagent_id=subagent_id,
                work_id=work_id,
            )
            assert query_result == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            return query_result

        def has_user_instruction(payload: dict, instruction: str) -> bool:
            input_items = payload.get("input")
            if not isinstance(input_items, list):
                return False
            for item in input_items:
                if not isinstance(item, dict) or item.get("role") != MessageRole.USER.value:
                    continue
                content = item.get("content")
                if isinstance(content, str) and instruction in content:
                    return True
                if isinstance(content, list) and any(isinstance(part, dict) and instruction in str(part.get("text") or "") for part in content):
                    return True
            return False

        monkeypatch.setattr(child_work_module, "post_subagent_board", coordinate_board_post)
        monkeypatch.setattr(child_work_module, "query_subagent_result", coordinate_query)
        try:
            new_work_id = await _submit(
                runtime,
                request_id="send-message-board-review-request",
                message="Continue the active child and answer after its result is available.",
            )
            assert new_work_id != case.origin_work_id
            send_arguments = {
                "operation": "send_message",
                "subagent_id": child["subagent_id"],
                "message": send_message_instruction,
            }
            send_call = InternalToolCall(
                id="send-message-board-review-call",
                name=AGENT_WORK_TOOL_NAME,
                arguments=send_arguments,
            )
            provider_before_send = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call]))
            new_waiting = await runtime.run_next(independent_only=False)
            assert new_waiting is not None
            assert new_waiting.id == new_work_id
            assert new_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert len(runtime.provider_payloads) == provider_before_send + 1
            assert not runtime.outputs

            old_status_response = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
            assert old_status_response.status_code == 200
            old_status_data = old_status_response.json()["data"]
            assert old_status_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
            assert old_status_data["result_message_id"] is None
            assert old_status_data["response"]["skip_reply"] is True
            provider_after_send = len(runtime.provider_payloads)
            assert await runtime.run_next(independent_only=False) is None
            assert len(runtime.provider_payloads) == provider_after_send

            async with runtime.factory() as db:
                actual_send_call_id = await _persisted_main_tool_call_id(
                    db,
                    session_id=runtime.session_id,
                    uid=runtime.uid,
                    profile_id=runtime.profile.id,
                    tool_call=send_call,
                )
                send_call_row = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == new_work_id,
                                SubagentToolCall.tool_call_id == actual_send_call_id,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                assert send_call_row.arguments == send_arguments
                assert send_call_row.child_work_id == child["work_id"]
                assert send_call_row.delivered is False
                assert send_call_row.tool_result_message_id is not None
                send_result_message_id = send_call_row.tool_result_message_id

            board_call = InternalToolCall(
                id="send-message-board-review-post",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "board_post",
                    "stage": stage,
                    "content": stage_content,
                },
            )
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[board_call]))
            child_task = asyncio.create_task(runtime.run_next(independent_only=True))
            await asyncio.wait_for(posted.wait(), timeout=5)

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, new_work_id)
                child_row = await db.get(SessionReplyWorkItem, child["work_id"])
                assert parent is not None and child_row is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert child_row.status == SessionReplyWorkStatus.RUNNING
                assert child_row.locked_by is not None
                stage_board = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert len(stage_board) == 1
                assert stage_board[0][3:] == (stage, stage_content)
                stage_markers, stage_events = await _notification_snapshot(db, session_id=runtime.session_id)
                assert len(stage_markers) == 1
                stage_marker_payload = json.loads(stage_markers[0][2] or "{}")
                assert stage_marker_payload["type"] == "subagent_board_update"
                assert stage_marker_payload["work_id"] == child["work_id"]
                stage_ready_events = [event for event in stage_events if event[2].get("type") == "work_ready" and event[2].get("work_id") == new_work_id]
                assert len(stage_ready_events) == 1
                assert stage_ready_events[0][1].startswith(f"subagent-board-ready:{new_work_id}:")
                assert not any(event[1].startswith(f"subagent-work-ready:{new_work_id}:{child['work_id']}") for event in stage_events)

                todo_write = await session_todo_crud.write(
                    db,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    todos=[{"content": "verify-send-result", "status": "in_progress"}],
                    expected_revision=0,
                )
                assert todo_write.written is True
                assert todo_write.plan is not None
                assert todo_write.plan.revision == 1
                assert todo_write.plan.todos == [{"content": "verify-send-result", "status": "in_progress"}]

            board_read_call = InternalToolCall(
                id="send-message-board-review-read",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "board_read", "subagent_id": child["subagent_id"]},
            )
            query_call = InternalToolCall(
                id="send-message-board-review-query",
                name=AGENT_WORK_TOOL_NAME,
                arguments=query_arguments,
            )
            parent_provider_start = len(runtime.provider_payloads)
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[board_read_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query_call]),
                ]
            )
            parent_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=10)
            assert parent_done is not None
            assert parent_done.id == new_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert query_wrapper_calls == 1
            assert not runtime.outputs

            requests = runtime.provider_payloads[parent_provider_start:]
            child_request_indexes = [index for index, payload in enumerate(requests) if has_user_instruction(payload, send_message_instruction)]
            assert len(requests) == 4
            assert len(child_request_indexes) == 1
            parent_requests = [payload for index, payload in enumerate(requests) if index not in child_request_indexes]
            assert len(parent_requests) == 3
            board_read_outputs = _provider_function_call_output_payloads(
                parent_requests[1],
                name=board_read_call.name,
                arguments=board_read_call.arguments,
            )
            query_result_outputs = _provider_function_call_output_payloads(
                parent_requests[2],
                name=query_call.name,
                arguments=query_call.arguments,
            )
            assert len(board_read_outputs) == 1
            assert board_read_outputs[0] == {
                "posts": [
                    {
                        "work_id": child["work_id"],
                        "stage": stage,
                        "content": stage_content,
                    }
                ]
            }
            assert len(query_result_outputs) == 1
            assert query_result_outputs[0] == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, new_work_id)
                child_row = await db.get(SessionReplyWorkItem, child["work_id"])
                actual_query_call_id = await _persisted_main_tool_call_id(
                    db,
                    session_id=runtime.session_id,
                    uid=runtime.uid,
                    profile_id=runtime.profile.id,
                    tool_call=query_call,
                )
                send_call_row = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == new_work_id,
                                SubagentToolCall.tool_call_id == actual_send_call_id,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                assert parent is not None and child_row is not None
                assert parent.status == SessionReplyWorkStatus.SUCCEEDED
                assert parent.result_message_id is not None
                assert not parent.execution_state.get("child_work_waits")
                assert child_row.status == SessionReplyWorkStatus.SUCCEEDED
                assert send_call_row.child_work_id == child["work_id"]
                assert send_call_row.delivered is True
                assert send_call_row.tool_result_message_id == send_result_message_id

                send_result = await db.get(Message, send_result_message_id)
                assert send_result is not None
                send_result_message = InternalMessage.model_validate_json(send_result.content or "{}")
                send_result_payload, _ = json.JSONDecoder().raw_decode(send_result_message.content or "")
                assert send_result_message.tool_call_id == actual_send_call_id
                assert send_result_message.tool_execution_status == "succeeded"
                assert send_result_payload == {
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                    "status": "succeeded",
                    "content": child_final,
                }

                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, new_work_id)
                assert isinstance(checkpoint, dict)
                checkpoint_messages = [InternalMessage.model_validate(item) for item in checkpoint["messages"]]
                persisted_query_calls = [tool_call for message in checkpoint_messages if message.role == MessageRole.ASSISTANT for tool_call in message.tool_calls or [] if tool_call.id == actual_query_call_id and tool_call.arguments == query_arguments]
                assert len(persisted_query_calls) == 1
                query_messages = [message for message in checkpoint_messages if message.role == MessageRole.TOOL and message.tool_call_id == actual_query_call_id]
                assert len(query_messages) == 1
                query_message = query_messages[0]
                query_content = query_message.content or ""
                query_payload, query_payload_end = json.JSONDecoder().raw_decode(query_content)
                assert query_message.tool_execution_status == "succeeded"
                assert query_payload == {
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                    "status": "succeeded",
                    "content": child_final,
                }
                assert query_content[query_payload_end:] == '\n\n<current_session_todo_snapshot>{"revision":1,"todos":[{"content":"verify-send-result","status":"in_progress"}]}</current_session_todo_snapshot>'

                final_message = await db.get(Message, parent.result_message_id)
                assert final_message is not None
                assert final_message.type == MessageType.TEXT
                assert final_message.content == parent_final

                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == child["subagent_id"]).order_by(SubagentToolCall.id))).scalars().all()
                assert len(calls) == 2
                assert calls[0].parent_work_id == case.origin_work_id
                assert calls[0].arguments["operation"] == "create"
                assert calls[0].child_work_id == child["work_id"]
                assert calls[1].parent_work_id == new_work_id
                assert calls[1].arguments == send_arguments
                assert calls[1].child_work_id == child["work_id"]

                final_board = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert final_board[0] == stage_board[0]
                assert any(post[3] == "succeeded" and child_final in post[4] for post in final_board)

                markers, events = await _notification_snapshot(db, session_id=runtime.session_id)
                assert markers == stage_markers
                parent_ready_events = [event for event in events if event[2].get("type") == "work_ready" and event[2].get("work_id") == new_work_id]
                assert len(parent_ready_events) == 1
                assert parent_ready_events[0][1].startswith(f"subagent-board-ready:{new_work_id}:")
                assert not any(event[1].startswith(f"subagent-work-ready:{new_work_id}:{child['work_id']}") for event in events)
                old_done_events = [event[2] for event in events if event[2].get("type") == "done" and event[2].get("work_id") == case.origin_work_id]
                assert len(old_done_events) == 1
                assert old_done_events[0]["skip_reply"] is True
                notification_before_cleanup = (markers, events)

            history_response = await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "size": 100},
            )
            assert history_response.status_code == 200
            history = history_response.json()["data"]
            assert sum(item.get("content") == parent_final for item in history) == 1

            provider_count = len(runtime.provider_payloads)
            async with runtime.factory() as db:
                assert await deliver_child_work_results(db, child_work_id=child["work_id"]) == 0
                assert await resume_child_work_for_parent(db, parent_work_id=new_work_id) is False
                assert await reconcile_child_work(db) == 0
            assert await runtime.run_next(independent_only=False) is None
            assert len(runtime.provider_payloads) == provider_count
            assert not runtime.outputs

            async with runtime.factory() as db:
                assert await _notification_snapshot(db, session_id=runtime.session_id) == notification_before_cleanup
        finally:
            release_post.set()
            if child_task is not None:
                if not child_task.done():
                    child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)


@pytest.mark.parametrize("review_mode", ["skip", "image"], ids=["skip", "image"])
@pytest.mark.asyncio
async def test_board_post_during_parent_review_is_reconciled_without_duplicate_delivery(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    review_mode: str,
) -> None:
    from app.transformers.openai import OpenAIResponsesTransformer

    first_stage = "first-check"
    first_content = "The child completed the first check and is continuing independently."
    second_stage = "second-check"
    second_content = "The child recorded a second progress update while the parent was reviewing the first one."
    child_final = "inspection-child-finished-after-concurrent-review"
    parent_final = "inspection-parent-final-after-concurrent-review"
    user_message = "inspection-unique-user-message-during-parent-review"
    user_final = "inspection-user-final-before-next-board-wake"
    user_request_id = "inspection-user-request-during-parent-review"
    image_prompt = "inspection-concurrent-review-image"
    first_posted = asyncio.Event()
    release_first_post = asyncio.Event()
    second_posted = asyncio.Event()
    release_second_post = asyncio.Event()
    parent_request_started = asyncio.Event()
    release_parent_request = asyncio.Event()
    hold_parent_requests = False
    child_task: asyncio.Task | None = None
    parent_task: asyncio.Task | None = None
    image_task_id: int | None = None

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        original_post = child_work_module.post_subagent_board

        async def coordinate_board_post(*args, **kwargs):
            result = await original_post(*args, **kwargs)
            content = kwargs.get("content")
            if content == first_content:
                first_posted.set()
                await release_first_post.wait()
            elif content == second_content:
                second_posted.set()
                await release_second_post.wait()
            return result

        original_post_json = OpenAIResponsesTransformer._post_json

        async def hold_parent_request(transformer, **kwargs):
            response = await original_post_json(transformer, **kwargs)
            if hold_parent_requests and not parent_request_started.is_set():
                parent_request_started.set()
                await release_parent_request.wait()
            return response

        monkeypatch.setattr(child_work_module, "post_subagent_board", coordinate_board_post)
        monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", hold_parent_request)
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="inspection-first-board-post",
                        name=AGENT_WORK_TOOL_NAME,
                        arguments={
                            "operation": "board_post",
                            "stage": first_stage,
                            "content": first_content,
                        },
                    )
                ],
            )
        )
        try:
            child_task = asyncio.create_task(runtime.run_next(independent_only=True))
            await asyncio.wait_for(first_posted.wait(), timeout=5)

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                child_row = await db.get(SessionReplyWorkItem, child["work_id"])
                assert origin is not None and child_row is not None
                assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert child_row.status == SessionReplyWorkStatus.RUNNING
                board_posts = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert len(board_posts) == 1
                assert board_posts[0][1:] == (child["subagent_id"], child["work_id"], first_stage, first_content)

            board_read_call = InternalToolCall(
                id="inspection-concurrent-board-read",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "board_read", "subagent_id": child["subagent_id"]},
            )
            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[board_read_call],
                )
            )
            parent_provider_start = len(runtime.provider_payloads)
            hold_parent_requests = True
            parent_task = asyncio.create_task(runtime.run_next(independent_only=False))
            await asyncio.wait_for(parent_request_started.wait(), timeout=5)

            user_work_id = await _submit(
                runtime,
                request_id=user_request_id,
                message=user_message,
                expected_status="queued",
            )
            assert user_work_id != case.origin_work_id
            assert await runtime.run_next(independent_only=False) is None

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                user_work = await db.get(SessionReplyWorkItem, user_work_id)
                assert origin is not None and user_work is not None
                assert origin.status == SessionReplyWorkStatus.RUNNING
                assert origin.locked_by is not None
                assert user_work.status == SessionReplyWorkStatus.READY_FOR_LLM

            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="inspection-second-board-post",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={
                                "operation": "board_post",
                                "stage": second_stage,
                                "content": second_content,
                            },
                        )
                    ],
                )
            )
            release_first_post.set()
            await asyncio.wait_for(second_posted.wait(), timeout=5)

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert origin is not None
                assert origin.status == SessionReplyWorkStatus.RUNNING
                board_posts = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert len(board_posts) == 2
                assert board_posts[0][1:] == (child["subagent_id"], child["work_id"], first_stage, first_content)
                assert board_posts[1][1:] == (child["subagent_id"], child["work_id"], second_stage, second_content)
                notification_during_review = await _notification_snapshot(db, session_id=runtime.session_id)
                markers_during_review, events_during_review = notification_during_review
                assert len(markers_during_review) == 1
                assert len([event for event in events_during_review if event[2].get("type") == "work_ready" and event[2].get("work_id") == case.origin_work_id]) == 1

            review_call = (
                InternalToolCall(
                    id="inspection-concurrent-review-end-session",
                    name=END_SESSION_TOOL_NAME,
                    arguments={"skip_reply": True},
                )
                if review_mode == "skip"
                else InternalToolCall(
                    id="inspection-concurrent-review-image",
                    name="generate_image",
                    arguments={"prompt": image_prompt},
                )
            )
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[review_call]))
            release_parent_request.set()
            parent_result = await parent_task
            parent_task = None
            assert parent_result is not None
            assert parent_result.id == case.origin_work_id
            assert parent_result.status == SessionReplyWorkStatus.READY_FOR_LLM

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert origin is not None and origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                notification_after_review = await _notification_snapshot(db, session_id=runtime.session_id)
                markers_after_review, events_after_review = notification_after_review
                assert len(markers_after_review) == 2
                assert len([event for event in events_after_review if event[2].get("type") == "work_ready" and event[2].get("work_id") == case.origin_work_id]) == 2
                board_after_review = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert len(board_after_review) == 2
                if review_mode == "image":
                    pending_tools = origin.execution_state.get("pending_tools")
                    assert isinstance(pending_tools, list)
                    assert len(pending_tools) == 1
                    pending_image = pending_tools[0]
                    assert isinstance(pending_image, dict)
                    assert pending_image.get("tool_name") == "generate_image"
                    image_task_id = pending_image.get("task_id")
                    assert isinstance(image_task_id, int) and not isinstance(image_task_id, bool) and image_task_id > 0

                before_duplicate_delivery = await _notification_snapshot(db, session_id=runtime.session_id)
                assert await deliver_child_work_results(db, child_work_id=child["work_id"]) == 0
                assert await deliver_child_work_results(db, child_work_id=child["work_id"]) == 0
                after_duplicate_delivery = await _notification_snapshot(db, session_id=runtime.session_id)
                assert after_duplicate_delivery == before_duplicate_delivery

            parent_review_payloads = runtime.provider_payloads[parent_provider_start:]
            board_read_outputs = _provider_function_call_output_payloads(
                runtime.provider_payloads[-1],
                name=board_read_call.name,
                arguments=board_read_call.arguments,
            )
            assert len(board_read_outputs) == 1
            assert board_read_outputs[0] == {
                "posts": [
                    {
                        "work_id": child["work_id"],
                        "stage": first_stage,
                        "content": first_content,
                    },
                    {
                        "work_id": child["work_id"],
                        "stage": second_stage,
                        "content": second_content,
                    },
                ]
            }
            assert user_message not in json.dumps(parent_review_payloads, ensure_ascii=False)

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))
            user_provider_start = len(runtime.provider_payloads)
            user_done = await runtime.run_next(independent_only=False)
            assert user_done is not None
            assert user_done.id == user_work_id
            assert user_done.id != case.origin_work_id
            assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
            user_provider_payloads = runtime.provider_payloads[user_provider_start:]
            assert user_provider_payloads
            assert user_message in json.dumps(user_provider_payloads, ensure_ascii=False)

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                user_work = await db.get(SessionReplyWorkItem, user_work_id)
                assert origin is not None and user_work is not None
                assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert origin.result_message_id is None
                assert user_work.status == SessionReplyWorkStatus.SUCCEEDED
                assert user_work.result_message_id is not None
                user_result_message = await db.get(Message, user_work.result_message_id)
                assert user_result_message is not None
                assert user_result_message.type == MessageType.TEXT
                assert user_result_message.content == user_final

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            release_second_post.set()
            child_done = await child_task
            child_task = None
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            if review_mode == "image":
                assert image_task_id is not None
                await runtime.complete_images(task_ids=[image_task_id])
                assert runtime.image_requests[-1]["prompt"] == image_prompt

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert origin is not None and origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                before_completed_duplicate = await _notification_snapshot(db, session_id=runtime.session_id)
                assert await deliver_child_work_results(db, child_work_id=child["work_id"]) == 0
                assert await deliver_child_work_results(db, child_work_id=child["work_id"]) == 0
                after_completed_duplicate = await _notification_snapshot(db, session_id=runtime.session_id)
                assert after_completed_duplicate == before_completed_duplicate

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await runtime.run_next(independent_only=False)
            assert parent_done is not None
            assert parent_done.id == case.origin_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        finally:
            release_first_post.set()
            release_second_post.set()
            release_parent_request.set()
            if parent_task is not None:
                if not parent_task.done():
                    parent_task.cancel()
                await asyncio.gather(parent_task, return_exceptions=True)
            if child_task is not None:
                if not child_task.done():
                    child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_queued_child_during_parent_review_does_not_rewake_parent(
    image_chat_runtime,
) -> None:
    first_final = "inspection-first-child-finished"
    parent_final = "inspection-parent-final-after-cancel"

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=2) as case:
        runtime = case.runtime
        first_child, second_child = case.children

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_final))
        first_done = await runtime.run_next(independent_only=True)
        assert first_done is not None
        assert first_done.id == first_child["work_id"]
        assert first_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            second_row = await db.get(SessionReplyWorkItem, second_child["work_id"])
            assert origin is not None and second_row is not None
            assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert second_row.status == SessionReplyWorkStatus.READY_FOR_LLM
            _markers, events = await _notification_snapshot(db, session_id=runtime.session_id)
            assert len([event for event in events if event[2].get("type") == "work_ready" and event[2].get("work_id") == case.origin_work_id]) == 1

        cancel_call = InternalToolCall(
            id="inspection-cancel-queued-child",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "cancel",
                "subagent_id": second_child["subagent_id"],
                "work_id": second_child["work_id"],
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[cancel_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=parent_final),
            ]
        )
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == case.origin_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            second_row = await db.get(SessionReplyWorkItem, second_child["work_id"])
            second_call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == case.origin_work_id,
                            SubagentToolCall.child_work_id == second_child["work_id"],
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert origin is not None and second_row is not None
            actual_cancel_call_id = await _persisted_main_tool_call_id(
                db,
                session_id=runtime.session_id,
                uid=runtime.uid,
                profile_id=runtime.profile.id,
                tool_call=cancel_call,
            )
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert second_row.status == SessionReplyWorkStatus.CANCELLED
            assert second_call.delivered is True
            assert second_call.tool_result_message_id is not None
            original_result = await db.get(Message, second_call.tool_result_message_id)
            assert original_result is not None
            original_result_message, original_result_payload = _payload(original_result)
            assert original_result_message.tool_call_id == second_call.tool_call_id
            assert original_result_message.tool_execution_status == "failed"
            assert original_result_payload == {
                "subagent_id": second_child["subagent_id"],
                "work_id": second_child["work_id"],
                "status": "cancelled",
                "reason": t(ERR_SUBAGENT_CANCELLED),
            }
            cancel_posts = await _board_snapshot(
                db,
                subagent_id=second_child["subagent_id"],
                child_work_id=second_child["work_id"],
            )
            assert len(cancel_posts) == 1
            assert cancel_posts[0][3] == "cancelled"
            markers, events = await _notification_snapshot(db, session_id=runtime.session_id)
            assert len([event for event in events if event[2].get("type") == "work_ready" and event[2].get("work_id") == case.origin_work_id]) == 1
            notification_before_reconcile = (markers, events)
            assert await reconcile_child_work(db) == 0
            assert await _notification_snapshot(db, session_id=runtime.session_id) == notification_before_reconcile

        final_request = runtime.provider_payloads[-1]
        final_input = final_request.get("input")
        assert isinstance(final_input, list)
        cancel_outputs = [item for item in final_input if isinstance(item, dict) and item.get("type") == "function_call_output" and item.get("call_id") == actual_cancel_call_id]
        assert len(cancel_outputs) == 1
        assert isinstance(cancel_outputs[0].get("output"), str)
        cancel_payload = json.loads(cancel_outputs[0]["output"])
        assert cancel_payload == {
            "subagent_id": second_child["subagent_id"],
            "work_id": second_child["work_id"],
            "status": "cancelled",
            "reason": t(ERR_SUBAGENT_CANCELLED),
        }

        assert await runtime.run_next(independent_only=False) is None

        history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert history_response.status_code == 200
        history = history_response.json()["data"]
        assert sum(item.get("content") == parent_final for item in history) == 1


@pytest.mark.parametrize(
    "wait_state",
    ["registered", "interrupted"],
    ids=["registered-wait", "interrupted-before-result"],
)
@pytest.mark.asyncio
async def test_legacy_explicit_wait_record_is_delivered_after_release(
    image_chat_runtime,
    wait_state: str,
) -> None:
    child_final = "legacy-explicit-wait-child-result"
    parent_final = "legacy-explicit-wait-parent-result"

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        wait_arguments = {
            "operation": "wait",
            "subagent_id": child["subagent_id"],
            "work_id": child["work_id"],
        }
        wait_call = InternalToolCall(
            id="legacy-explicit-wait-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments=wait_arguments,
        )
        wait_result_message_id: int | None = None
        queued_wait_content: str | None = None

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
            assert parent is not None and isinstance(checkpoint, dict)

            wait_assistant = InternalMessage(
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                tool_calls=[wait_call],
            )
            saved_wait_assistant = await save_message(
                db,
                runtime.session_id,
                runtime.uid,
                MessageRole.ASSISTANT,
                MessageType.TOOL_CALL,
                wait_assistant,
                runtime.profile.id,
                commit=False,
            )
            assert saved_wait_assistant.id is not None
            wait_assistant.id = saved_wait_assistant.id
            wait_assistant.created_at = saved_wait_assistant.created_at

            messages = [InternalMessage.model_validate(message) for message in checkpoint["messages"]]
            turn_messages = [InternalMessage.model_validate(message) for message in checkpoint["turn_messages"]]
            messages.append(wait_assistant)
            turn_messages.append(wait_assistant.model_copy(deep=True))

            call = SubagentToolCall(
                subagent_id=child["subagent_id"],
                child_work_id=child["work_id"],
                parent_work_id=case.origin_work_id,
                tool_call_id=wait_call.id,
                arguments=deepcopy(wait_arguments),
                delivered=False,
            )

            if wait_state == "registered":
                queued_payload = {
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                    "status": "queued",
                }
                wait_result = InternalMessage(
                    role=MessageRole.TOOL,
                    type=MessageType.TOOL_RESULT,
                    tool_call_id=wait_call.id,
                    content=json.dumps(queued_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                )
                saved_wait_result = await save_message(
                    db,
                    runtime.session_id,
                    runtime.uid,
                    MessageRole.TOOL,
                    MessageType.TOOL_RESULT,
                    wait_result,
                    runtime.profile.id,
                    commit=False,
                )
                assert saved_wait_result.id is not None
                wait_result.id = saved_wait_result.id
                wait_result.created_at = saved_wait_result.created_at
                messages.append(wait_result)
                turn_messages.append(wait_result.model_copy(deep=True))
                wait_result_message_id = saved_wait_result.id
                queued_wait_content = saved_wait_result.content
                call.tool_result_message_id = wait_result_message_id

                state = dict(parent.execution_state)
                existing_waits = state.get("child_work_waits")
                assert isinstance(existing_waits, list)
                state["child_work_waits"] = [
                    *existing_waits,
                    {
                        "child_work_id": child["work_id"],
                        "subagent_id": child["subagent_id"],
                        "tool_call_id": wait_call.id,
                        "tool_name": AGENT_WORK_TOOL_NAME,
                        "tool_result_message_id": wait_result_message_id,
                        "response_id": None,
                    },
                ]
                external_result_ids = state.get("external_tool_result_message_ids")
                if isinstance(external_result_ids, list):
                    state["external_tool_result_message_ids"] = [*external_result_ids, wait_result_message_id]
                parent.execution_state = state
            else:
                parent.status = SessionReplyWorkStatus.READY_FOR_LLM
                parent.locked_by = None
                parent.lock_until = None

            db.add(call)
            checkpoint["messages"] = [message.model_dump(mode="json", exclude_none=True) for message in messages]
            checkpoint["turn_messages"] = [message.model_dump(mode="json", exclude_none=True) for message in turn_messages]
            await session_reply_work_item_crud.save_checkpoint(db, case.origin_work_id, checkpoint)
            await db.commit()

        if wait_state == "interrupted":
            provider_count = len(runtime.provider_payloads)
            recovered = await runtime.run_next(independent_only=False)
            assert recovered is not None
            assert recovered.id == case.origin_work_id
            assert recovered.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert len(runtime.provider_payloads) == provider_count
            assert not runtime.outputs

            async with runtime.factory() as db:
                call = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == case.origin_work_id,
                                SubagentToolCall.tool_call_id == wait_call.id,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                assert call.tool_result_message_id is not None
                wait_result_message_id = call.tool_result_message_id
                queued_result = await db.get(Message, wait_result_message_id)
                assert queued_result is not None
                queued_wait_content = queued_result.content

        assert wait_result_message_id is not None
        assert queued_wait_content is not None

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None
        assert child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
            call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == case.origin_work_id,
                            SubagentToolCall.tool_call_id == wait_call.id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            result_row = await db.get(Message, wait_result_message_id)
            assert parent is not None and result_row is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert call.delivered is True
            assert call.tool_result_message_id == wait_result_message_id
            assert result_row.content != queued_wait_content
            result_message, result_payload = _payload(result_row)
            assert result_message.tool_execution_status == "succeeded"
            assert result_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == case.origin_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs
        assert len(runtime.provider_payloads[parent_provider_start:]) == 1
        assert child_final in json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)

        provider_count = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count
        history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert history_response.status_code == 200
        history = history_response.json()["data"]
        assert sum(item.get("content") == parent_final for item in history) == 1


@pytest.mark.parametrize("recover_child", [False, True], ids=["normal", "recovery"])
@pytest.mark.asyncio
async def test_subagent_direct_image_delivery_uses_verified_path_and_recovers_saved_reply(image_chat_runtime, monkeypatch: pytest.MonkeyPatch, recover_child: bool) -> None:
    parent_final = "subagent-image-parent-final-reply"
    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES") as case:
        runtime, child = case.runtime, case.children[0]
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                generated_images=[
                    InternalGeneratedImage(
                        id=f"subagent-direct-image-{recover_child}",
                        data=base64.b64encode(runtime.image_bytes).decode("ascii"),
                        mime_type="image/png",
                    )
                ],
            )
        )
        child_provider_start = len(runtime.provider_payloads)
        saved_message_id: int | None = None
        if recover_child:
            original_save = interactive_runtime_module.save_assistant_message

            async def interrupt_after_save(*args, **kwargs):
                nonlocal saved_message_id
                db = args[0] if args else kwargs["db"]
                claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
                claimed_work_id = claim[0] if isinstance(claim, (tuple, list)) and len(claim) == 2 else None
                if claimed_work_id != child["work_id"]:
                    return await original_save(*args, **kwargs)
                message = args[4] if len(args) > 4 else kwargs["ai_msg"]
                assert isinstance(message, InternalMessage) and not message.tool_calls
                assert json.loads(message.content or "")["text"] == ""
                saved_message = await original_save(*args, **kwargs)
                saved_message_id = saved_message.id
                raise asyncio.CancelledError("interrupted after saving the child image reply")

            with monkeypatch.context() as patch:
                patch.setattr(interactive_runtime_module, "save_assistant_message", interrupt_after_save)
                with pytest.raises(asyncio.CancelledError):
                    await runtime.run_next(independent_only=True)
            assert isinstance(saved_message_id, int) and not runtime.outputs
            async with runtime.factory() as db:
                work = await db.get(SessionReplyWorkItem, child["work_id"])
                round_row = await db.get(SubagentWork, child["work_id"])
                saved_message = await db.get(Message, saved_message_id)
                assert work is not None and round_row is not None and saved_message is not None
                assert work.status == SessionReplyWorkStatus.RUNNING and work.result_message_id is None and round_row.result is None
                assert saved_message.type == MessageType.TEXT and json.loads(saved_message.content or "")["type"] == "assistant_files"
                work.lock_until = 0
                await db.commit()
            before_recovery = len(runtime.provider_payloads)
            await SessionReplyConsumer()._recover_expired()
            assert len(runtime.provider_payloads) == before_recovery
            async with runtime.factory() as db:
                work = await db.get(SessionReplyWorkItem, child["work_id"])
                round_row = await db.get(SubagentWork, child["work_id"])
                assert work is not None and round_row is not None
                assert work.status == SessionReplyWorkStatus.READY_FOR_LLM and round_row.result is None
            child_done = await runtime.run_next(independent_only=True)
        else:
            child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None and child_done.id == child["work_id"] and child_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == child_provider_start + 1 and runtime.image_requests == []
        child_tools = runtime.provider_payloads[child_provider_start].get("tools", [])
        assert all((tool.get("function") if isinstance(tool.get("function"), dict) else tool).get("name") != SEND_FILE_TO_USER_TOOL_NAME for tool in child_tools)

        async with runtime.factory() as db:
            work = await db.get(SessionReplyWorkItem, child["work_id"])
            round_row = await db.get(SubagentWork, child["work_id"])
            create_message = await db.get(Message, child["create_result_id"])
            assert work is not None and round_row is not None and create_message is not None
            assert work.status == SessionReplyWorkStatus.SUCCEEDED and work.result_message_id is not None and round_row.result is not None
            saved_message = await db.get(Message, work.result_message_id)
            assert saved_message is not None and saved_message.type == MessageType.TEXT
            saved_payload = json.loads(saved_message.content or "{}")
            assert saved_payload["type"] == "assistant_files" and saved_payload["text"] == "" and "path" not in saved_payload["files"][0]
            _, create_payload = _payload(create_message)
            assert create_payload["subagent_id"] == child["subagent_id"] and create_payload["work_id"] == child["work_id"]
            assert create_payload["status"] == "succeeded" and "content" not in create_payload and len(create_payload["files"]) == 1

        generated_file = create_payload["files"][0]
        generated_path = Path(generated_file["path"])
        assert generated_path.is_absolute() and generated_path.parent == (get_user_temp_dir(Path.cwd(), runtime.uid) / "generated_images").resolve()
        assert generated_path.read_bytes() == runtime.image_bytes
        query_call = InternalToolCall(
            id="direct-image-query-result",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "query_result", "subagent_id": child["subagent_id"], "work_id": child["work_id"]},
        )
        board_call = InternalToolCall(
            id="direct-image-board-read",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "board_read", "subagent_id": child["subagent_id"]},
        )
        send_call = InternalToolCall(
            id="direct-image-send-file",
            name=SEND_FILE_TO_USER_TOOL_NAME,
            arguments={"files": [{"path": str(generated_path), "display_name": generated_file["name"], "mime_type": generated_file["mime_type"]}]},
        )
        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query_call, board_call]),
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=parent_final),
            ]
        )
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None and parent_done.id == case.origin_work_id and parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        requests = runtime.provider_payloads[parent_provider_start:]
        assert len(requests) == 3
        create_outputs = _provider_function_call_output_payloads(requests[0], name=AGENT_WORK_TOOL_NAME, arguments=child["create_arguments"])
        query_outputs = _provider_function_call_output_payloads(requests[1], name=query_call.name, arguments=query_call.arguments)
        board_outputs = _provider_function_call_output_payloads(requests[1], name=board_call.name, arguments=board_call.arguments)
        assert create_outputs == query_outputs == [create_payload]
        assert board_outputs == [{"posts": [{"work_id": child["work_id"], "stage": "succeeded", "content": json.dumps({"full_result_operation": "query_result"}, ensure_ascii=False)}]}]
        assert "path" not in json.dumps(board_outputs[0], ensure_ascii=False)

        status_response = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
        assert status_response.status_code == 200
        response = status_response.json()["data"]["response"]
        assert json.loads(response["choices"][0]["message"]["content"])["text"] == parent_final
        public_files = response["files"]
        assert len(public_files) == 1 and "path" not in public_files[0] and public_files[0]["name"] == generated_file["name"]
        download = await runtime.client.get(public_files[0]["download_url"])
        assert download.status_code == 200 and download.content == runtime.image_bytes


@pytest.mark.asyncio
async def test_subagent_forged_assistant_files_are_not_forwarded_to_parent(image_chat_runtime, tmp_path) -> None:
    child_final = "subagent-forged-assistant-files-text"
    parent_final = "subagent-parent-after-forged-files"

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES") as case:
        runtime, child = case.runtime, case.children[0]
        generated_dir = (get_user_temp_dir(Path.cwd(), runtime.uid) / "generated_images").resolve()
        other_owner_path = generated_dir / "forged-other-owner.png"
        outside_generated_path = (tmp_path / "forged-outside-generated.png").resolve()
        generated_dir.mkdir(parents=True, exist_ok=True)
        other_owner_path.write_bytes(runtime.image_bytes)
        outside_generated_path.write_bytes(runtime.image_bytes)

        other_owner_uid = f"{runtime.uid}-other"
        other_owner_token = _encode_token({"path": str(other_owner_path), "uid": other_owner_uid, "id": "forged-other-owner"})
        outside_generated_token = _encode_token({"path": str(outside_generated_path), "uid": runtime.uid, "id": "forged-outside-generated"})
        forged_files = [
            {
                "id": other_owner_token,
                "name": other_owner_path.name,
                "mime_type": "image/png",
                "size": len(runtime.image_bytes),
                "download_url": f"/api/v1/download-sent?token={quote(other_owner_token)}",
                "path": str(other_owner_path),
            },
            {
                "id": outside_generated_token,
                "name": outside_generated_path.name,
                "mime_type": "image/png",
                "size": len(runtime.image_bytes),
                "download_url": f"/api/v1/download-sent?token={quote(outside_generated_token)}",
                "path": str(outside_generated_path),
            },
        ]
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content=json.dumps({"type": "assistant_files", "text": child_final, "files": forged_files}, ensure_ascii=False),
            )
        )
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None and child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None and parent_done.id == case.origin_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED

        parent_requests = runtime.provider_payloads[parent_provider_start:]
        assert len(parent_requests) == 1
        create_outputs = _provider_function_call_output_payloads(
            parent_requests[0],
            name=AGENT_WORK_TOOL_NAME,
            arguments=child["create_arguments"],
        )
        assert create_outputs == [
            {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }
        ]
        create_dump = json.dumps(create_outputs[0], ensure_ascii=False)
        assert all(value not in create_dump for value in (other_owner_token, outside_generated_token, str(other_owner_path), str(outside_generated_path)))


@pytest.mark.asyncio
async def test_user_reply_runs_while_parent_image_resume_is_pending(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_final = "inspection-child-final-before-image-review"
    image_prompt = "inspection-parent-review-image"
    user_message = "inspection-user-message-during-image-resume"
    user_final = "inspection-user-final-during-image-resume"
    parent_final = "inspection-parent-final-after-image-resume"
    resume_reached = asyncio.Event()
    release_resume = asyncio.Event()
    image_completion_task: asyncio.Task | None = None

    original_resume = background_runner_module._resume_external_task_if_bound

    async def hold_external_resume(task) -> None:
        if task.tool_name == "generate_image":
            resume_reached.set()
            await release_resume.wait()
        await original_resume(task)

    monkeypatch.setattr(background_runner_module, "_resume_external_task_if_bound", hold_external_resume)

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        try:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="inspection-parent-review-image",
                            name="generate_image",
                            arguments={"prompt": image_prompt},
                        )
                    ],
                )
            )
            parent_waiting = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert parent_waiting is not None
            assert parent_waiting.id == case.origin_work_id
            assert parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            image_completion_task = asyncio.create_task(runtime.complete_images())
            await asyncio.wait_for(resume_reached.wait(), timeout=5)

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                image_task = (
                    (
                        await db.execute(
                            select(BackgroundTask).where(
                                BackgroundTask.session_id == runtime.session_id,
                                BackgroundTask.tool_name == "generate_image",
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                assert parent is not None and image_task is not None
                assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
                assert image_task.status == BackgroundTaskStatus.SUCCEEDED
                assert image_task.result is not None
                image_display_name = image_task.result["content"]["send_file_to_user"]["files"][0]["display_name"]

            user_work_id = await asyncio.wait_for(
                _submit(
                    runtime,
                    request_id="inspection-user-during-image-resume",
                    message=user_message,
                    expected_status="accepted",
                ),
                timeout=5,
            )
            assert user_work_id != case.origin_work_id
            user_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))
            user_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert user_done is not None
            assert user_done.id == user_work_id
            assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert image_display_name not in json.dumps(runtime.provider_payloads[user_provider_start:], ensure_ascii=False)
            user_status = await runtime.client.get(f"/api/v1/chat/reply-works/{user_work_id}")
            assert user_status.status_code == 200
            user_data = user_status.json()["data"]
            assert user_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
            assert user_data["request_ids"] == ["inspection-user-during-image-resume"]
            assert user_data["response"]["choices"][0]["message"]["content"] == user_final

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            release_resume.set()
            await asyncio.wait_for(image_completion_task, timeout=5)
            image_completion_task = None

            parent_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert parent_done is not None
            assert parent_done.id == case.origin_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            parent_request_dump = json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)
            assert image_display_name in parent_request_dump
            assert child_final in parent_request_dump
        finally:
            release_resume.set()
            if image_completion_task is not None:
                if not image_completion_task.done():
                    image_completion_task.cancel()
                await asyncio.gather(image_completion_task, return_exceptions=True)


@pytest.mark.parametrize("interruption_point", ["before-result-save", "after-result-save"], ids=["before-send-result-save", "after-send-result-save"])
@pytest.mark.asyncio
async def test_cross_reply_send_message_recovery_keeps_original_image_delivery(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    interruption_point: str,
) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module

    image_prompt = "cross-reply-send-message-image"
    append_instruction = "Continue the unfinished child task and report its final result."
    child_final = "cross-reply-child-final"
    original_final = "cross-reply-original-parent-final"
    new_final = "cross-reply-new-parent-final"
    image_call = InternalToolCall(
        id=f"cross-reply-image-{interruption_point}",
        name="generate_image",
        arguments={"prompt": image_prompt},
    )

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", extra_tool_calls=[image_call]) as case:
        runtime = case.runtime
        child = case.children[0]
        async with runtime.factory() as db:
            image_task = (await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id, BackgroundTask.tool_name == "generate_image"))).scalars().one()
            assert image_task.status == BackgroundTaskStatus.PENDING
            image_task_id = image_task.id

        new_work_id = await _submit(
            runtime,
            request_id=f"cross-reply-send-{interruption_point}",
            message="Continue the existing child task from this new reply.",
        )
        send_arguments = {
            "operation": "send_message",
            "subagent_id": child["subagent_id"],
            "message": append_instruction,
        }
        provider_before_send = len(runtime.provider_payloads)
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id=f"cross-reply-send-call-{interruption_point}", name=AGENT_WORK_TOOL_NAME, arguments=send_arguments)],
            )
        )
        interrupted = False
        saved_result_id: int | None = None
        original_save_tool_response = interactive_tools_module.save_tool_response

        async def interrupt_send_result(*args, **kwargs):
            nonlocal interrupted, saved_result_id
            db = args[0] if args else kwargs["db"]
            tool_result = kwargs.get("tool_res")
            if tool_result is None and len(args) > 4:
                tool_result = args[4]
            try:
                payload = json.loads(tool_result.content or "{}")
            except (AttributeError, TypeError, ValueError):
                payload = None
            claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
            claimed_work_id = claim[0] if isinstance(claim, (tuple, list)) and len(claim) == 2 else None
            target = not interrupted and claimed_work_id == new_work_id and isinstance(payload, dict) and payload.get("status") == "queued" and payload.get("subagent_id") == child["subagent_id"] and payload.get("work_id") == child["work_id"]
            if target:
                if interruption_point == "after-result-save":
                    saved_result_id = (await original_save_tool_response(*args, **kwargs)).id
                interrupted = True
                raise asyncio.CancelledError("interrupted around cross-reply send result save")
            return await original_save_tool_response(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(interactive_tools_module, "save_tool_response", interrupt_send_result)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=False)
        assert interrupted and len(runtime.provider_payloads) == provider_before_send + 1

        async with runtime.factory() as db:
            new_work = await db.get(SessionReplyWorkItem, new_work_id)
            assert new_work is not None and new_work.status == SessionReplyWorkStatus.RUNNING
            new_work.lock_until = 0
            await db.commit()

        provider_count = len(runtime.provider_payloads)
        await SessionReplyConsumer()._recover_expired()
        assert len(runtime.provider_payloads) == provider_count
        recovered = await runtime.run_next(independent_only=False)
        assert recovered is not None and recovered.id == new_work_id
        assert recovered.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads) == provider_count

        async with runtime.factory() as db:
            send_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == new_work_id))).scalars().one()
            assert send_row.arguments == send_arguments
            assert send_row.child_work_id == child["work_id"]
            assert send_row.tool_result_message_id is not None
            send_result = await db.get(Message, send_row.tool_result_message_id)
            assert send_result is not None
            _, send_payload = _payload(send_result)
            assert send_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "queued",
            }
            assert (saved_result_id is not None) is (interruption_point == "after-result-save")
            if saved_result_id is not None:
                assert send_row.tool_result_message_id == saved_result_id

        await runtime.complete_images()
        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            image_task = await db.get(BackgroundTask, image_task_id)
            assert origin is not None and origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert image_task is not None and image_task.status == BackgroundTaskStatus.SUCCEEDED and image_task.result is not None
            image_display_name = image_task.result["content"]["send_file_to_user"]["files"][0]["display_name"]
            waits = origin.execution_state.get("child_work_waits", [])
            assert child["work_id"] not in {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)}

        original_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=original_final))
        original_done = await runtime.run_next(independent_only=False)
        assert original_done is not None and original_done.id == case.origin_work_id
        assert original_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads[original_provider_start:]) == 1
        assert image_display_name in json.dumps(runtime.provider_payloads[original_provider_start:], ensure_ascii=False)

        child_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None and child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        child_request_dump = json.dumps(runtime.provider_payloads[child_provider_start:], ensure_ascii=False)
        assert len(runtime.provider_payloads[child_provider_start:]) == 1
        assert child_request_dump.count(append_instruction) == 1

        new_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_final))
        new_done = await runtime.run_next(independent_only=False)
        assert new_done is not None and new_done.id == new_work_id
        assert new_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads[new_provider_start:]) == 1
        assert child_final in json.dumps(runtime.provider_payloads[new_provider_start:], ensure_ascii=False)

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            new_work = await db.get(SessionReplyWorkItem, new_work_id)
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == child["subagent_id"]).order_by(SubagentToolCall.id))).scalars().all()
            assert origin is not None and new_work is not None
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED and new_work.status == SessionReplyWorkStatus.SUCCEEDED and origin.result_message_id is not None and new_work.result_message_id is not None
            assert [call.arguments["operation"] for call in calls] == ["create", "send_message"]
            assert all(call.delivered for call in calls)
            assert calls[0].tool_result_message_id == child["create_result_id"]
            old_create_result = await db.get(Message, calls[0].tool_result_message_id)
            send_result = await db.get(Message, calls[1].tool_result_message_id)
            assert old_create_result is not None and send_result is not None
            _, old_create_payload = _payload(old_create_result)
            _, send_payload = _payload(send_result)
            assert old_create_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }
            assert send_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }
            original_message = await db.get(Message, origin.result_message_id)
            new_message = await db.get(Message, new_work.result_message_id)
            assert original_message is not None and original_message.content == original_final
            assert new_message is not None and new_message.content == new_final

        provider_count = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count
