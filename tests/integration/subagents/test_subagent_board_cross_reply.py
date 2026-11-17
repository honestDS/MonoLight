import asyncio
import json
from copy import deepcopy

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.child_work as child_work_module
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_SUBAGENT_CANCELLED,
    SUBAGENT_RESULT_NOTIFICATION_KIND,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.i18n import t
from app.core.prompts import SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT
from app.core.session_reply_queue.child_delivery import build_subagent_notification_key
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentToolCall, SubagentWork
from tests.integration.subagents.subagent_board_workflow_support import (
    _board_snapshot,
    _create_case,
    _notification_snapshot,
    _payload,
    _provider_function_call_output_payloads,
    _submit,
)


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
                "status": "queued",
            }

            send_result = await db.get(Message, send_call_row.tool_result_message_id)
            assert send_result is not None
            send_result_message, send_result_payload = _payload(send_result)
            assert send_result_message.tool_call_id == send_call_row.tool_call_id
            assert send_result_message.tool_execution_status == "succeeded"
            assert send_result_payload == {
                "subagent_id": old_child["subagent_id"],
                "work_id": new_child_id,
                "status": "queued",
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
                "status": "queued",
            }

            send_result = await db.get(Message, send_call_row.tool_result_message_id)
            assert send_result is not None
            _, send_payload = _payload(send_result)
            assert send_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": new_child_id,
                "status": "queued",
            }

        assert await runtime.run_next(independent_only=False) is None


@pytest.mark.parametrize(
    "interrupt_after_cancel_save",
    [False, True],
    ids=["normal", "recover-after-cancel-result-save"],
)
@pytest.mark.asyncio
async def test_same_batch_send_message_and_cancel_do_not_wait_for_cancelled_child(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after_cancel_save: bool,
) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module

    parent_final = f"same-batch-send-cancel-parent-final-{interrupt_after_cancel_save}"

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES") as case:
        runtime = case.runtime
        child = case.children[0]
        parent_work_id = await _submit(
            runtime,
            request_id=f"same-batch-send-cancel-{interrupt_after_cancel_save}",
            message="Continue the existing child, cancel it, and answer this foreground reply.",
        )
        assert parent_work_id != case.origin_work_id

        send_arguments = {
            "operation": "send_message",
            "subagent_id": child["subagent_id"],
            "message": "Continue the existing child before it is cancelled.",
        }
        cancel_arguments = {
            "operation": "cancel",
            "subagent_id": child["subagent_id"],
            "work_id": child["work_id"],
        }
        send_call = InternalToolCall(
            id=f"same-batch-send-{parent_work_id}",
            name=AGENT_WORK_TOOL_NAME,
            arguments=send_arguments,
        )
        cancel_call = InternalToolCall(
            id=f"same-batch-cancel-{parent_work_id}",
            name=AGENT_WORK_TOOL_NAME,
            arguments=cancel_arguments,
        )

        send_result_saved = asyncio.Event()
        cancel_result_saved = False
        original_save_tool_response = interactive_tools_module.save_tool_response
        original_cancel_subagent = child_work_module.cancel_subagent

        async def observe_tool_response(*args, **kwargs):
            nonlocal cancel_result_saved
            tool_result = kwargs.get("tool_res")
            if tool_result is None and len(args) > 4:
                tool_result = args[4]
            payload = None
            content = getattr(tool_result, "content", None)
            if isinstance(content, str):
                try:
                    payload, _ = json.JSONDecoder().raw_decode(content)
                except (TypeError, ValueError):
                    payload = None

            stored_tool_result = await original_save_tool_response(*args, **kwargs)
            target_payload = None
            if isinstance(payload, dict) and payload.get("subagent_id") == child["subagent_id"] and payload.get("work_id") == child["work_id"]:
                target_payload = payload
            if target_payload is not None and target_payload.get("status") == "queued":
                send_result_saved.set()
            if target_payload is not None and target_payload.get("status") == "cancelled":
                cancel_result_saved = True
                if interrupt_after_cancel_save:
                    raise asyncio.CancelledError("interrupted after saving the cancel result")
            return stored_tool_result

        async def wait_for_send_result_before_cancel(*args, **kwargs):
            assert kwargs.get("work_id") == child["work_id"]
            await asyncio.wait_for(send_result_saved.wait(), timeout=5)
            return await original_cancel_subagent(*args, **kwargs)

        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call, cancel_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=parent_final),
            ]
        )
        provider_start = len(runtime.provider_payloads)
        with monkeypatch.context() as patch:
            patch.setattr(interactive_tools_module, "save_tool_response", observe_tool_response)
            patch.setattr(child_work_module, "cancel_subagent", wait_for_send_result_before_cancel)
            if interrupt_after_cancel_save:
                with pytest.raises(asyncio.CancelledError):
                    await runtime.run_next(independent_only=False)
            else:
                parent_done = await runtime.run_next(independent_only=False)

        assert send_result_saved.is_set()
        assert cancel_result_saved

        if interrupt_after_cancel_save:
            assert len(runtime.provider_payloads) == provider_start + 1
            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.RUNNING
                parent.lock_until = 0
                await db.commit()

            async with runtime.factory() as db:
                # Another worker claims the released lease before compensation runs.
                recovered_count, terminal_claims = await session_reply_work_item_crud.recover_expired(db)
                assert recovered_count == 1
                assert terminal_claims == []
            parent_done = await runtime.run_next(independent_only=False)

        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == provider_start + 2

        final_request = runtime.provider_payloads[provider_start + 1]
        assert _provider_function_call_output_payloads(final_request, name=AGENT_WORK_TOOL_NAME, arguments=send_arguments) == [
            {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "queued",
            }
        ]
        assert _provider_function_call_output_payloads(final_request, name=AGENT_WORK_TOOL_NAME, arguments=cancel_arguments) == [
            {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "cancelled",
                "reason": t(ERR_SUBAGENT_CANCELLED),
            }
        ]

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child_row = await db.get(SessionReplyWorkItem, child["work_id"])
            round_row = await db.get(SubagentWork, child["work_id"])
            send_calls = (
                (
                    await db.execute(
                        select(SubagentToolCall)
                        .where(
                            SubagentToolCall.parent_work_id == parent_work_id,
                            SubagentToolCall.subagent_id == child["subagent_id"],
                        )
                        .order_by(SubagentToolCall.id)
                    )
                )
                .scalars()
                .all()
            )
            assert parent is not None and child_row is not None and round_row is not None
            assert parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert parent.result_message_id is not None
            assert not parent.execution_state.get("child_work_waits")
            final_message = await db.get(Message, parent.result_message_id)
            assert final_message is not None and final_message.content == parent_final
            assert child_row.status == SessionReplyWorkStatus.CANCELLED
            assert round_row.result is not None
            assert round_row.result["completed"] is True
            assert round_row.result["status"] == "cancelled"
            assert round_row.result["error"] == t(ERR_SUBAGENT_CANCELLED)
            assert len(send_calls) == 1
            send_call_row = send_calls[0]
            assert send_call_row.arguments == send_arguments
            assert send_call_row.child_work_id == child["work_id"]
            assert send_call_row.delivered is True
            assert send_call_row.tool_result_message_id is not None

            send_result = await db.get(Message, send_call_row.tool_result_message_id)
            assert send_result is not None
            send_result_message, send_result_payload = _payload(send_result)
            assert send_result_message.tool_call_id == send_call_row.tool_call_id
            assert send_result_message.tool_execution_status == "succeeded"
            assert send_result_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "queued",
            }

            assert send_call_row.id is not None
            cancel_notification_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.type == MessageType.SUBAGENT_RESULT,
                            Message.dedupe_key == build_subagent_notification_key(parent, SUBAGENT_RESULT_NOTIFICATION_KIND, f"{send_call_row.subagent_id}:{send_call_row.tool_call_id}"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert cancel_notification_rows == []

            markers, events = await _notification_snapshot(db, session_id=runtime.session_id)
            notification_before_reconcile = (markers, events)
            assert await reconcile_child_work(db) == 0
            assert await _notification_snapshot(db, session_id=runtime.session_id) == notification_before_reconcile

        provider_count = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count
