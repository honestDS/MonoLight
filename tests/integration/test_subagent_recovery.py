import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, END_SESSION_TOOL_NAME, FILE_TOOL_NAME, SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.core.utils.llm_request_params import build_session_cache_key
from app.models.background_task import BackgroundTask, BackgroundTaskReplyMode, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.subagent import SubagentBoardPost, SubagentToolCall, SubagentWork


async def _child_for_parent(db, parent_work_id: int) -> SessionReplyWorkItem:
    return (
        (
            await db.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                    SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                    SessionReplyWorkItem.source_id == str(parent_work_id),
                )
            )
        )
        .scalars()
        .one()
    )


def _result_payload(row: Message) -> tuple[InternalMessage, dict]:
    message = InternalMessage.model_validate_json(row.content or "{}")
    return message, json.loads(message.content or "{}")


async def _queue_fixed_context_background_resume(runtime, monkeypatch: pytest.MonkeyPatch, fixed_context_marker: str) -> int:
    import app.core.background_tasks.reply_trigger as reply_trigger_module

    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
        profile.configs = configs
        task = BackgroundTask(
            uid=runtime.uid,
            session_id=runtime.session_id,
            profile_id=runtime.profile.id,
            tool_call_id="background-resume-terminal-watch-call",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            status=BackgroundTaskStatus.SUCCEEDED,
            arguments={"terminal_session_id": "background-resume-terminal", "after_offset": 0, "condition": "process_exit"},
            result={"status": "succeeded", "tool_name": TERMINAL_WATCH_TOOL_NAME, "content": {"event": "process_exit", "eof": True}},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            extra={
                "auto_terminal_watch": True,
                "reply_mode": BackgroundTaskReplyMode.RESUME_AGENT.value,
                "terminal_session_id": "background-resume-terminal",
                "frozen_context_messages": [InternalMessage(role=MessageRole.USER, content=fixed_context_marker).model_dump(mode="json", exclude_none=True)],
            },
        )
        db.add(task)
        await db.commit()
        assert task.id is not None
        task_id = task.id

    monkeypatch.setattr(reply_trigger_module, "AsyncSessionLocal", runtime.factory)
    await reply_trigger_module.trigger_background_task_reply(task_id)
    return task_id


@pytest.mark.parametrize("saved_result", [False, True], ids=["before-save", "after-save"])
@pytest.mark.asyncio
async def test_interrupted_subagent_round_recovers_saved_progress_without_replaying_file_write(image_chat_runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, saved_result: bool) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        target = (tmp_path / "subagent-recovery.txt").resolve()
        target.write_text("before\n", encoding="utf-8")
        written_content = "written-by-interrupted-child\n"
        sentinel_content = "sentinel-after-interruption\n"
        child_instruction = f"Use file_tool to write exactly {written_content!r} to {target}, then report completion."
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="recovery-create-call", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "create", "name": "文件写入恢复", "message": child_instruction})],
            )
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={"session_id": runtime.session_id, "request_id": "subagent-recovery-create", "message": "Create the independent child task."},
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]
        parent_submitted = await runtime.run_next()
        assert parent_submitted is not None and parent_submitted.id == parent_work_id
        assert parent_submitted.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            child = await _child_for_parent(db, parent_work_id)
            assert child.id is not None and child.subagent_id is not None
            child_work_id, subagent_id = child.id, child.subagent_id
            parent_key = build_session_cache_key(uid=runtime.uid, session_id=runtime.session_id)
            child_key = build_session_cache_key(uid=runtime.uid, session_id=runtime.session_id, subagent_id=subagent_id)
            assert parent_key != child_key
            parent_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()

        child_provider_start = len(runtime.provider_payloads)
        write_call_id = "recovery-child-write-call"
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id=write_call_id, name=FILE_TOOL_NAME, arguments={"operation": "write", "path": str(target), "content": written_content})],
            )
        )
        original_save_tool_response = interactive_tools_module.save_tool_response
        interrupted = False
        write_result_call_id: str | None = None

        async def interrupt_after_file_result(*args, **kwargs):
            nonlocal interrupted, write_result_call_id
            db = args[0] if args else kwargs["db"]
            tool_result = kwargs.get("tool_res") if "tool_res" in kwargs else args[4]
            try:
                payload = json.loads(tool_result.content or "{}")
            except (AttributeError, TypeError, ValueError):
                payload = None
            claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
            claimed_work_id = claim[0] if isinstance(claim, tuple) and len(claim) == 2 else None
            target_write = not interrupted and claimed_work_id == child_work_id and isinstance(payload, dict) and payload.get("operation") == "write" and payload.get("path") == str(target)
            if target_write:
                write_result_call_id = tool_result.tool_call_id
                if saved_result:
                    await original_save_tool_response(*args, **kwargs)
                interrupted = True
                raise asyncio.CancelledError("interrupt subagent after file write")
            return await original_save_tool_response(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(interactive_tools_module, "save_tool_response", interrupt_after_file_result)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next()
        assert interrupted and target.read_text(encoding="utf-8") == written_content
        target.write_text(sentinel_content, encoding="utf-8")
        assert isinstance(write_result_call_id, str) and write_result_call_id
        interrupted_child_provider_payloads = runtime.provider_payloads[child_provider_start:]
        assert interrupted_child_provider_payloads
        assert all(payload["prompt_cache_key"] == child_key for payload in interrupted_child_provider_payloads)

        async with runtime.factory() as db:
            child = await db.get(SessionReplyWorkItem, child_work_id)
            assert child is not None and child.status == SessionReplyWorkStatus.RUNNING
            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, child_work_id)
            assert isinstance(checkpoint, dict)
            checkpoint_messages = [InternalMessage.model_validate(item) for item in checkpoint["messages"]]
            assert any(message.role == MessageRole.ASSISTANT and any(call.id == write_result_call_id for call in message.tool_calls or []) for message in checkpoint_messages)
            assert not any(message.tool_call_id == write_result_call_id for message in checkpoint_messages)
            child.lock_until = 0
            child.max_attempts = child.attempt_count
            await db.commit()

        await SessionReplyConsumer()._recover_expired()
        async with runtime.factory() as db:
            child = await db.get(SessionReplyWorkItem, child_work_id)
            old_round = await db.get(SubagentWork, child_work_id)
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert child is not None and child.status == SessionReplyWorkStatus.FAILED
            assert old_round is not None and old_round.result is not None and old_round.result["completed"] is True
            assert old_round.result["status"] == "execution_unknown"
            old_error = old_round.result["error"]
            board_posts = (await db.execute(select(SubagentBoardPost).where(SubagentBoardPost.child_work_id == child_work_id))).scalars().all()
            assert len(board_posts) == 1
            assert board_posts[0].stage == "execution_unknown"
            assert board_posts[0].content == old_error
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid))).scalars().all()
            ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
            assert len(ready_events) == 1
            result_call = await db.get(SubagentToolCall, parent_call.id)
            assert result_call is not None and result_call.delivered is True
            result_row = await db.get(Message, result_call.tool_result_message_id)
            assert result_row is not None
            result_message, result_payload = _result_payload(result_row)
            assert result_message.tool_call_id == result_call.tool_call_id
            assert result_payload == {
                "subagent_id": result_call.subagent_id,
                "work_id": child_work_id,
                "status": "execution_unknown",
                "reason": old_error,
            }
        old_parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="原父已收到未知结果"))
        old_parent_done = await runtime.run_next()
        assert old_parent_done is not None and old_parent_done.id == parent_work_id
        assert old_parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert old_error in json.dumps(runtime.provider_payloads[old_parent_provider_start:], ensure_ascii=False)
        query_latest = InternalToolCall(id="recovery-query-latest", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "query_result", "subagent_id": subagent_id})
        query_old = InternalToolCall(id="recovery-query-old", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "query_result", "subagent_id": subagent_id, "work_id": child_work_id})
        send_call = InternalToolCall(id="recovery-send-message", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "send_message", "subagent_id": subagent_id, "message": "继续上次任务并报告恢复结果。"})
        runtime.outputs.extend([InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query_latest, query_old]), InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call])])
        new_accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={"session_id": runtime.session_id, "request_id": "subagent-recovery-new-parent", "message": "查询旧子轮后继续同一个子代理。"},
        )
        assert new_accepted.status_code == 200
        new_parent_work_id = new_accepted.json()["data"]["work_id"]
        new_parent_replied = await runtime.run_next()
        assert new_parent_replied is not None and new_parent_replied.id == new_parent_work_id
        assert new_parent_replied.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            result_rows = (await db.execute(select(Message).where(Message.role == MessageRole.TOOL, Message.type == MessageType.TOOL_RESULT))).scalars().all()
            query_call_ids = set()
            for row in (await db.execute(select(Message).where(Message.role == MessageRole.ASSISTANT, Message.type == MessageType.TOOL_CALL))).scalars().all():
                query_call_ids.update(call.id for call in InternalMessage.model_validate_json(row.content or "{}").tool_calls or [] if call.name == AGENT_WORK_TOOL_NAME and call.arguments.get("operation") == "query_result" and call.arguments.get("subagent_id") == subagent_id)
            assert len(query_call_ids) == 2
            query_results = {}
            for row in result_rows:
                message, payload = _result_payload(row)
                if message.tool_call_id in query_call_ids:
                    assert message.tool_execution_status == "succeeded"
                    query_results[message.tool_call_id] = payload
            assert set(query_results) == query_call_ids
            for payload in query_results.values():
                assert payload == {
                    "subagent_id": subagent_id,
                    "work_id": child_work_id,
                    "status": "execution_unknown",
                    "reason": old_error,
                }
            new_child = await _child_for_parent(db, new_parent_work_id)
            assert new_child.id is not None and new_child.id != child_work_id and new_child.subagent_id == subagent_id
            new_child_work_id = new_child.id

        new_child_message = "继续上次任务并报告恢复结果。"
        new_summary = "新子轮已沿保存进度完成"
        provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_summary))
        new_child_done = await runtime.run_next()
        assert new_child_done is not None and new_child_done.id == new_child_work_id
        assert new_child_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert target.read_text(encoding="utf-8") == sentinel_content
        new_child_provider_payloads = runtime.provider_payloads[provider_start:]
        assert new_child_provider_payloads
        assert all(payload["prompt_cache_key"] == child_key for payload in new_child_provider_payloads)

        first_resume_request = json.dumps(runtime.provider_payloads[provider_start], ensure_ascii=False)
        assert write_result_call_id in first_resume_request and new_child_message in first_resume_request
        assert ("bytes_written" in first_resume_request) is saved_result
        assert ("execution_unknown" in first_resume_request) is (not saved_result)

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="新父任务已完成"))
        new_parent_done = await runtime.run_next()
        assert new_parent_done is not None and new_parent_done.id == new_parent_work_id
        assert new_parent_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            old_child = await db.get(SessionReplyWorkItem, child_work_id)
            new_child = await db.get(SessionReplyWorkItem, new_child_work_id)
            old_parent = await db.get(SessionReplyWorkItem, parent_work_id)
            new_parent = await db.get(SessionReplyWorkItem, new_parent_work_id)
            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.subagent_id == subagent_id))).scalars().all()
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == subagent_id).order_by(SubagentToolCall.id))).scalars().all()
            assert old_child is not None and old_child.status == SessionReplyWorkStatus.FAILED
            assert new_child is not None and new_child.status == SessionReplyWorkStatus.SUCCEEDED
            assert old_parent is not None and old_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert new_parent is not None and new_parent.status == SessionReplyWorkStatus.SUCCEEDED
            old_round = next(row for row in rounds if row.child_work_id == child_work_id)
            new_round = next(row for row in rounds if row.child_work_id == new_child_work_id)
            assert old_round.result["completed"] is True and old_round.result["status"] == "execution_unknown"
            assert new_round.result["completed"] is True and new_round.result["status"] == "succeeded"
            assert new_round.result["result"]["content"] == new_summary
            assert [(call.arguments["operation"], call.child_work_id, call.parent_work_id) for call in calls] == [("create", child_work_id, parent_work_id), ("send_message", new_child_work_id, new_parent_work_id)]
            for call in calls:
                assert call.delivered is True
                result_row = await db.get(Message, call.tool_result_message_id)
                assert result_row is not None
                result_message, result_payload = _result_payload(result_row)
                assert result_message.tool_call_id == call.tool_call_id
                if call.arguments["operation"] == "create":
                    assert result_payload == {
                        "subagent_id": call.subagent_id,
                        "work_id": call.child_work_id,
                        "status": "execution_unknown",
                        "reason": old_error,
                    }
                else:
                    assert result_payload == {
                        "subagent_id": call.subagent_id,
                        "work_id": call.child_work_id,
                        "status": "succeeded",
                        "content": new_summary,
                    }
        assert target.read_text(encoding="utf-8") == sentinel_content


@pytest.mark.asyncio
async def test_interrupted_mixed_child_and_image_wait_recovers_without_losing_image(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.crud.session.external_work import external_work_crud

    child_instruction = "Independently complete the child task and report its final summary."
    child_summary = "child-summary-after-mixed-recovery"
    image_prompt = "subagent-recovery-mixed-image-unique-prompt"

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        create_call = InternalToolCall(
            id="mixed-recovery-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "混合等待恢复", "message": child_instruction},
        )
        image_call = InternalToolCall(
            id="mixed-recovery-image-call",
            name="generate_image",
            arguments={"prompt": image_prompt},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call, image_call]))

        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "mixed-recovery-interrupted-request",
                "message": "Create the child task and generate the image.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        async def interrupt_suspend_claimed(*args, **kwargs):
            raise asyncio.CancelledError("interrupted before mixed external wait registration")

        with monkeypatch.context() as patch:
            patch.setattr(external_work_crud, "suspend_claimed", interrupt_suspend_claimed)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next()

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.locked_by is not None
            assert parent.execution_state.get("has_child_work") is True

            child = await _child_for_parent(db, parent_work_id)
            assert child.id is not None and child.subagent_id is not None
            assert child.status == SessionReplyWorkStatus.READY_FOR_LLM
            child_work_id = child.id

            child_rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child_work_id))).scalars().all()
            assert len(child_rounds) == 1
            assistant_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.ASSISTANT,
                            Message.type == MessageType.TOOL_CALL,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            create_matches = []
            image_matches = []
            for assistant_row in assistant_rows:
                assistant_message = InternalMessage.model_validate_json(assistant_row.content or "{}")
                for persisted_call in assistant_message.tool_calls or []:
                    if persisted_call.name == AGENT_WORK_TOOL_NAME and persisted_call.arguments == create_call.arguments:
                        create_matches.append(persisted_call)
                    if persisted_call.name == image_call.name and persisted_call.arguments == image_call.arguments:
                        image_matches.append(persisted_call)
            assert len(create_matches) == 1
            assert len(image_matches) == 1
            actual_create_call_id = create_matches[0].id
            actual_image_call_id = image_matches[0].id
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 1
            parent_call = calls[0]
            assert parent_call.arguments == create_call.arguments
            assert parent_call.tool_call_id == actual_create_call_id
            assert parent_call.delivered is False

            image_tasks = (
                (
                    await db.execute(
                        select(BackgroundTask).where(
                            BackgroundTask.session_id == runtime.session_id,
                            BackgroundTask.tool_name == "generate_image",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(image_tasks) == 1
            image_task = image_tasks[0]
            assert image_task.id is not None
            image_task_id = image_task.id
            assert image_task.tool_call_id == actual_image_call_id
            assert image_task.status == BackgroundTaskStatus.PENDING
            assert image_task.attempt_count == 0
            assert image_task.result is None

            result_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(result_rows) == 2
            queued_results = {}
            queued_result_rows = {}
            for row in result_rows:
                result_message, result_payload = _result_payload(row)
                queued_results[result_message.tool_call_id] = result_payload
                queued_result_rows[result_message.tool_call_id] = row
            assert set(queued_results) == {actual_create_call_id, actual_image_call_id}
            assert queued_results[actual_create_call_id] == {
                "subagent_id": child.subagent_id,
                "work_id": child_work_id,
                "status": "queued",
            }
            assert queued_results[actual_image_call_id]["status"] == "queued"
            assert queued_results[actual_image_call_id]["task_id"] == image_task_id
            queued_child_result_row = queued_result_rows[actual_create_call_id]
            assert queued_child_result_row.id is not None
            queued_child_result_id = queued_child_result_row.id

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            parent.lock_until = 0
            await db.commit()

        await SessionReplyConsumer()._recover_expired()
        assert not runtime.outputs
        provider_payload_count = len(runtime.provider_payloads)
        recovered_waiting = await runtime.run_next(independent_only=False)
        assert recovered_waiting is not None
        assert recovered_waiting.id == parent_work_id
        assert recovered_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == provider_payload_count

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert child is not None and child.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert len((await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child_work_id))).scalars().all()) == 1
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 1
            assert calls[0].tool_result_message_id == queued_child_result_id
            image_tasks = (
                (
                    await db.execute(
                        select(BackgroundTask).where(
                            BackgroundTask.session_id == runtime.session_id,
                            BackgroundTask.tool_name == "generate_image",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(image_tasks) == 1
            image_task = image_tasks[0]
            assert image_task.id == image_task_id
            assert image_task.status == BackgroundTaskStatus.PENDING
            assert image_task.attempt_count == 0

        await runtime.complete_images(task_ids=[image_task_id])

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            image_task = await db.get(BackgroundTask, image_task_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert image_task is not None
            assert image_task.status == BackgroundTaskStatus.SUCCEEDED
            assert image_task.attempt_count == 1
            assert image_task.result is not None
            image_file = image_task.result["content"]["send_file_to_user"]["files"][0]
            image_path = Path(image_file["path"])
            assert image_path.is_file() and image_path.read_bytes() == runtime.image_bytes
            image_display_name = image_file["display_name"]

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_summary))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None and child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="mixed-recovery-send-file-call",
                            name="send_file_to_user",
                            arguments={"files": [deepcopy(image_file)]},
                        )
                    ],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="parent-final-after-mixed-recovery"),
            ]
        )
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None and parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        parent_request_dump = json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)
        assert child_summary in parent_request_dump
        assert image_display_name in parent_request_dump

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert child is not None and child.status == SessionReplyWorkStatus.SUCCEEDED

            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 1
            parent_call = calls[0]
            assert parent_call.delivered is True
            assert parent_call.tool_result_message_id == queued_child_result_id
            result_row = await db.get(Message, parent_call.tool_result_message_id)
            assert result_row is not None
            result_message, result_payload = _result_payload(result_row)
            assert result_message.tool_call_id == parent_call.tool_call_id
            assert result_message.tool_execution_status == "succeeded"
            assert result_payload == {
                "subagent_id": parent_call.subagent_id,
                "work_id": child_work_id,
                "status": "succeeded",
                "content": child_summary,
            }

            image_tasks = (
                (
                    await db.execute(
                        select(BackgroundTask).where(
                            BackgroundTask.session_id == runtime.session_id,
                            BackgroundTask.tool_name == "generate_image",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(image_tasks) == 1
            image_task = image_tasks[0]
            assert image_task.id == image_task_id
            assert image_task.status == BackgroundTaskStatus.SUCCEEDED
            assert image_task.attempt_count == 1
            assert image_task.result is not None
            assert image_task.result["content"]["send_file_to_user"]["files"][0]["display_name"] == image_display_name

            response = parent.execution_state.get("response")
            assert isinstance(response, dict)
            response_files = response.get("files")
            assert isinstance(response_files, list) and response_files
            response_file = response_files[0]
            assert isinstance(response_file, dict)
            download_url = response_file.get("download_url")
            assert isinstance(download_url, str) and download_url
        download_response = await runtime.client.get(download_url)
        assert download_response.status_code == 200
        assert download_response.content == runtime.image_bytes


@pytest.mark.parametrize("operation", ["create", "send_message"], ids=["second-create", "second-send-message"])
@pytest.mark.parametrize("interruption_point", ["before-result-save", "after-result-save"])
@pytest.mark.asyncio
async def test_interrupted_second_subagent_round_after_external_resume_recovers_without_replaying(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    interruption_point: str,
) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module

    first_instruction = "Complete the first independent child round and return its unique first summary."
    first_summary = "second-round-recovery-first-summary"
    second_instruction = f"Complete the second independent child round for operation {operation} and return its unique second summary."
    second_summary = "second-round-recovery-second-summary"

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        first_create_call = InternalToolCall(
            id="second-round-first-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "独立初始任务", "message": first_instruction},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[first_create_call]))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "second-round-first-parent-request",
                "message": "Create the first child round.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        first_submitted = await runtime.run_next(independent_only=False)
        assert first_submitted is not None and first_submitted.id == parent_work_id
        assert first_submitted.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_summary))
        first_child_done = await runtime.run_next(independent_only=True)
        assert first_child_done is not None and first_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert parent.execution_state.get("external_resume_pending") is True
            first_child = await _child_for_parent(db, parent_work_id)
            assert first_child.id is not None and first_child.subagent_id is not None
            first_child_work_id = first_child.id
            first_subagent_id = first_child.subagent_id
            first_round = await db.get(SubagentWork, first_child_work_id)
            assert first_round is not None and first_round.result is not None
            assert first_round.result["result"]["content"] == first_summary
            first_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(first_calls) == 1
            first_call = first_calls[0]
            assert first_call.delivered is True
            assert first_call.tool_result_message_id is not None
            first_result_message_id = first_call.tool_result_message_id

        if operation == "create":
            second_arguments = {"operation": "create", "name": "独立后续任务", "message": second_instruction}
        else:
            second_arguments = {"operation": "send_message", "subagent_id": first_subagent_id, "message": second_instruction}
        second_model_call = InternalToolCall(
            id="second-round-second-parent-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments=second_arguments,
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[second_model_call]))

        interrupted = False
        saved_second_result_id: int | None = None
        second_provider_start = len(runtime.provider_payloads)
        original_save_tool_response = interactive_tools_module.save_tool_response

        async def interrupt_queued_child_result(*args, **kwargs):
            nonlocal interrupted, saved_second_result_id
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
            persisted_calls = []
            if claimed_work_id == parent_work_id:
                persisted_calls = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == claimed_work_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            matching_calls = [call for call in persisted_calls if call.arguments == second_arguments and call.delivered is False]
            target_result = (
                not interrupted
                and claimed_work_id == parent_work_id
                and isinstance(payload, dict)
                and set(payload) == {"subagent_id", "work_id", "status"}
                and payload.get("status") == "queued"
                and len(matching_calls) == 1
                and payload.get("work_id") == matching_calls[0].child_work_id
                and payload.get("subagent_id") == matching_calls[0].subagent_id
            )
            if target_result:
                if interruption_point == "after-result-save":
                    saved_result = await original_save_tool_response(*args, **kwargs)
                    saved_second_result_id = saved_result.id
                interrupted = True
                raise asyncio.CancelledError("interrupted around second queued child result save")
            return await original_save_tool_response(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(interactive_tools_module, "save_tool_response", interrupt_queued_child_result)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=False)
        assert interrupted
        assert len(runtime.provider_payloads) == second_provider_start + 1
        provider_payload_count = len(runtime.provider_payloads)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.execution_state.get("external_resume_pending") is True
            children = (
                (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK, SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK, SessionReplyWorkItem.source_id == str(parent_work_id)).order_by(SessionReplyWorkItem.id))).scalars().all()
            )
            assert len(children) == 2
            assert children[0].id == first_child_work_id
            second_child_work_id = children[1].id
            assert second_child_work_id is not None and second_child_work_id != first_child_work_id
            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([first_child_work_id, second_child_work_id])).order_by(SubagentWork.child_work_id))).scalars().all()
            assert len(rounds) == 2
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 2
            assert calls[0].child_work_id == first_child_work_id
            assert calls[0].delivered is True
            assert calls[0].tool_result_message_id == first_result_message_id
            assert calls[1].child_work_id == second_child_work_id
            assert calls[1].arguments == second_arguments
            assert calls[1].delivered is False
            assert calls[1].subagent_id == children[1].subagent_id
            if operation == "send_message":
                assert calls[1].subagent_id == first_subagent_id
            else:
                assert calls[1].subagent_id != first_subagent_id
            second_call_id = calls[1].tool_call_id
            assert isinstance(second_call_id, str) and second_call_id

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            parent.lock_until = 0
            await db.commit()

        await SessionReplyConsumer()._recover_expired()
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == provider_payload_count
        recovered_parent = await runtime.run_next(independent_only=False)
        assert recovered_parent is not None and recovered_parent.id == parent_work_id
        assert recovered_parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == provider_payload_count

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert len((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK, SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK, SessionReplyWorkItem.source_id == str(parent_work_id)))).scalars().all()) == 2
            assert len((await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([first_child_work_id, second_child_work_id])))).scalars().all()) == 2
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 2
            assert calls[0].delivered is True and calls[0].tool_result_message_id == first_result_message_id
            assert calls[1].delivered is False and calls[1].tool_call_id == second_call_id
            assert calls[1].tool_result_message_id is not None
            second_result_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == runtime.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            matching_second_results = []
            for row in second_result_rows:
                result_message, result_payload = _result_payload(row)
                if result_message.tool_call_id == second_call_id:
                    matching_second_results.append((row, result_message, result_payload))
            assert len(matching_second_results) == 1
            second_result_row, _, second_result_payload = matching_second_results[0]
            assert second_result_row.id == calls[1].tool_result_message_id
            assert second_result_payload == {
                "subagent_id": calls[1].subagent_id,
                "work_id": second_child_work_id,
                "status": "queued",
            }
            assert (saved_second_result_id is not None) is (interruption_point == "after-result-save")
            if saved_second_result_id is not None:
                assert second_result_row.id == saved_second_result_id

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_summary))
        second_child_done = await runtime.run_next(independent_only=True)
        assert second_child_done is not None and second_child_done.id == second_child_work_id
        assert second_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 2
            assert all(call.delivered is True for call in calls)

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="second-round-parent-final"))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None and parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads[parent_provider_start:]) == 1
        parent_request_dump = json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)
        assert first_summary in parent_request_dump
        assert second_summary in parent_request_dump

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.SUCCEEDED
            children = (
                (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK, SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK, SessionReplyWorkItem.source_id == str(parent_work_id)).order_by(SessionReplyWorkItem.id))).scalars().all()
            )
            assert len(children) == 2
            assert all(child.status == SessionReplyWorkStatus.SUCCEEDED for child in children)
            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([first_child_work_id, second_child_work_id])).order_by(SubagentWork.child_work_id))).scalars().all()
            assert len(rounds) == 2
            assert [round_row.result["result"]["content"] for round_row in rounds] == [first_summary, second_summary]
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 2
            assert [call.arguments["operation"] for call in calls] == ["create", operation]
            assert all(call.delivered is True for call in calls)
            for call, expected_summary in zip(calls, [first_summary, second_summary], strict=True):
                assert call.tool_result_message_id is not None
                result_rows = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == parent.session_id,
                                Message.uid == parent.uid,
                                Message.profile_id == parent.profile_id,
                                Message.role == MessageRole.TOOL,
                                Message.type == MessageType.TOOL_RESULT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                matching_results = []
                for row in result_rows:
                    result_message, result_payload = _result_payload(row)
                    if result_message.tool_call_id == call.tool_call_id:
                        matching_results.append((row, result_message, result_payload))
                assert len(matching_results) == 1
                result_row, result_message, result_payload = matching_results[0]
                assert result_row.id == call.tool_result_message_id
                assert result_message.tool_execution_status == "succeeded"
                assert result_payload == {
                    "subagent_id": call.subagent_id,
                    "work_id": call.child_work_id,
                    "status": "succeeded",
                    "content": expected_summary,
                }


@pytest.mark.parametrize("interruption_point", ["before-result-save", "after-result-save"], ids=["before-c-result-save", "after-c-result-save"])
@pytest.mark.asyncio
async def test_interrupted_parent_create_preserves_registered_child_delivery_without_replaying_children(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    interruption_point: str,
) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module

    first_instruction = "Complete child A independently and return its unique summary."
    second_instruction = "Complete child B independently and return its unique summary."
    third_instruction = "Complete child C independently and return its unique summary."
    first_summary = "multi-child-recovery-summary-A"
    second_summary = "multi-child-recovery-summary-B"
    third_summary = "multi-child-recovery-summary-C"

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        first_create_call = InternalToolCall(
            id="multi-child-recovery-create-A",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "并行资料整理", "message": first_instruction},
        )
        second_create_call = InternalToolCall(
            id="multi-child-recovery-create-B",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "并行方案分析", "message": second_instruction},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[first_create_call, second_create_call]))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "multi-child-recovery-initial-request",
                "message": "Create child A and child B, then continue with the parent reply.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        initial_done = await runtime.run_next(independent_only=False)
        assert initial_done is not None and initial_done.id == parent_work_id
        assert initial_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
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
            child_a, child_b = children
            assert child_a.id is not None and child_b.id is not None and child_a.id != child_b.id
            child_a_work_id, child_b_work_id = child_a.id, child_b.id
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 2
            assert {(call.arguments["name"], call.arguments["message"]) for call in calls} == {
                ("并行资料整理", first_instruction),
                ("并行方案分析", second_instruction),
            }
            call_by_child_id = {call.child_work_id: call for call in calls}
            assert set(call_by_child_id) == {child_a_work_id, child_b_work_id}
            assert all(call.delivered is False and call.tool_result_message_id is not None for call in calls)
            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([child_a_work_id, child_b_work_id])))).scalars().all()
            assert len(rounds) == 2
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)} == {child_a_work_id, child_b_work_id}

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_summary))
        child_a_done = await runtime.run_next(independent_only=True)
        assert child_a_done is not None and child_a_done.id == child_a_work_id
        assert child_a_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child_b = await db.get(SessionReplyWorkItem, child_b_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert child_b is not None and child_b.status == SessionReplyWorkStatus.READY_FOR_LLM
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list) and len(waits) == 1
            assert waits[0]["child_work_id"] == child_b_work_id
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 2
            assert {call.child_work_id for call in calls if call.delivered} == {child_a_work_id}
            assert {call.child_work_id for call in calls if not call.delivered} == {child_b_work_id}
            b_call = next(call for call in calls if call.child_work_id == child_b_work_id)
            assert b_call.tool_result_message_id is not None
            b_result_message_id = b_call.tool_result_message_id

        third_create_call = InternalToolCall(
            id="multi-child-recovery-create-C",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "补充资料核查", "message": third_instruction},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[third_create_call]))
        provider_count_before_c = len(runtime.provider_payloads)
        interrupted = False
        saved_c_result_id: int | None = None
        original_save_tool_response = interactive_tools_module.save_tool_response

        async def interrupt_c_queued_result(*args, **kwargs):
            nonlocal interrupted, saved_c_result_id
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
            persisted_calls = []
            if claimed_work_id == parent_work_id:
                persisted_calls = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == claimed_work_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            matching_calls = [call for call in persisted_calls if call.arguments == third_create_call.arguments and call.delivered is False]
            target_result = (
                not interrupted
                and claimed_work_id == parent_work_id
                and isinstance(payload, dict)
                and set(payload) == {"subagent_id", "work_id", "status"}
                and payload.get("status") == "queued"
                and len(matching_calls) == 1
                and payload.get("work_id") == matching_calls[0].child_work_id
                and payload.get("subagent_id") == matching_calls[0].subagent_id
            )
            if target_result:
                if interruption_point == "after-result-save":
                    saved_result = await original_save_tool_response(*args, **kwargs)
                    saved_c_result_id = saved_result.id
                interrupted = True
                raise asyncio.CancelledError("interrupted around child C queued result save")
            return await original_save_tool_response(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(interactive_tools_module, "save_tool_response", interrupt_c_queued_result)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=False)
        assert interrupted
        assert len(runtime.provider_payloads) == provider_count_before_c + 1

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.RUNNING
            children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                        .order_by(SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(children) == 3
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 3
            c_call = next(call for call in calls if call.arguments == third_create_call.arguments)
            assert c_call.child_work_id is not None and c_call.child_work_id not in {child_a_work_id, child_b_work_id}
            child_c_work_id = c_call.child_work_id
            assert {child.id for child in children} == {child_a_work_id, child_b_work_id, child_c_work_id}
            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([child_a_work_id, child_b_work_id, child_c_work_id])))).scalars().all()
            assert len(rounds) == 3
            child_by_id = {child.id: child for child in children}
            assert child_by_id[child_a_work_id].status == SessionReplyWorkStatus.SUCCEEDED
            assert child_by_id[child_b_work_id].status == SessionReplyWorkStatus.READY_FOR_LLM
            assert child_by_id[child_c_work_id].status == SessionReplyWorkStatus.READY_FOR_LLM
            assert {call.child_work_id for call in calls if call.delivered} == {child_a_work_id}
            assert {call.child_work_id for call in calls if not call.delivered} == {child_b_work_id, child_c_work_id}
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list) and len(waits) == 1
            assert waits[0]["child_work_id"] == child_b_work_id
            counts_after_interruption = (len(children), len(rounds), len(calls))

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            parent.lock_until = 0
            await db.commit()

        await SessionReplyConsumer()._recover_expired()
        assert not runtime.outputs
        provider_count_after_recovery = len(runtime.provider_payloads)
        recovered_done = await runtime.run_next(independent_only=False)
        assert recovered_done is not None and recovered_done.id == parent_work_id
        assert recovered_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == provider_count_after_recovery

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                        .order_by(SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([child_a_work_id, child_b_work_id, child_c_work_id])))).scalars().all()
            assert (len(children), len(rounds), len(calls)) == counts_after_interruption
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list) and {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)} == {child_b_work_id, child_c_work_id}
            b_wait = next(slot for slot in waits if isinstance(slot, dict) and slot.get("child_work_id") == child_b_work_id)
            assert b_wait["tool_result_message_id"] == b_result_message_id
            c_wait = next(slot for slot in waits if isinstance(slot, dict) and slot.get("child_work_id") == child_c_work_id)
            assert c_wait["tool_result_message_id"] is not None
            if interruption_point == "after-result-save":
                assert saved_c_result_id is not None
                assert c_wait["tool_result_message_id"] == saved_c_result_id
            assert {call.child_work_id for call in calls if call.delivered} == {child_a_work_id}
            assert {call.child_work_id for call in calls if not call.delivered} == {child_b_work_id, child_c_work_id}
            assert all(call.tool_result_message_id is not None for call in calls)
            b_call = next(call for call in calls if call.child_work_id == child_b_work_id)
            assert b_call.tool_result_message_id == b_result_message_id
            result_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(result_rows) == 3
            result_call_ids = {_result_payload(row)[0].tool_call_id for row in result_rows}
            assert result_call_ids == {call.tool_call_id for call in calls}

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_summary))
        child_b_done = await runtime.run_next(independent_only=True)
        assert child_b_done is not None and child_b_done.id == child_b_work_id
        assert child_b_done.status == SessionReplyWorkStatus.SUCCEEDED

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=third_summary))
        child_c_done = await runtime.run_next(independent_only=True)
        assert child_c_done is not None and child_c_done.id == child_c_work_id
        assert child_c_done.status == SessionReplyWorkStatus.SUCCEEDED

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="multi-child-recovery-parent-final"))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None and parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        parent_payloads = runtime.provider_payloads[parent_provider_start:]
        assert len(parent_payloads) == 1
        provider_items = parent_payloads[0]["input"]
        assert isinstance(provider_items, list) and provider_items
        assert provider_items[-1].get("role") != MessageRole.ASSISTANT.value
        parent_request_dump = json.dumps(parent_payloads[0], ensure_ascii=False)
        assert first_summary in parent_request_dump
        assert second_summary in parent_request_dump
        assert third_summary in parent_request_dump

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.SUCCEEDED
            children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                        .order_by(SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([child_a_work_id, child_b_work_id, child_c_work_id])))).scalars().all()
            assert (len(children), len(rounds), len(calls)) == counts_after_interruption
            assert all(child.status == SessionReplyWorkStatus.SUCCEEDED for child in children)
            rounds_by_child_id = {round_row.child_work_id: round_row for round_row in rounds}
            expected_summaries = {
                child_a_work_id: first_summary,
                child_b_work_id: second_summary,
                child_c_work_id: third_summary,
            }
            assert set(rounds_by_child_id) == set(expected_summaries)
            for child_work_id, expected_summary in expected_summaries.items():
                round_row = rounds_by_child_id[child_work_id]
                assert round_row.result is not None
                assert round_row.result["completed"] is True
                assert round_row.result["status"] == "succeeded"
                assert round_row.result["result"]["content"] == expected_summary

            assert len(calls) == 3 and all(call.delivered is True for call in calls)
            result_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(result_rows) == 3
            result_rows_by_call_id = {}
            for row in result_rows:
                result_message, result_payload = _result_payload(row)
                result_rows_by_call_id[result_message.tool_call_id] = (row, result_message, result_payload)
            assert set(result_rows_by_call_id) == {call.tool_call_id for call in calls}
            calls_by_child_id = {call.child_work_id: call for call in calls}
            for child_work_id, expected_summary in expected_summaries.items():
                call = calls_by_child_id[child_work_id]
                result_row, result_message, result_payload = result_rows_by_call_id[call.tool_call_id]
                assert call.tool_result_message_id == result_row.id
                assert result_message.tool_execution_status == "succeeded"
                assert result_payload == {
                    "subagent_id": call.subagent_id,
                    "work_id": child_work_id,
                    "status": "succeeded",
                    "content": expected_summary,
                }


@pytest.mark.asyncio
async def test_background_resume_fixed_context_reviews_first_child_before_resuspending(image_chat_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    first_summary = "background-resume-first-child-summary"
    second_summary = "background-resume-second-child-summary"
    parent_final = "background-resume-parent-final"
    fixed_context_marker = "background-resume-fixed-context"

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        task_id = await _queue_fixed_context_background_resume(runtime, monkeypatch, fixed_context_marker)

        async with runtime.factory() as db:
            queued_work = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.BACKGROUND_TASK,
                            SessionReplyWorkItem.source_id == str(task_id),
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert queued_work.id is not None
            parent_work_id = queued_work.id
            assert queued_work.status == SessionReplyWorkStatus.READY_FOR_LLM

        first_create_call = InternalToolCall(
            id="background-resume-first-create",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "background resume child one", "message": "Complete child one and report its result."},
        )
        second_create_call = InternalToolCall(
            id="background-resume-second-create",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "background resume child two", "message": "Complete child two and report its result."},
        )
        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[first_create_call, second_create_call]))
        initial_parent = await runtime.run_next(independent_only=False)
        assert initial_parent is not None and initial_parent.id == parent_work_id
        assert initial_parent.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME
        assert initial_parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads[parent_provider_start:]) == 1
        assert fixed_context_marker in json.dumps(runtime.provider_payloads[parent_provider_start], ensure_ascii=False)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert isinstance(parent.execution_state.get("frozen_context_message_ids"), list)
            children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                        .order_by(SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(children) == 2
            child_work_ids = [child.id for child in children]
            assert all(isinstance(child_work_id, int) for child_work_id in child_work_ids)
            assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_summary))
        first_child_done = await runtime.run_next(independent_only=True)
        assert first_child_done is not None and first_child_done.id == child_work_ids[0]
        assert first_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            second_child = await db.get(SessionReplyWorkItem, child_work_ids[1])
            second_round = await db.get(SubagentWork, child_work_ids[1])
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert second_child is not None and second_child.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert second_round is not None and second_round.result is None

        review_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="background-resume-review-end", name=END_SESSION_TOOL_NAME, arguments={"skip_reply": True})],
            )
        )
        reviewed_parent = await runtime.run_next(independent_only=False)
        assert reviewed_parent is not None and reviewed_parent.id == parent_work_id
        assert reviewed_parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads[review_provider_start:]) == 1
        review_request_dump = json.dumps(runtime.provider_payloads[review_provider_start], ensure_ascii=False)
        assert first_summary in review_request_dump
        review_platform_constraints = []
        for item in runtime.provider_payloads[review_provider_start]["input"]:
            if item.get("role") != MessageRole.USER.value or not isinstance(item.get("content"), str):
                continue
            try:
                user_content = json.loads(item["content"])
            except json.JSONDecodeError:
                continue
            if isinstance(user_content, dict):
                review_platform_constraints.append(user_content.get("platform_constraints"))
        assert any(isinstance(platform_constraints, str) and SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT in platform_constraints for platform_constraints in review_platform_constraints)

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_summary))
        second_child_done = await runtime.run_next(independent_only=True)
        assert second_child_done is not None and second_child_done.id == child_work_ids[1]
        assert second_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        final_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
        final_parent = await runtime.run_next(independent_only=False)
        assert final_parent is not None and final_parent.id == parent_work_id
        assert final_parent.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads[final_provider_start:]) == 1
        final_request_dump = json.dumps(runtime.provider_payloads[final_provider_start], ensure_ascii=False)
        assert first_summary in final_request_dump
        assert second_summary in final_request_dump


@pytest.mark.parametrize("case", ["none", "waiting", "ready"], ids=["none", "waiting", "ready"])
@pytest.mark.asyncio
async def test_send_message_takes_over_fixed_context_child_report(image_chat_runtime, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    first_summary = "fixed-context-takeover-first-summary"
    sibling_summary = "fixed-context-takeover-sibling-summary"
    new_summary = "fixed-context-takeover-new-summary"
    new_parent_final = "fixed-context-takeover-new-parent-final"
    old_parent_final = "fixed-context-takeover-old-parent-final"
    fixed_context_marker = "fixed-context-takeover-marker"
    followup_message = "Continue the same subagent task with the new instruction."

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        task_id = await _queue_fixed_context_background_resume(runtime, monkeypatch, fixed_context_marker)
        first_create_call = InternalToolCall(
            id="fixed-context-takeover-first-create",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "fixed context first child", "message": "Complete the first child and report its result."},
        )
        initial_calls = [first_create_call]
        if case != "none":
            initial_calls.append(
                InternalToolCall(
                    id="fixed-context-takeover-sibling-create",
                    name=AGENT_WORK_TOOL_NAME,
                    arguments={"operation": "create", "name": "fixed context sibling child", "message": "Complete the sibling child and report its result."},
                )
            )

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=initial_calls))
        initial_parent = await runtime.run_next(independent_only=False)
        assert initial_parent is not None and initial_parent.id is not None
        assert initial_parent.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME
        assert initial_parent.source_id == str(task_id)
        assert initial_parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        background_work_id = initial_parent.id
        assert fixed_context_marker in json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_summary))
        first_child = await runtime.run_next(independent_only=True)
        assert first_child is not None and first_child.id is not None and first_child.status == SessionReplyWorkStatus.SUCCEEDED
        first_child_work_id = first_child.id
        first_subagent_id = first_child.subagent_id
        assert isinstance(first_subagent_id, str) and first_subagent_id
        if case == "ready":
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=sibling_summary))
            sibling_done = await runtime.run_next(independent_only=True)
            assert sibling_done is not None and sibling_done.id != first_child_work_id and sibling_done.status == SessionReplyWorkStatus.SUCCEEDED

        send_call = InternalToolCall(
            id="fixed-context-takeover-send-message",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "send_message", "subagent_id": first_subagent_id, "message": followup_message},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call]))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={"session_id": runtime.session_id, "request_id": "fixed-context-takeover-followup-request", "message": "Follow up on the completed background child."},
        )
        assert accepted.status_code == 200
        new_parent_work_id = accepted.json()["data"]["work_id"]
        new_parent_waiting = await runtime.run_next(independent_only=False)
        assert new_parent_waiting is not None and new_parent_waiting.id == new_parent_work_id
        assert new_parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            old_parent = await db.get(SessionReplyWorkItem, background_work_id)
            assert old_parent is not None
            expected_status = {"none": SessionReplyWorkStatus.SUCCEEDED, "waiting": SessionReplyWorkStatus.WAITING_EXTERNAL_WORK, "ready": SessionReplyWorkStatus.READY_FOR_LLM}[case]
            assert old_parent.status == expected_status
            if case == "none":
                assert old_parent.result_message_id is None
                response = old_parent.execution_state.get("response")
                assert isinstance(response, dict) and response.get("skip_reply") is True
            old_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == background_work_id, SubagentToolCall.child_work_id == first_child_work_id))).scalars().one()
            assert old_call.arguments.get("operation") == "create" and old_call.child_work_id == first_child_work_id and old_call.delivered is True
            old_create_result_message_id = old_call.tool_result_message_id
            assert old_create_result_message_id is not None

        if case == "none":
            before_silent = len(runtime.provider_payloads)
            assert await runtime.run_next(independent_only=False) is None
            assert len(runtime.provider_payloads) == before_silent
        else:
            if case == "waiting":
                before_waiting = len(runtime.provider_payloads)
                assert await runtime.run_next(independent_only=False) is None
                assert len(runtime.provider_payloads) == before_waiting
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=sibling_summary))
                sibling_done = await runtime.run_next(independent_only=True)
                assert sibling_done is not None and sibling_done.id != first_child_work_id and sibling_done.status == SessionReplyWorkStatus.SUCCEEDED
            old_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=old_parent_final))
            old_done = await runtime.run_next(independent_only=False)
            assert old_done is not None and old_done.id == background_work_id and old_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[old_provider_start:]) == 1
            old_request = json.dumps(runtime.provider_payloads[old_provider_start:], ensure_ascii=False)
            assert fixed_context_marker in old_request and sibling_summary in old_request

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_summary))
        new_child = await runtime.run_next(independent_only=True)
        assert new_child is not None and new_child.id is not None and new_child.subagent_id == first_subagent_id
        assert new_child.status == SessionReplyWorkStatus.SUCCEEDED
        new_child_work_id = new_child.id
        assert followup_message in json.dumps(runtime.provider_payloads, ensure_ascii=False)

        new_parent_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=new_parent_final))
        new_parent_done = await runtime.run_next(independent_only=False)
        assert new_parent_done is not None and new_parent_done.id == new_parent_work_id and new_parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert new_summary in json.dumps(runtime.provider_payloads[new_parent_start:], ensure_ascii=False)

        async with runtime.factory() as db:
            old_parent = await db.get(SessionReplyWorkItem, background_work_id)
            new_parent = await db.get(SessionReplyWorkItem, new_parent_work_id)
            background_task = await db.get(BackgroundTask, task_id)
            assert old_parent is not None and old_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert new_parent is not None and new_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert background_task is not None and background_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
            reply_owner = background_task.extra.get("reply_owner") if isinstance(background_task.extra, dict) else None
            assert isinstance(reply_owner, dict) and reply_owner.get("status") == "handled"
            active = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.status.in_((SessionReplyWorkStatus.READY_FOR_LLM, SessionReplyWorkStatus.RUNNING, SessionReplyWorkStatus.WAITING_EXTERNAL_WORK))))).scalars().all()
            assert active == []

            old_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == background_work_id))).scalars().all()
            assert old_calls and all(call.arguments["operation"] == "create" and call.delivered is True and call.tool_result_message_id is not None for call in old_calls)
            old_result = await db.get(Message, old_create_result_message_id)
            assert old_result is not None
            old_result_message, old_payload = _result_payload(old_result)
            old_call = next(call for call in old_calls if call.tool_result_message_id == old_create_result_message_id)
            assert old_result_message.tool_call_id == old_call.tool_call_id
            assert old_payload == {
                "subagent_id": old_call.subagent_id,
                "work_id": old_call.child_work_id,
                "status": "succeeded",
                "content": first_summary,
            }

            new_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == new_parent_work_id, SubagentToolCall.child_work_id == new_child_work_id))).scalars().one()
            assert new_call.arguments == send_call.arguments and new_call.delivered is True and new_call.tool_result_message_id is not None
            new_result = await db.get(Message, new_call.tool_result_message_id)
            assert new_result is not None
            new_result_message, new_payload = _result_payload(new_result)
            assert new_result_message.tool_call_id == new_call.tool_call_id
            assert new_payload == {
                "subagent_id": new_call.subagent_id,
                "work_id": new_call.child_work_id,
                "status": "succeeded",
                "content": new_summary,
            }

            assistant_rows = (await db.execute(select(Message).where(Message.role == MessageRole.ASSISTANT))).scalars().all()
            assert sum(new_parent_final in (row.content or "") for row in assistant_rows) == 1
            if case != "none":
                assert sum(old_parent_final in (row.content or "") for row in assistant_rows) == 1
