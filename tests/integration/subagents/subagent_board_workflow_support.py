import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentBoardPost, SubagentToolCall


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


async def _subagent_external_resume_result(
    db,
    *,
    session_id: str,
    uid: str,
    profile_id: int,
    subagent_id: str,
    work_id: int,
    call_id: str,
    dedupe_key: str,
) -> tuple[dict, dict]:
    messages = (
        (
            await db.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.uid == uid,
                    Message.profile_id == profile_id,
                    Message.type == MessageType.SUBAGENT_RESULT,
                    Message.dedupe_key == dedupe_key,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(messages) == 1
    message = messages[0]
    assert message.role == MessageRole.USER
    payload = json.loads(message.content or "{}")
    assert isinstance(payload, dict)
    assert payload.get("type") == "external_work_resume"
    assert payload.get("source") == "subagent"
    tool_call = payload.get("tool_call")
    assert isinstance(tool_call, dict)
    assert tool_call.get("id") == call_id
    results = payload.get("results")
    assert isinstance(results, list) and len(results) == 1
    result = results[0]
    assert isinstance(result, dict)
    assert result.get("subagent_id") == subagent_id
    assert result.get("work_id") == work_id
    return payload, result


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
            output_payload = json.loads(output)
        except json.JSONDecodeError:
            continue
        if isinstance(output_payload, dict) and isinstance(output_payload.get("tool_result"), dict):
            output_payloads.append(output_payload["tool_result"])
    return output_payloads


def _provider_subagent_resume_results(payload: dict) -> list[dict]:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return []

    results = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("role") != MessageRole.USER.value:
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
        else:
            continue

        for text in texts:
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("user_message"), str):
                try:
                    parsed = json.loads(parsed["user_message"])
                except (TypeError, json.JSONDecodeError):
                    continue
            if not isinstance(parsed, dict) or parsed.get("type") != "external_work_resume" or parsed.get("source") != "subagent":
                continue
            resume_results = parsed.get("results")
            if not isinstance(resume_results, list):
                continue
            results.extend(result for result in resume_results if isinstance(result, dict))
    return results


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
