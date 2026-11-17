import asyncio
import json

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.child_work as child_work_module
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    END_SESSION_TOOL_NAME,
    SUBAGENT_BOARD_READY_EVENT_KIND,
    SUBAGENT_WORK_READY_EVENT_KIND,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.todo import session_todo_crud
from app.core.prompts import SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT
from app.core.session_reply_queue.child_delivery import (
    build_subagent_notification_key,
    deliver_child_work_results,
    resume_child_work_for_parent,
)
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentBoardPost, SubagentToolCall, SubagentWork
from tests.integration.subagents.subagent_board_workflow_support import (
    _board_snapshot,
    _create_case,
    _notification_snapshot,
    _persisted_main_tool_call_id,
    _provider_agent_work_operations,
    _provider_function_call_output_payloads,
    _submit,
)


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
                stage_ready_key = build_subagent_notification_key(
                    parent,
                    SUBAGENT_BOARD_READY_EVENT_KIND,
                    f"{child['subagent_id']}:{stage_board[0][0]}",
                )
                result_ready_key = build_subagent_notification_key(parent, SUBAGENT_WORK_READY_EVENT_KIND, child["work_id"])
                stage_ready_events = [event for event in stage_events if event[2].get("type") == "work_ready" and event[2].get("work_id") == new_work_id]
                assert len(stage_ready_events) == 1
                assert stage_ready_events[0][1] == stage_ready_key
                assert not any(event[1] == result_ready_key for event in stage_events)

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
                send_result_payload = json.loads(send_result_message.content or "")
                assert send_result_message.tool_call_id == actual_send_call_id
                assert send_result_message.tool_execution_status == "succeeded"
                assert send_result_payload == {
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                    "status": "queued",
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
                query_payload = json.loads(query_content)
                assert query_message.tool_execution_status == "succeeded"
                assert query_payload == {
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                    "status": "succeeded",
                    "content": child_final,
                }
                assert query_message.current_session_todo == {
                    "revision": 1,
                    "todos": [{"content": "verify-send-result", "status": "in_progress"}],
                }

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
                assert parent_ready_events[0][1] == stage_ready_key
                assert not any(event[1] == result_ready_key for event in events)
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

            release_parent_request.set()
            parent_result = await parent_task
            parent_task = None
            assert parent_result is not None
            assert parent_result.id == case.origin_work_id
            assert parent_result.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert not runtime.outputs

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                user_work = await db.get(SessionReplyWorkItem, user_work_id)
                assert origin is not None and user_work is not None
                assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert origin.result_message_id is None
                assert origin.locked_by is None
                assert origin.lock_until is None
                assert origin.sequence_no > user_work.sequence_no

                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                pending_generation = checkpoint.get("pending_generation")
                assert isinstance(pending_generation, dict)
                pending_result = pending_generation.get("result")
                assert isinstance(pending_result, dict)
                pending_message = pending_result.get("message")
                assert isinstance(pending_message, dict)
                pending_tool_calls = pending_message.get("tool_calls")
                assert isinstance(pending_tool_calls, list)
                assert any(isinstance(tool_call, dict) and tool_call.get("name") == board_read_call.name and tool_call.get("arguments") == board_read_call.arguments for tool_call in pending_tool_calls)

                notification_before_user = await _notification_snapshot(db, session_id=runtime.session_id)
                markers_before_user, events_before_user = notification_before_user
                assert len(markers_before_user) == 1
                assert len([event for event in events_before_user if event[2].get("type") == "work_ready" and event[2].get("work_id") == case.origin_work_id]) == 1
                board_before_user = await _board_snapshot(db, subagent_id=child["subagent_id"], child_work_id=child["work_id"])
                assert len(board_before_user) == 2
                assert board_before_user[0][1:] == (child["subagent_id"], child["work_id"], first_stage, first_content)
                assert board_before_user[1][1:] == (child["subagent_id"], child["work_id"], second_stage, second_content)

            yielded_parent_payloads = [payload for payload in runtime.provider_payloads[parent_provider_start:] if "board_read" in (_provider_agent_work_operations(payload) or [])]
            assert len(yielded_parent_payloads) == 1

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
            assert not runtime.outputs

            user_status = await runtime.client.get(f"/api/v1/chat/reply-works/{user_work_id}")
            assert user_status.status_code == 200
            user_data = user_status.json()["data"]
            assert user_data["request_ids"] == [user_request_id]
            assert user_data["response"]["choices"][0]["message"]["content"] == user_final

            async with runtime.factory() as db:
                origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
                user_work = await db.get(SessionReplyWorkItem, user_work_id)
                assert origin is not None and user_work is not None
                assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert origin.result_message_id is None
                assert user_work.status == SessionReplyWorkStatus.SUCCEEDED
                assert user_work.result_message_id is not None
                assert user_work.execution_state.get("request_ids") == [user_request_id]
                assert user_work.input_message_ids is not None
                assert len(user_work.input_message_ids) == 1
                user_input_message = await db.get(Message, user_work.input_message_ids[0])
                assert user_input_message is not None
                assert user_input_message.content == user_message
                user_result_message = await db.get(Message, user_work.result_message_id)
                assert user_result_message is not None
                assert user_result_message.type == MessageType.TEXT
                assert user_result_message.content == user_final

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
            review_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[review_call]))
            parent_review_result = await runtime.run_next(independent_only=False)
            assert parent_review_result is not None
            assert parent_review_result.id == case.origin_work_id
            assert parent_review_result.status == SessionReplyWorkStatus.READY_FOR_LLM
            review_provider_payloads = runtime.provider_payloads[review_provider_start:]
            assert len(review_provider_payloads) == 1
            assert not runtime.outputs

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

            parent_review_payloads = [*yielded_parent_payloads, *review_provider_payloads]
            board_read_outputs = _provider_function_call_output_payloads(
                review_provider_payloads[-1],
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
