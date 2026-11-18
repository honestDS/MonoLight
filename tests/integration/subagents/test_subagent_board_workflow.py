import asyncio
import json
from contextlib import aclosing
from copy import deepcopy

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.child_work as child_work_module
from app.api.v1 import chat as chat_api
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_SUBAGENT_CANCELLED,
    ERR_SUBAGENT_RESULT_NOT_READY,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.i18n import t
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.executor_lifecycle import execute_session_reply_work
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.dispatcher.process_single_tool import get_tool_execution_status
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork
from tests.integration.subagents.subagent_board_workflow_support import (
    _board_snapshot,
    _create_case,
    _next_done_event,
    _notification_snapshot,
    _payload,
    _persisted_main_tool_call_id,
    _provider_agent_work_operations,
    _submit,
)


@pytest.mark.parametrize("operation", ["create", "send_message"], ids=["create", "send-message"])
@pytest.mark.asyncio
async def test_replaying_delivered_subagent_submission_returns_queued_result_without_mutation(
    image_chat_runtime,
    operation: str,
) -> None:
    first_child_final = f"inspection-existing-submission-first-{operation}"
    second_child_final = f"inspection-existing-submission-second-{operation}"

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES") as case:
        runtime = case.runtime
        child = case.children[0]

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_child_final))
        first_child_done = await runtime.run_next(independent_only=True)
        assert first_child_done is not None
        assert first_child_done.id == child["work_id"]
        assert first_child_done.status == SessionReplyWorkStatus.SUCCEEDED
        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            origin_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().one()
            assert origin is not None
            assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert origin_call.delivered is True

        target_parent_id = case.origin_work_id
        target_final = first_child_final
        if operation == "send_message":
            followup_work_id = await _submit(
                runtime,
                request_id="inspection-existing-submission-followup",
                message="Continue the completed child with a follow-up task.",
            )
            send_arguments = {
                "operation": "send_message",
                "subagent_id": child["subagent_id"],
                "message": "Continue the same child and report the follow-up result.",
            }
            send_call = InternalToolCall(
                id="inspection-existing-submission-send",
                name=AGENT_WORK_TOOL_NAME,
                arguments=send_arguments,
            )
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call]))
            waiting = await runtime.run_next(independent_only=False)
            assert waiting is not None
            assert waiting.id == followup_work_id
            assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            async with runtime.factory() as db:
                send_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == followup_work_id))).scalars().one()
                assert send_call_row.arguments == send_arguments
                assert send_call_row.child_work_id != child["work_id"]
                continuation_child = await db.get(SessionReplyWorkItem, send_call_row.child_work_id)
                assert continuation_child is not None
                assert continuation_child.status == SessionReplyWorkStatus.READY_FOR_LLM
                continuation_child_work_id = send_call_row.child_work_id

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_child_final))
            continuation_done = await runtime.run_next(independent_only=True)
            assert continuation_done is not None
            assert continuation_done.id == continuation_child_work_id
            assert continuation_done.status == SessionReplyWorkStatus.SUCCEEDED

            target_parent_id = followup_work_id
            target_final = second_child_final

        async with runtime.factory() as db:
            target_parent = await db.get(SessionReplyWorkItem, target_parent_id)
            assert target_parent is not None
            assert target_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            target_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == target_parent_id).order_by(SubagentToolCall.id))).scalars().one()
            assert target_call.id is not None
            assert isinstance(target_call.tool_call_id, str) and target_call.tool_call_id
            assert target_call.delivered is True
            assert target_call.tool_result_message_id is not None

            target_round = await db.get(SubagentWork, target_call.child_work_id)
            assert target_round is not None
            assert target_round.result is not None
            assert target_round.result["completed"] is True
            assert target_round.result["status"] == "succeeded"
            assert target_round.result["result"]["content"] == target_final

            target_result = await db.get(Message, target_call.tool_result_message_id)
            assert target_result is not None
            target_result_message, queued_payload = _payload(target_result)
            assert target_result_message.tool_call_id == target_call.tool_call_id
            assert target_result_message.tool_execution_status == "succeeded"
            assert queued_payload == {
                "subagent_id": target_call.subagent_id,
                "work_id": target_call.child_work_id,
                "status": "queued",
            }
            original_result_content = target_result.content
            original_result_suffix = target_result.model_context_suffix
            original_round_result = deepcopy(target_round.result)
            original_call_arguments = deepcopy(target_call.arguments)
            target_call_id = target_call.id
            target_tool_call_id = target_call.tool_call_id
            target_result_id = target_call.tool_result_message_id
            target_child_work_id = target_call.child_work_id
            target_subagent_id = target_call.subagent_id

            checkpoint_before = await session_reply_work_item_crud.get_checkpoint(db, target_parent_id)
            assert isinstance(checkpoint_before, dict)
            checkpoint_before = deepcopy(checkpoint_before)
            notification_before = await _notification_snapshot(db, session_id=runtime.session_id)
            message_count_before = len(
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            round_count_before = len((await db.execute(select(SubagentWork).where(SubagentWork.subagent_id == target_subagent_id))).scalars().all())
            call_count_before = len((await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == target_subagent_id))).scalars().all())

            worker_id = f"inspection-existing-submission-{operation}-{target_parent_id}"
            claimed = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
                independent_only=False,
            )
            assert claimed is not None
            assert claimed.id == target_parent_id
            assert claimed.status == SessionReplyWorkStatus.RUNNING
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_parent_id, worker_id)

            if operation == "create":
                retry_result = await child_work_module.create_subagent(
                    db,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    profile_id=runtime.profile.id,
                    tool_call_id=target_tool_call_id,
                    name=original_call_arguments["name"],
                    message=original_call_arguments["message"],
                )
            else:
                retry_result = await child_work_module.send_subagent_message(
                    db,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    profile_id=runtime.profile.id,
                    tool_call_id=target_tool_call_id,
                    subagent_id=original_call_arguments["subagent_id"],
                    message=original_call_arguments["message"],
                )
            assert retry_result == queued_payload
            assert target_final not in json.dumps(retry_result, ensure_ascii=False)

            target_call_after = await db.get(SubagentToolCall, target_call_id)
            assert target_call_after is not None
            assert target_call_after.arguments == original_call_arguments
            assert target_call_after.child_work_id == target_child_work_id
            assert target_call_after.delivered is True
            assert target_call_after.tool_result_message_id == target_result_id

            target_result_after = await db.get(Message, target_result_id)
            assert target_result_after is not None
            assert target_result_after.content == original_result_content
            assert target_result_after.model_context_suffix == original_result_suffix
            _, queued_payload_after = _payload(target_result_after)
            assert queued_payload_after == queued_payload

            target_round_after = await db.get(SubagentWork, target_child_work_id)
            assert target_round_after is not None
            assert target_round_after.result == original_round_result
            assert (
                len(
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == runtime.session_id,
                                Message.uid == runtime.uid,
                                Message.profile_id == runtime.profile.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                == message_count_before
            )
            assert len((await db.execute(select(SubagentWork).where(SubagentWork.subagent_id == target_subagent_id))).scalars().all()) == round_count_before
            assert len((await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == target_subagent_id))).scalars().all()) == call_count_before
            assert await session_reply_work_item_crud.get_checkpoint(db, target_parent_id) == checkpoint_before
            assert await _notification_snapshot(db, session_id=runtime.session_id) == notification_before


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
            assert original_result_message.tool_execution_status == "succeeded"
            assert original_result_payload == {
                "subagent_id": second_child["subagent_id"],
                "work_id": second_child["work_id"],
                "status": "queued",
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
        cancel_payload = json.loads(cancel_outputs[0]["output"])["tool_result"]
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


@pytest.mark.asyncio
async def test_subagent_board_http_workflow_tracks_committed_revision_and_hides_rolled_back_posts(image_chat_runtime) -> None:
    progress_stage = "investigating"
    progress_content = "The child verified the first independent check and is continuing with the remaining work."
    rolled_back_stage = "rolled-back"
    rolled_back_content = "This progress must not become visible after rollback."
    child_final = "inspection-http-board-child-final"
    worker_id = "inspection-http-board-worker"

    async def activity_row(runtime):
        response = await runtime.client.get("/api/v1/chat/sessions/activity")
        assert response.status_code == 200
        return next(row for row in response.json()["data"] if row["session_id"] == runtime.session_id)

    async def board_data(runtime):
        response = await runtime.client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": runtime.session_id},
        )
        assert response.status_code == 200
        return response.json()["data"]

    async def detail_data(runtime, subagent_id: str):
        response = await runtime.client.get(
            f"/api/v1/chat/sessions/subagents/{subagent_id}",
            params={"session_id": runtime.session_id},
        )
        assert response.status_code == 200
        return response.json()["data"]

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES") as case:
        runtime = case.runtime
        child = case.children[0]

        initial_activity = await activity_row(runtime)
        assert initial_activity["subagent_board_revision"] > 0
        assert await activity_row(runtime) == initial_activity

        initial_board = await board_data(runtime)
        initial_entry = next(entry for entry in initial_board["active"] if entry["work_id"] == child["work_id"])
        assert initial_entry["status"] == SessionReplyWorkStatus.READY_FOR_LLM.value
        assert initial_entry["completed"] is False

        async with runtime.factory() as db:
            claimed = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
                independent_only=True,
            )
            assert claimed is not None
            assert claimed.id == child["work_id"]
            assert claimed.status == SessionReplyWorkStatus.RUNNING

        claimed_activity = await activity_row(runtime)
        assert claimed_activity["subagent_board_revision"] > initial_activity["subagent_board_revision"]
        claimed_board = await board_data(runtime)
        claimed_entry = next(entry for entry in claimed_board["active"] if entry["work_id"] == child["work_id"])
        assert claimed_entry["status"] == SessionReplyWorkStatus.RUNNING.value

        async with runtime.factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child["work_id"], worker_id)
            assert await child_work_module.post_subagent_board(
                db,
                uid=runtime.uid,
                session_id=runtime.session_id,
                profile_id=runtime.profile.id,
                stage=progress_stage,
                content=progress_content,
            ) == {"status": "saved"}

        progress_activity = await activity_row(runtime)
        assert progress_activity["subagent_board_revision"] > claimed_activity["subagent_board_revision"]
        progress_board = await board_data(runtime)
        progress_entry = next(entry for entry in progress_board["active"] if entry["work_id"] == child["work_id"])
        assert progress_entry["status"] == SessionReplyWorkStatus.RUNNING.value
        assert progress_entry["stage"] == progress_stage
        assert progress_entry["preview"] == progress_content
        progress_detail = await detail_data(runtime, child["subagent_id"])
        assert progress_detail["status"] == SessionReplyWorkStatus.RUNNING.value
        assert [(post["stage"], post["content"]) for post in progress_detail["posts"]] == [(progress_stage, progress_content)]

        async with runtime.factory() as db:
            rolled_back_post = await subagent_crud.create_board_post(
                db,
                uid=runtime.uid,
                session_id=runtime.session_id,
                profile_id=runtime.profile.id,
                subagent_id=child["subagent_id"],
                child_work_id=child["work_id"],
                stage=rolled_back_stage,
                content=rolled_back_content,
            )
            assert rolled_back_post.id is not None
            await db.rollback()

        rolled_back_activity = await activity_row(runtime)
        assert rolled_back_activity["subagent_board_revision"] == progress_activity["subagent_board_revision"]
        rolled_back_detail = await detail_data(runtime, child["subagent_id"])
        assert rolled_back_detail == progress_detail
        assert rolled_back_content not in json.dumps(rolled_back_detail, ensure_ascii=False)

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
        await execute_session_reply_work(child["work_id"], worker_id)
        assert not runtime.outputs

        completed_activity = await activity_row(runtime)
        assert completed_activity["subagent_board_revision"] > rolled_back_activity["subagent_board_revision"]
        completed_board = await board_data(runtime)
        assert completed_board["active"] == []
        completed_entry = next(entry for entry in completed_board["recent"] if entry["work_id"] == child["work_id"])
        assert completed_entry["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert completed_entry["completed"] is True
        completed_detail = await detail_data(runtime, child["subagent_id"])
        assert completed_detail["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert completed_detail["completed"] is True
        assert completed_detail["result_text"] == child_final
        assert completed_detail["posts"] == progress_detail["posts"]

        assert await activity_row(runtime) == completed_activity
        assert await board_data(runtime) == completed_board
        assert await detail_data(runtime, child["subagent_id"]) == completed_detail


@pytest.mark.asyncio
async def test_http_stop_cancels_queued_child_and_preserves_board_revision_for_foreground_stop(image_chat_runtime) -> None:
    async def activity_revision(runtime) -> int:
        response = await runtime.client.get("/api/v1/chat/sessions/activity")
        assert response.status_code == 200
        row = next(item for item in response.json()["data"] if item["session_id"] == runtime.session_id)
        return row["subagent_board_revision"]

    async def board_data(runtime) -> dict:
        response = await runtime.client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": runtime.session_id},
        )
        assert response.status_code == 200
        return response.json()["data"]

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES") as case:
        runtime = case.runtime
        child = case.children[0]
        initial_revision = await activity_revision(runtime)

        first_stop = await runtime.client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": runtime.session_id},
        )
        assert first_stop.status_code == 200
        assert first_stop.json()["data"] == {
            "session_id": runtime.session_id,
            "cancelled_count": 2,
        }

        cancelled_revision = await activity_revision(runtime)
        assert cancelled_revision > initial_revision
        cancelled_entry = next(entry for entry in (await board_data(runtime))["recent"] if entry["work_id"] == child["work_id"])
        assert cancelled_entry["subagent_id"] == child["subagent_id"]
        assert cancelled_entry["status"] == SessionReplyWorkStatus.CANCELLED.value
        assert cancelled_entry["completed"] is True

        foreground_work_id = await _submit(
            runtime,
            request_id="http-stop-foreground-after-child-cancel",
            message="Answer this ordinary foreground request without running any worker.",
        )
        assert next(entry for entry in (await board_data(runtime))["recent"] if entry["work_id"] == child["work_id"]) == cancelled_entry
        before_foreground_stop_revision = await activity_revision(runtime)
        assert before_foreground_stop_revision == cancelled_revision

        async with runtime.factory() as db:
            foreground = await db.get(SessionReplyWorkItem, foreground_work_id)
            assert foreground is not None
            assert foreground.status == SessionReplyWorkStatus.READY_FOR_LLM

        second_stop = await runtime.client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": runtime.session_id},
        )
        assert second_stop.status_code == 200
        assert second_stop.json()["data"] == {
            "session_id": runtime.session_id,
            "cancelled_count": 1,
        }

        async with runtime.factory() as db:
            foreground = await db.get(SessionReplyWorkItem, foreground_work_id)
            assert foreground is not None
            assert foreground.status == SessionReplyWorkStatus.CANCELLED

        assert await activity_revision(runtime) == before_foreground_stop_revision
        assert next(entry for entry in (await board_data(runtime))["recent"] if entry["work_id"] == child["work_id"])["status"] == SessionReplyWorkStatus.CANCELLED.value


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
            assert result_row.content == queued_wait_content
            queued_wait_message = InternalMessage.model_validate_json(queued_wait_content)
            result_message, result_payload = _payload(result_row)
            assert result_message.tool_execution_status == queued_wait_message.tool_execution_status
            assert result_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "queued",
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
