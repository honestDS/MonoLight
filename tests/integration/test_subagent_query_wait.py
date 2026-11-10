import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME
from app.core.utils.dispatcher.process_single_tool import get_tool_execution_status
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork


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
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls))
        origin_work_id = await _submit(
            runtime,
            request_id="inspection-origin-request",
            message="Create independent child tasks, then answer the parent immediately.",
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="inspection-parent-final"))
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
@pytest.mark.parametrize("operation", ["query", "list", "board_read"])
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
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
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
            if operation == "board_read":
                assert inspection_results[0]["status"] == "success"
                assert inspection_results[0]["operation"] == "board_read"
                assert inspection_results[0]["posts"] == []
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_start + 2
