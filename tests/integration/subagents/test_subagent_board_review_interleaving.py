import asyncio
import json

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.child_work as child_work_module
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    END_SESSION_TOOL_NAME,
    FILE_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue.executor_lifecycle import execute_session_reply_work, fail_session_reply_work
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentToolCall
from tests.integration.subagents.subagent_board_workflow_support import (
    _create_case,
    _notification_snapshot,
    _provider_function_call_output_payloads,
    _provider_subagent_resume_results,
    _submit,
)
from tests.integration.subagents.test_subagent_board_review_context import _persist_summary


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("summary", [0, 1], ids=["no-summary", "summary"])
@pytest.mark.asyncio
async def test_board_review_interleaving_discards_stale_tool_generation(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    outcome: str,
    summary: int,
) -> None:
    from app.transformers.openai import OpenAIResponsesTransformer

    case_id = f"{outcome}-{summary}"
    child_final = f"interleaving-child-final-{case_id}"
    child_error = "child-error-marker"
    user_message = f"Cancel the still-running child and report its cancellation for {case_id}." if outcome == "cancelled" else f"Review the latest child result for {case_id} and report the relevant update."
    reported_marker = f"interleaving-result-reported-{case_id}"
    user_report = {
        "succeeded": f"The child completed with result {child_final}.",
        "failed": f"The child failed with reason {child_error}.",
        "cancelled": "The child cancellation completed.",
    }[outcome]
    user_final = f"{user_report} This result was already reported. Report marker: {reported_marker}."
    write_path = (tmp_path / "stale-write.txt").resolve()
    write_call = InternalToolCall(
        id=f"interleaving-stale-write-{case_id}",
        name=FILE_TOOL_NAME,
        arguments={"operation": "write", "path": str(write_path), "content": f"stale-content-{case_id}"},
    )
    release_parent_request = asyncio.Event()
    parent_request_started = asyncio.Event()
    parent_task: asyncio.Task | None = None
    transformer = OpenAIResponsesTransformer

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES") as case:
        runtime = case.runtime
        child = case.children[0]
        child_worker_id = f"interleaving-child-worker-{case_id}"
        fixture_post_json = transformer._post_json

        try:
            async with runtime.factory() as db:
                claimed_child = await session_reply_work_item_crud.claim_next(
                    db,
                    worker_id=child_worker_id,
                    lease_seconds=300,
                    independent_only=True,
                )
                assert claimed_child is not None
                assert claimed_child.id == child["work_id"]
                assert claimed_child.status == SessionReplyWorkStatus.RUNNING
                db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child["work_id"], child_worker_id)
                assert await child_work_module.post_subagent_board(
                    db,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    profile_id=runtime.profile.id,
                    stage="investigating",
                    content=f"Interleaving progress for {case_id}.",
                ) == {"status": "saved"}

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                running_child = await db.get(SessionReplyWorkItem, child["work_id"])
                assert parent is not None and running_child is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert running_child.status == SessionReplyWorkStatus.RUNNING
                assert running_child.source_type == SessionReplySourceType.PARENT_WORK
                assert running_child.source_id == str(case.origin_work_id)

            async def hold_parent_post_json(_transformer, **kwargs):
                response = await fixture_post_json(_transformer, **kwargs)
                if not parent_request_started.is_set():
                    parent_request_started.set()
                    await release_parent_request.wait()
                return response

            monkeypatch.setattr(transformer, "_post_json", hold_parent_post_json)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[write_call]))
            parent_task = asyncio.create_task(runtime.run_next(independent_only=False))
            await asyncio.wait_for(parent_request_started.wait(), timeout=5)

            user_work_id = await asyncio.wait_for(
                _submit(
                    runtime,
                    request_id=f"interleaving-user-request-{case_id}",
                    message=user_message,
                    expected_status="queued",
                ),
                timeout=5,
            )
            assert user_work_id != case.origin_work_id
            assert not write_path.exists()

            release_parent_request.set()
            yielded_parent = await asyncio.wait_for(parent_task, timeout=5)
            parent_task = None
            assert yielded_parent is not None
            assert yielded_parent.id == case.origin_work_id
            assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert not runtime.outputs
            assert not write_path.exists()
            monkeypatch.setattr(transformer, "_post_json", fixture_post_json)

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                queued_user = await db.get(SessionReplyWorkItem, user_work_id)
                assert parent is not None and queued_user is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert queued_user.status == SessionReplyWorkStatus.READY_FOR_LLM
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert checkpoint.get("yield_to_user_pending") is True
                pending_generation = checkpoint.get("pending_generation")
                assert isinstance(pending_generation, dict)
                pending_result = pending_generation.get("result")
                assert isinstance(pending_result, dict)
                pending_message = pending_result.get("message")
                assert isinstance(pending_message, dict)
                pending_tool_calls = pending_message.get("tool_calls")
                assert isinstance(pending_tool_calls, list)
                assert any(isinstance(tool_call, dict) and tool_call.get("name") == FILE_TOOL_NAME and tool_call.get("arguments") == write_call.arguments for tool_call in pending_tool_calls)

            if outcome == "succeeded":
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
                await asyncio.wait_for(execute_session_reply_work(child["work_id"], child_worker_id), timeout=5)
            elif outcome == "failed":
                await asyncio.wait_for(fail_session_reply_work(child["work_id"], child_worker_id, child_error), timeout=5)
            assert not runtime.outputs

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                child_work = await db.get(SessionReplyWorkItem, child["work_id"])
                assert child_work is not None
                if outcome == "succeeded":
                    assert child_work.status == SessionReplyWorkStatus.SUCCEEDED
                elif outcome == "failed":
                    assert child_work.status == SessionReplyWorkStatus.FAILED
                    assert child_work.error == child_error
                else:
                    assert child_work.status == SessionReplyWorkStatus.RUNNING

            if outcome == "cancelled":
                cancel_arguments = {
                    "operation": "cancel",
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                }
                runtime.outputs.extend(
                    [
                        InternalMessage(
                            role=MessageRole.ASSISTANT,
                            tool_calls=[
                                InternalToolCall(
                                    id=f"interleaving-cancel-{case_id}",
                                    name=AGENT_WORK_TOOL_NAME,
                                    arguments=cancel_arguments,
                                )
                            ],
                        ),
                        InternalMessage(role=MessageRole.ASSISTANT, content=user_final),
                    ]
                )
            else:
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))

            user_provider_start = len(runtime.provider_payloads)
            user_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert user_done is not None
            assert user_done.id == user_work_id
            assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[user_provider_start:]) == (2 if outcome == "cancelled" else 1)
            assert not runtime.outputs

            if outcome == "cancelled":
                cancel_outputs = _provider_function_call_output_payloads(
                    runtime.provider_payloads[user_provider_start + 1],
                    name=AGENT_WORK_TOOL_NAME,
                    arguments=cancel_arguments,
                )
                assert len(cancel_outputs) == 1
                assert cancel_outputs[0].get("status") == "cancelled"

            user_status = await asyncio.wait_for(runtime.client.get(f"/api/v1/chat/reply-works/{user_work_id}"), timeout=5)
            assert user_status.status_code == 200
            user_data = user_status.json()["data"]
            assert user_data["request_ids"] == [f"interleaving-user-request-{case_id}"]
            assert user_data["response"]["choices"][0]["message"]["content"] == user_final

            history_response = await asyncio.wait_for(
                runtime.client.get(
                    "/api/v1/chat/sessions/history",
                    params={"session_id": runtime.session_id, "size": 100},
                ),
                timeout=5,
            )
            assert history_response.status_code == 200
            history = history_response.json()["data"]
            assert sum(item.get("content") == user_final for item in history) == 1

            if summary:
                summary_text = f"Task purpose: verify and report the original child task result. The {outcome} result was already reported. Report marker: {reported_marker}."
                assert user_final not in summary_text
                await asyncio.wait_for(_persist_summary(runtime, work_id=user_work_id, summary_text=summary_text), timeout=5)

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert parent.result_message_id is None
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert checkpoint.get("yield_to_user_pending") is True
                assert isinstance(checkpoint.get("pending_generation"), dict)

                child_work = await db.get(SessionReplyWorkItem, child["work_id"])
                assert child_work is not None
                expected_status = {
                    "succeeded": SessionReplyWorkStatus.SUCCEEDED,
                    "failed": SessionReplyWorkStatus.FAILED,
                    "cancelled": SessionReplyWorkStatus.CANCELLED,
                }[outcome]
                assert child_work.status == expected_status
                child_error = child_work.error
                child_calls = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == case.origin_work_id,
                                SubagentToolCall.child_work_id == child["work_id"],
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(child_calls) == 1
                assert child_calls[0].delivered is True

            async with runtime.factory() as db:
                notification_before_resume = await asyncio.wait_for(
                    _notification_snapshot(db, session_id=runtime.session_id),
                    timeout=5,
                )
            external_notifications = []
            for _, _, content in notification_before_resume[0]:
                payload = json.loads(content)
                if payload.get("type") != "external_work_resume" or payload.get("source") != "subagent":
                    continue
                results = payload.get("results")
                if isinstance(results, list):
                    external_notifications.extend(result for result in results if isinstance(result, dict) and result.get("subagent_id") == child["subagent_id"] and result.get("work_id") == child["work_id"])
            assert len(external_notifications) == 1
            expected_notification = {"subagent_id": child["subagent_id"], "work_id": child["work_id"], "status": outcome}
            if outcome == "succeeded":
                expected_notification["content"] = child_final
            else:
                expected_notification["reason"] = child_error
            assert external_notifications[0] == expected_notification

            end_session_call = InternalToolCall(
                id=f"interleaving-end-session-{case_id}",
                name=END_SESSION_TOOL_NAME,
                arguments={"skip_reply": True},
            )
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[end_session_call]))
            resume_provider_start = len(runtime.provider_payloads)
            resumed_parent = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert resumed_parent is not None
            assert resumed_parent.id == case.origin_work_id
            assert resumed_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[resume_provider_start:]) == 1
            assert not runtime.outputs

            resume_payload = runtime.provider_payloads[resume_provider_start]
            resume_dump = json.dumps(resume_payload, ensure_ascii=False)
            if summary:
                assert summary_text in resume_dump
                assert reported_marker in resume_dump
                assert user_final not in resume_dump
            else:
                assert user_message in resume_dump
                assert user_final in resume_dump
                assert reported_marker in resume_dump

            resume_results = _provider_subagent_resume_results(resume_payload)
            assert len(resume_results) == 1
            assert resume_results[0] == expected_notification

            status_response = await asyncio.wait_for(
                runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}"),
                timeout=5,
            )
            assert status_response.status_code == 200
            status_data = status_response.json()["data"]
            assert status_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
            assert status_data["result_message_id"] is None
            assert status_data["response"]["skip_reply"] is True

            async with runtime.factory() as db:
                notification_after_resume = await asyncio.wait_for(
                    _notification_snapshot(db, session_id=runtime.session_id),
                    timeout=5,
                )
            assert notification_after_resume[0] == notification_before_resume[0]

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.SUCCEEDED
                assert parent.result_message_id is None
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert "pending_generation" not in checkpoint
                assert checkpoint.get("yield_to_user_pending") is not True

                tool_call_rows = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == runtime.session_id,
                                Message.uid == runtime.uid,
                                Message.profile_id == runtime.profile.id,
                                Message.type == MessageType.TOOL_CALL,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                stale_calls = []
                for row in tool_call_rows:
                    persisted_message = InternalMessage.model_validate_json(row.content or "{}")
                    stale_calls.extend(call for call in persisted_message.tool_calls or [] if call.name == FILE_TOOL_NAME and call.arguments == write_call.arguments)
                assert stale_calls == []

            assert not write_path.exists()
        finally:
            release_parent_request.set()
            monkeypatch.setattr(transformer, "_post_json", fixture_post_json)
            if parent_task is not None:
                if not parent_task.done():
                    parent_task.cancel()
                await asyncio.wait_for(asyncio.gather(parent_task, return_exceptions=True), timeout=5)


@pytest.mark.asyncio
async def test_board_review_interruption_restores_consumed_notice_and_background_image(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.background_task import BackgroundTask, BackgroundTaskStatus
    from app.transformers.openai import OpenAIResponsesTransformer

    case_id = "interleaving-notice-with-background-image"
    child_final = f"interleaving-notice-child-final-{case_id}"
    image_prompt = f"interleaving-notice-image-prompt-{case_id}"
    user_message = f"The child completed and its result was already reported. Handle this update for {case_id}."
    reported_marker = f"interleaving-notice-reported-{case_id}"
    user_final = f"The child completed, and its result was already reported. Marker: {reported_marker}."
    stale_parent_text = f"Stale pre-interruption parent answer that must not be delivered: {case_id}."
    fresh_parent_final = f"The child review is complete, and the generated image is attached for {case_id}."
    image_call = InternalToolCall(
        id=f"interleaving-notice-image-call-{case_id}",
        name="generate_image",
        arguments={"prompt": image_prompt},
    )
    release_second_parent_request = asyncio.Event()
    second_parent_request_started = asyncio.Event()
    parent_task: asyncio.Task | None = None
    transformer = OpenAIResponsesTransformer
    parent_post_count = 0

    def background_result_payloads(provider_payload: dict) -> list[dict]:
        input_items = provider_payload.get("input")
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
                    payload = json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("user_message"), str):
                    try:
                        payload = json.loads(payload["user_message"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "background_tool_result":
                    results.append(payload)
                elif payload.get("type") == "external_work_resume":
                    tasks = payload.get("tasks")
                    if isinstance(tasks, list):
                        results.extend(task for task in tasks if isinstance(task, dict) and task.get("type") == "background_tool_result")
        return results

    async def hold_second_parent_post(_transformer, **kwargs):
        nonlocal parent_post_count
        parent_post_count += 1
        response = await fixture_post_json(_transformer, **kwargs)
        if parent_post_count == 2:
            second_parent_request_started.set()
            await release_second_parent_request.wait()
        return response

    async with _create_case(
        image_chat_runtime,
        protocol="OPENAI_RESPONSES",
        extra_tool_calls=[image_call],
    ) as case:
        runtime = case.runtime
        fixture_post_json = transformer._post_json
        child = case.children[0]
        try:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert not runtime.outputs

            await asyncio.wait_for(runtime.complete_images(), timeout=5)

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                child_work = await db.get(SessionReplyWorkItem, child["work_id"])
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
                assert parent is not None and child_work is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert parent.result_message_id is None
                assert child_work.status == SessionReplyWorkStatus.SUCCEEDED
                assert child_work.source_type == SessionReplySourceType.PARENT_WORK
                assert child_work.source_id == str(case.origin_work_id)
                assert image_task.id is not None
                assert image_task.status == BackgroundTaskStatus.SUCCEEDED
                assert image_task.result is not None
                image_content = image_task.result.get("content")
                assert isinstance(image_content, dict)
                send_file_result = image_content.get("send_file_to_user")
                assert isinstance(send_file_result, dict)
                image_files = send_file_result.get("files")
                assert isinstance(image_files, list) and len(image_files) == 1
                image_file = image_files[0]
                assert isinstance(image_file, dict)
                image_display_name = image_file.get("display_name") or image_file.get("name")
                assert isinstance(image_display_name, str) and image_display_name
                image_path = image_file.get("path")
                image_mime_type = image_file.get("mime_type")
                assert isinstance(image_path, str) and image_path
                assert isinstance(image_mime_type, str) and image_mime_type

                calls = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == case.origin_work_id,
                                SubagentToolCall.child_work_id == child["work_id"],
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(calls) == 1
                assert calls[0].delivered is True
                child_call_snapshot = (
                    calls[0].id,
                    calls[0].parent_work_id,
                    calls[0].child_work_id,
                    calls[0].tool_call_id,
                    calls[0].tool_result_message_id,
                    calls[0].delivered,
                )
                notifications_before_review = await _notification_snapshot(db, session_id=runtime.session_id)
                assert len(notifications_before_review[0]) == 1
                child_notice_id = notifications_before_review[0][0][0]
                child_notice = json.loads(notifications_before_review[0][0][2])
                expected_child_notice = {
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                    "status": "succeeded",
                    "content": child_final,
                }
                assert child_notice.get("source") == "subagent"
                assert child_notice.get("results") == [expected_child_notice]
                image_task_id = image_task.id

            board_read_call = InternalToolCall(
                id=f"interleaving-notice-board-read-{case_id}",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "board_read", "subagent_id": child["subagent_id"]},
            )
            send_image_call = InternalToolCall(
                id=f"interleaving-notice-send-image-{case_id}",
                name="send_file_to_user",
                arguments={
                    "files": [
                        {
                            "path": image_path,
                            "display_name": image_display_name,
                            "mime_type": image_mime_type,
                        }
                    ]
                },
            )
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[board_read_call, send_image_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, content=stale_parent_text),
                ]
            )
            monkeypatch.setattr(transformer, "_post_json", hold_second_parent_post)
            review_provider_start = len(runtime.provider_payloads)
            parent_task = asyncio.create_task(runtime.run_next(independent_only=False))
            await asyncio.wait_for(second_parent_request_started.wait(), timeout=5)
            assert parent_post_count == 2

            user_work_id = await asyncio.wait_for(
                _submit(
                    runtime,
                    request_id=f"interleaving-notice-user-request-{case_id}",
                    message=user_message,
                    expected_status="queued",
                ),
                timeout=5,
            )
            assert user_work_id != case.origin_work_id

            release_second_parent_request.set()
            yielded_parent = await asyncio.wait_for(parent_task, timeout=5)
            parent_task = None
            monkeypatch.setattr(transformer, "_post_json", fixture_post_json)
            assert yielded_parent is not None
            assert yielded_parent.id == case.origin_work_id
            assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert not runtime.outputs

            old_review_payloads = runtime.provider_payloads[review_provider_start:]
            assert len(old_review_payloads) == 2
            expected_child_notice = {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }
            assert _provider_subagent_resume_results(old_review_payloads[0]) == [expected_child_notice]
            board_outputs = _provider_function_call_output_payloads(
                old_review_payloads[1],
                name=board_read_call.name,
                arguments=board_read_call.arguments,
            )
            assert len(board_outputs) == 1

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                queued_user = await db.get(SessionReplyWorkItem, user_work_id)
                assert parent is not None and queued_user is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert parent.result_message_id is None
                assert queued_user.status == SessionReplyWorkStatus.READY_FOR_LLM
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert not checkpoint.get("pending_resume_message_ids")
                pending_generation = checkpoint.get("pending_generation")
                assert isinstance(pending_generation, dict)
                pending_result = pending_generation.get("result")
                assert isinstance(pending_result, dict)
                pending_message = pending_result.get("message")
                assert isinstance(pending_message, dict)
                assert pending_message.get("content") == stale_parent_text

                checkpoint_messages = checkpoint.get("messages")
                assert isinstance(checkpoint_messages, list)
                notice_positions = [index for index, item in enumerate(checkpoint_messages) if isinstance(item, dict) and item.get("id") == child_notice_id]
                board_call_positions = [
                    index
                    for index, item in enumerate(checkpoint_messages)
                    if isinstance(item, dict) and item.get("role") == MessageRole.ASSISTANT.value and any(isinstance(tool_call, dict) and tool_call.get("name") == board_read_call.name and tool_call.get("arguments") == board_read_call.arguments for tool_call in item.get("tool_calls", []))
                ]
                assert len(notice_positions) == 1
                assert len(board_call_positions) == 1
                assert notice_positions[0] < board_call_positions[0]
                board_read_checkpoint_calls = [tool_call for tool_call in checkpoint_messages[board_call_positions[0]].get("tool_calls", []) if isinstance(tool_call, dict) and tool_call.get("name") == board_read_call.name and tool_call.get("arguments") == board_read_call.arguments]
                assert len(board_read_checkpoint_calls) == 1
                board_read_checkpoint_call_id = board_read_checkpoint_calls[0].get("id")
                assert isinstance(board_read_checkpoint_call_id, str) and board_read_checkpoint_call_id
                assert any(isinstance(item, dict) and item.get("role") == MessageRole.TOOL.value and item.get("tool_call_id") == board_read_checkpoint_call_id for item in checkpoint_messages)
                checkpoint_background_results = background_result_payloads({"input": checkpoint_messages})
                assert len(checkpoint_background_results) == 1
                assert checkpoint_background_results[0].get("task_id") == image_task_id

                files_to_user = checkpoint.get("files_to_user")
                assert isinstance(files_to_user, list)
                assert any(isinstance(file_item, dict) and (file_item.get("display_name") == image_display_name or file_item.get("name") == image_display_name) for file_item in files_to_user)

            background_results = background_result_payloads(old_review_payloads[0])
            assert len(background_results) == 1
            assert background_results[0].get("task_id") == image_task_id
            background_dump = json.dumps(background_results[0], ensure_ascii=False)
            assert image_prompt in background_dump
            assert image_display_name in background_dump

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))
            user_provider_start = len(runtime.provider_payloads)
            user_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert user_done is not None
            assert user_done.id == user_work_id
            assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[user_provider_start:]) == 1
            assert not runtime.outputs

            summary_text = f"Task purpose: review the child task and deliver the generated image. The child result was already reported; marker: {reported_marker}. The generated image remains undelivered."
            assert child_final not in summary_text
            assert user_final not in summary_text
            await asyncio.wait_for(
                _persist_summary(runtime, work_id=user_work_id, summary_text=summary_text),
                timeout=5,
            )

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                completed_user = await db.get(SessionReplyWorkItem, user_work_id)
                assert parent is not None and completed_user is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert parent.result_message_id is None
                assert completed_user.status == SessionReplyWorkStatus.SUCCEEDED
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                files_to_user = checkpoint.get("files_to_user")
                assert isinstance(files_to_user, list)
                assert any(isinstance(file_item, dict) and (file_item.get("display_name") == image_display_name or file_item.get("name") == image_display_name) for file_item in files_to_user)

            end_session_call = InternalToolCall(
                id=f"interleaving-notice-end-session-{case_id}",
                name=END_SESSION_TOOL_NAME,
                arguments={"skip_reply": True},
            )
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[end_session_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, content=fresh_parent_final),
                ]
            )
            resume_provider_start = len(runtime.provider_payloads)
            resumed_parent = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert resumed_parent is not None
            assert resumed_parent.id == case.origin_work_id
            assert resumed_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[resume_provider_start:]) == 2
            assert not runtime.outputs

            recovery_payloads = runtime.provider_payloads[resume_provider_start:]
            recovery_dump = json.dumps(recovery_payloads[0], ensure_ascii=False)
            assert summary_text in recovery_dump
            assert user_final not in recovery_dump
            assert stale_parent_text not in recovery_dump
            assert _provider_subagent_resume_results(recovery_payloads[0]) == [expected_child_notice]
            assert recovery_dump.count(child_final) == 1
            recovery_background_results = background_result_payloads(recovery_payloads[0])
            assert len(recovery_background_results) == 1
            assert recovery_background_results[0].get("task_id") == image_task_id
            recovery_background_dump = json.dumps(recovery_background_results[0], ensure_ascii=False)
            assert image_prompt in recovery_background_dump
            assert image_display_name in recovery_background_dump

            status_response = await asyncio.wait_for(
                runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}"),
                timeout=5,
            )
            assert status_response.status_code == 200
            status_data = status_response.json()["data"]
            assert status_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
            assert status_data["request_ids"] == ["inspection-origin-request"]
            response = status_data["response"]
            assert status_data["result_message_id"] is not None
            response_files = response["files"]
            assert isinstance(response_files, list) and len(response_files) == 1
            assert "path" not in response_files[0]
            assistant_files = json.loads(response["choices"][0]["message"]["content"])
            assert assistant_files["type"] == "assistant_files"
            assert assistant_files["text"] == fresh_parent_final
            assert assistant_files["files"] == response_files
            download = await asyncio.wait_for(runtime.client.get(response_files[0]["download_url"]), timeout=5)
            assert download.status_code == 200
            assert download.content == runtime.image_bytes

            history_response = await asyncio.wait_for(
                runtime.client.get(
                    "/api/v1/chat/sessions/history",
                    params={"session_id": runtime.session_id, "size": 100},
                ),
                timeout=5,
            )
            assert history_response.status_code == 200
            history_dump = json.dumps(history_response.json()["data"], ensure_ascii=False)
            assert stale_parent_text not in history_dump
            assert history_dump.count(user_final) == 1
            assert fresh_parent_final in history_dump

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                child_work = await db.get(SessionReplyWorkItem, child["work_id"])
                assert parent is not None and child_work is not None
                assert parent.status == SessionReplyWorkStatus.SUCCEEDED
                assert child_work.source_type == SessionReplySourceType.PARENT_WORK
                assert child_work.source_id == str(case.origin_work_id)
                final_calls = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == case.origin_work_id,
                                SubagentToolCall.child_work_id == child["work_id"],
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(final_calls) == 1
                assert (
                    final_calls[0].id,
                    final_calls[0].parent_work_id,
                    final_calls[0].child_work_id,
                    final_calls[0].tool_call_id,
                    final_calls[0].tool_result_message_id,
                    final_calls[0].delivered,
                ) == child_call_snapshot
                notifications_after_resume = await _notification_snapshot(db, session_id=runtime.session_id)
                assert notifications_after_resume[0] == notifications_before_review[0]
        finally:
            release_second_parent_request.set()
            monkeypatch.setattr(transformer, "_post_json", fixture_post_json)
            if parent_task is not None:
                if not parent_task.done():
                    parent_task.cancel()
                await asyncio.wait_for(asyncio.gather(parent_task, return_exceptions=True), timeout=5)
