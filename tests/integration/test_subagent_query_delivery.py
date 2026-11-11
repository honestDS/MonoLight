import json
from copy import deepcopy

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.child_work as child_work_module
from app.core.constants import AGENT_WORK_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.todo import session_todo_crud
from app.core.session_reply_queue.child_delivery import (
    deliver_child_work_results,
    resume_child_work_for_parent,
)
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentToolCall, SubagentWork


def _provider_items(payload: dict) -> list[dict]:
    items = payload.get("input")
    assert isinstance(items, list)
    return [item for item in items if isinstance(item, dict)]


def _has_user_instruction(payload: dict, instruction: str) -> bool:
    for item in _provider_items(payload):
        if item.get("role") != MessageRole.USER.value:
            continue
        content = item.get("content")
        if isinstance(content, str) and instruction in content:
            return True
        if isinstance(content, list) and any(isinstance(part, dict) and instruction in str(part.get("text") or "") for part in content):
            return True
    return False


@pytest.mark.parametrize("query_with_work_id", [False, True], ids=["query-latest", "query-explicit-work"])
@pytest.mark.parametrize("with_todo_snapshot", [False, True], ids=["without-todo-snapshot", "with-todo-snapshot"])
@pytest.mark.asyncio
async def test_completed_subagent_query_does_not_reawaken_parent(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    query_with_work_id: bool,
    with_todo_snapshot: bool,
) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        create_messages = (
            "query-delivery-child-request-one",
            "query-delivery-child-request-two",
        )
        summary_a = "query-delivery-child-A-complete-summary"
        summary_b = "query-delivery-child-B-complete-summary"
        parent_summary = "query-delivery-parent-final-summary"
        create_calls = [
            InternalToolCall(
                id="query-delivery-create-a",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "create", "name": "构建检查", "message": create_messages[0]},
            ),
            InternalToolCall(
                id="query-delivery-create-b",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "create", "name": "依赖分析", "message": create_messages[1]},
            ),
        ]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "query-delivery-parent-request",
                "message": "Create two independent child tasks and wait for their results.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        waiting = await runtime.run_next(independent_only=False)
        assert waiting is not None
        assert waiting.id == parent_work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                        .order_by(SessionReplyWorkItem.created_at, SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(children) == 2
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 2
            calls_by_message = {call.arguments["message"]: call for call in calls}
            assert set(calls_by_message) == set(create_messages)
            first_child, second_child = children
            assert first_child.id is not None and second_child.id is not None
            message_by_child_id = {call.child_work_id: call.arguments["message"] for call in calls}
            instruction_a = message_by_child_id[first_child.id]
            instruction_b = message_by_child_id[second_child.id]
            call_a = calls_by_message[instruction_a]
            call_b = calls_by_message[instruction_b]
            assert call_a.id is not None and call_b.id is not None
            assert call_a.tool_result_message_id is not None and call_b.tool_result_message_id is not None
            assert call_a.delivered is False and call_b.delivered is False
            child_by_id = {child.id: child for child in children}
            assert call_a.child_work_id in child_by_id and call_b.child_work_id in child_by_id
            child_a_work_id = call_a.child_work_id
            child_b_work_id = call_b.child_work_id
            b_call_id = call_b.id
            b_result_message_id = call_b.tool_result_message_id
            b_subagent_id = call_b.subagent_id
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)} == {child_a_work_id, child_b_work_id}

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=summary_a))
        child_a_done = await runtime.run_next(independent_only=True)
        assert child_a_done is not None
        assert child_a_done.id == child_a_work_id
        assert child_a_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            call_a = await db.get(SubagentToolCall, call_a.id)
            call_b = await db.get(SubagentToolCall, b_call_id)
            assert parent is not None and call_a is not None and call_b is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert call_a.delivered is True
            assert call_b.delivered is False
            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
            assert len(markers) == 1
            assert summary_a in (markers[0].content or "")
            assert summary_b not in (markers[0].content or "")
            if with_todo_snapshot:
                todo_write = await session_todo_crud.write(
                    db,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    todos=[{"content": "verify-child-delivery", "status": "in_progress"}],
                    expected_revision=0,
                )
                assert todo_write.written is True
                assert todo_write.plan is not None
                assert todo_write.plan.revision == 1
                assert todo_write.plan.todos == [{"content": "verify-child-delivery", "status": "in_progress"}]

        query_arguments = {"operation": "query_result", "subagent_id": b_subagent_id}
        if query_with_work_id:
            query_arguments["work_id"] = child_b_work_id
        query_call = InternalToolCall(
            id=f"query-delivery-query-{query_with_work_id}",
            name=AGENT_WORK_TOOL_NAME,
            arguments=query_arguments,
        )
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
            nonlocal query_wrapper_calls
            query_wrapper_calls += 1
            assert query_wrapper_calls == 1
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=summary_b))
            child_b_done = await runtime.run_next(independent_only=True)
            assert child_b_done is not None
            assert child_b_done.id == child_b_work_id
            assert child_b_done.status == SessionReplyWorkStatus.SUCCEEDED

            query_result = await original_query_subagent_result(
                db,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                subagent_id=subagent_id,
                work_id=work_id,
            )
            assert query_result["work"]["completed"] is True
            assert query_result["work"]["result"]["content"] == summary_b
            async with runtime.factory() as check_db:
                parent = await check_db.get(SessionReplyWorkItem, parent_work_id)
                call_b = await check_db.get(SubagentToolCall, b_call_id)
                assert parent is not None and call_b is not None
                assert parent.status == SessionReplyWorkStatus.RUNNING
                assert call_b.delivered is False
                waits = parent.execution_state.get("child_work_waits")
                assert isinstance(waits, list)
                assert any(slot.get("child_work_id") == child_b_work_id for slot in waits if isinstance(slot, dict))
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_summary))
            return query_result

        monkeypatch.setattr(child_work_module, "query_subagent_result", coordinate_query)
        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query_call]))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert query_wrapper_calls == 1

        followup_payloads = runtime.provider_payloads[parent_provider_start:]
        child_payload_indexes = [index for index, payload in enumerate(followup_payloads) if _has_user_instruction(payload, instruction_b)]
        assert len(child_payload_indexes) == 1
        parent_payloads = [payload for index, payload in enumerate(followup_payloads) if index not in child_payload_indexes]
        assert len(parent_payloads) == 2
        query_payloads = [payload for payload in parent_payloads if summary_b not in json.dumps(payload, ensure_ascii=False)]
        final_payloads = [payload for payload in parent_payloads if summary_b in json.dumps(payload, ensure_ascii=False)]
        assert len(query_payloads) == 1
        assert len(final_payloads) == 1
        final_payload_dump = json.dumps(final_payloads[0], ensure_ascii=False)
        final_query_items = []
        for item in _provider_items(final_payloads[0]):
            if item.get("type") != "function_call" or item.get("name") != AGENT_WORK_TOOL_NAME:
                continue
            if json.loads(item["arguments"]) == query_arguments:
                final_query_items.append(item)
        assert len(final_query_items) == 1
        assert json.loads(final_query_items[0]["arguments"]) == query_arguments
        assert summary_b in final_payload_dump
        b_create_outputs = []
        for item in _provider_items(final_payloads[0]):
            if item.get("type") != "function_call_output":
                continue
            output_payload, _ = json.JSONDecoder().raw_decode(item["output"])
            if output_payload.get("operation") == "create" and output_payload.get("child_work_id") == child_b_work_id:
                b_create_outputs.append(output_payload)
        assert len(b_create_outputs) == 1
        b_create_output = b_create_outputs[0]
        assert b_create_output["status"] == "success"
        assert b_create_output["work"]["completed"] is True
        assert b_create_output["work"]["result"]["content"] == summary_b

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            call_rows = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert not parent.execution_state.get("child_work_waits")
            assert len(call_rows) == 2
            assert {call.arguments["operation"] for call in call_rows} == {"create"}
            call_b = next(call for call in call_rows if call.id == b_call_id)
            assert call_b.delivered is True
            assert call_b.tool_result_message_id == b_result_message_id
            b_result_row = await db.get(Message, b_result_message_id)
            assert b_result_row is not None
            b_result_message = InternalMessage.model_validate_json(b_result_row.content or "{}")
            b_result_payload = json.loads(b_result_message.content or "{}")
            assert b_result_message.tool_call_id == call_b.tool_call_id
            assert b_result_message.tool_execution_status == "succeeded"
            assert b_result_payload["operation"] == "create"
            assert b_result_payload["work"]["completed"] is True
            assert b_result_payload["work"]["result"]["content"] == summary_b

            b_round = await db.get(SubagentWork, child_b_work_id)
            assert b_round is not None and b_round.result is not None
            assert b_round.result["result"]["content"] == summary_b

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            checkpoint_messages = [InternalMessage.model_validate(item) for item in checkpoint["messages"]]
            persisted_query_calls = [tool_call for message in checkpoint_messages if message.role == MessageRole.ASSISTANT for tool_call in message.tool_calls or [] if tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments == query_arguments]
            assert len(persisted_query_calls) == 1
            actual_query_call_id = persisted_query_calls[0].id
            query_messages = [message for message in checkpoint_messages if message.role == MessageRole.TOOL and message.tool_call_id == actual_query_call_id]
            assert len(query_messages) == 1
            query_content = query_messages[0].content or ""
            query_payload, query_payload_end = json.JSONDecoder().raw_decode(query_content)
            query_snapshot = query_content[query_payload_end:]
            assert query_payload["operation"] == "query_result"
            assert query_payload["work"]["completed"] is True
            assert query_payload["work"]["result"]["content"] == summary_b
            if with_todo_snapshot:
                expected_snapshot = '\n\n<current_session_todo_snapshot>{"revision":1,"todos":[{"content":"verify-child-delivery","status":"in_progress"}]}</current_session_todo_snapshot>'
                assert query_snapshot == expected_snapshot
            else:
                assert query_snapshot == ""
            checkpoint_message_lists = [checkpoint_messages]
            turn_messages = checkpoint.get("turn_messages")
            if isinstance(turn_messages, list):
                checkpoint_message_lists.append([InternalMessage.model_validate(item) for item in turn_messages])
            checkpoint_b_results = []
            for message_list in checkpoint_message_lists:
                for message in message_list:
                    if message.role == MessageRole.TOOL and message.tool_call_id == call_b.tool_call_id:
                        checkpoint_b_results.append(message)
            assert checkpoint_b_results
            for result_message in checkpoint_b_results:
                assert result_message.tool_execution_status == "succeeded"
                result_payload = json.loads(result_message.content or "{}")
                assert result_payload["status"] == "success"
                assert result_payload["work"]["completed"] is True
                assert result_payload["work"]["result"]["content"] == summary_b

            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
            assert len(markers) == 1
            assert summary_a in (markers[0].content or "")
            assert summary_b not in (markers[0].content or "")
            marker_snapshot = [(marker.id, marker.dedupe_key, marker.content) for marker in markers]

            events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id).order_by(SessionEvent.id))).scalars().all()
            parent_ready_events = [event for event in events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
            assert len(parent_ready_events) == 1
            event_snapshot = [(event.id, event.dedupe_key, deepcopy(event.event)) for event in events]

        async with runtime.factory() as db:
            assert await deliver_child_work_results(db, child_work_id=child_b_work_id) == 0
        async with runtime.factory() as db:
            assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False
        async with runtime.factory() as db:
            assert await reconcile_child_work(db) == 0

        provider_count = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count
        assert not runtime.outputs

        async with runtime.factory() as db:
            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
            events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id).order_by(SessionEvent.id))).scalars().all()
            assert [(marker.id, marker.dedupe_key, marker.content) for marker in markers] == marker_snapshot
            assert [(event.id, event.dedupe_key, event.event) for event in events] == event_snapshot
            call_rows = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(call_rows) == 2
            assert all(call.arguments["operation"] == "create" and call.delivered is True for call in call_rows)
