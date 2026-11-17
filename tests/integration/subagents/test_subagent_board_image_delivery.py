import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import select

import app.core.background_tasks.runner as background_runner_module
import app.core.dispatchers.interactive_runtime as interactive_runtime_module
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    SEND_FILE_TO_USER_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SUBAGENT_RESULT_NOTIFICATION_KIND,
)
from app.core.paths import get_user_temp_dir
from app.core.session_reply_queue.child_delivery import build_subagent_notification_key
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.core.tools.send_file_to_user import _encode_token
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalGeneratedImage, InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentToolCall, SubagentWork
from tests.integration.subagents.subagent_board_workflow_support import (
    _create_case,
    _payload,
    _provider_function_call_output_payloads,
    _provider_subagent_resume_results,
    _subagent_external_resume_result,
    _submit,
)


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
            parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
            round_row = await db.get(SubagentWork, child["work_id"])
            create_call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == case.origin_work_id,
                            SubagentToolCall.child_work_id == child["work_id"],
                        )
                    )
                )
                .scalars()
                .one()
            )
            create_message = await db.get(Message, child["create_result_id"])
            assert work is not None and round_row is not None and create_call is not None and create_message is not None
            assert parent is not None
            assert create_call.arguments["operation"] == "create"
            assert create_call.tool_result_message_id == child["create_result_id"]
            assert work.status == SessionReplyWorkStatus.SUCCEEDED and work.result_message_id is not None and round_row.result is not None
            saved_message = await db.get(Message, work.result_message_id)
            assert saved_message is not None and saved_message.type == MessageType.TEXT
            saved_payload = json.loads(saved_message.content or "{}")
            assert saved_payload["type"] == "assistant_files" and saved_payload["text"] == "" and "path" not in saved_payload["files"][0]
            create_result_message, create_payload = _payload(create_message)
            assert create_result_message.tool_call_id == create_call.tool_call_id
            assert create_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "queued",
            }
            assert "files" not in create_payload
            assert create_call.id is not None
            assert isinstance(create_call.tool_call_id, str) and create_call.tool_call_id
            _, notification_result = await _subagent_external_resume_result(
                db,
                session_id=runtime.session_id,
                uid=runtime.uid,
                profile_id=runtime.profile.id,
                subagent_id=child["subagent_id"],
                work_id=child["work_id"],
                call_id=create_call.tool_call_id,
                dedupe_key=build_subagent_notification_key(parent, SUBAGENT_RESULT_NOTIFICATION_KIND, f"{create_call.subagent_id}:{create_call.tool_call_id}"),
            )
            assert notification_result["status"] == "succeeded"
            assert isinstance(notification_result.get("files"), list) and len(notification_result["files"]) == 1

        generated_file = notification_result["files"][0]
        assert isinstance(generated_file, dict)
        generated_path = Path(generated_file["path"])
        assert generated_path.is_absolute() and generated_path.parent == (get_user_temp_dir(Path.cwd(), runtime.uid) / "generated_images").resolve()
        assert generated_path.read_bytes() == runtime.image_bytes
        assert notification_result["files"][0]["path"] == str(generated_path)
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
        assert create_outputs == [create_payload]
        assert query_outputs == [notification_result]
        resume_results = [result for result in _provider_subagent_resume_results(requests[0]) if result.get("subagent_id") == child["subagent_id"] and result.get("work_id") == child["work_id"]]
        assert len(resume_results) == 1
        assert resume_results[0] == notification_result
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
            create_call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == case.origin_work_id,
                            SubagentToolCall.child_work_id == child["work_id"],
                        )
                    )
                )
                .scalars()
                .one()
            )
            create_message = await db.get(Message, child["create_result_id"])
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert create_message is not None
            assert create_call.arguments["operation"] == "create"
            assert create_call.tool_result_message_id == child["create_result_id"]
            create_result_message, create_payload = _payload(create_message)
            assert create_result_message.tool_call_id == create_call.tool_call_id
            assert create_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "queued",
            }
            assert "files" not in create_payload
            assert create_call.id is not None
            assert isinstance(create_call.tool_call_id, str) and create_call.tool_call_id
            notification_payload, notification_result = await _subagent_external_resume_result(
                db,
                session_id=runtime.session_id,
                uid=runtime.uid,
                profile_id=runtime.profile.id,
                subagent_id=child["subagent_id"],
                work_id=child["work_id"],
                call_id=create_call.tool_call_id,
                dedupe_key=build_subagent_notification_key(parent, SUBAGENT_RESULT_NOTIFICATION_KIND, f"{create_call.subagent_id}:{create_call.tool_call_id}"),
            )
            assert notification_result == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "succeeded",
                "content": child_final,
            }
            assert "files" not in notification_result
            notification_dump = json.dumps(notification_payload, ensure_ascii=False)
            assert all(value not in notification_dump for value in (other_owner_token, outside_generated_token, str(other_owner_path), str(outside_generated_path)))

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
        assert create_outputs == [create_payload]
        create_dump = json.dumps(create_outputs[0], ensure_ascii=False)
        assert all(value not in create_dump for value in (other_owner_token, outside_generated_token, str(other_owner_path), str(outside_generated_path)))
        parent_dump = json.dumps(parent_requests[0], ensure_ascii=False)
        assert child_final in parent_dump
        assert all(value not in parent_dump for value in (other_owner_token, outside_generated_token, str(other_owner_path), str(outside_generated_path)))


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
                "status": "queued",
            }
            assert send_payload == {
                "subagent_id": child["subagent_id"],
                "work_id": child["work_id"],
                "status": "queued",
            }
            original_message = await db.get(Message, origin.result_message_id)
            new_message = await db.get(Message, new_work.result_message_id)
            assert original_message is not None and original_message.content == original_final
            assert new_message is not None and new_message.content == new_final

        provider_count = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count
