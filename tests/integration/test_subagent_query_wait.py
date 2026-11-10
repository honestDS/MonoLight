import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.child_work as child_work_module
from app.core.constants import AGENT_WORK_TOOL_NAME, END_SESSION_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT
from app.core.session_reply_queue.child_delivery import deliver_child_work_results
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.utils.dispatcher.process_single_tool import get_tool_execution_status
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_event import SessionEvent
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
            message="Create independent child tasks, then wait for their results before answering the parent.",
        )
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
            if operation == "board_read":
                assert inspection_results[0]["status"] == "success"
                assert inspection_results[0]["operation"] == "board_read"
                assert inspection_results[0]["posts"] == []
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_start + 2


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
            board_payload = json.loads(posts[0].content)
            if long_report:
                assert len(posts[0].content.encode("utf-8")) <= 65_535
                round_row = await db.get(SubagentWork, child["work_id"])
                assert round_row is not None
                assert round_row.result is not None
                assert round_row.result["completed"] is True
                assert round_row.result["status"] == "succeeded"
                assert round_row.result["work_id"] == child["work_id"]
                assert round_row.result["result"]["content"] == child_final
                assert board_payload["completed"] is True
                assert board_payload["status"] == "succeeded"
                assert board_payload["work_id"] == child["work_id"]
                assert board_payload["full_result_operation"] == "query"
            else:
                assert board_payload["completed"] is True
                assert board_payload["result"]["content"] == child_final

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

            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="inspection-concurrent-board-read",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={"operation": "board_read", "subagent_id": child["subagent_id"]},
                        )
                    ],
                )
            )
            hold_parent_requests = True
            parent_task = asyncio.create_task(runtime.run_next(independent_only=False))
            await asyncio.wait_for(parent_request_started.wait(), timeout=5)

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
            assert original_result_payload["work"]["status"] == "cancelled"
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
        assert cancel_payload["operation"] == "cancel"
        assert cancel_payload["work"]["status"] == "cancelled"

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
                    "status": "queued",
                    "operation": "wait",
                    "work_id": child["work_id"],
                    "child_work_id": child["work_id"],
                    "subagent_id": child["subagent_id"],
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
            assert result_payload["status"] == "success"
            assert result_payload["operation"] == "wait"
            assert result_payload["work"]["result"]["content"] == child_final

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
