import asyncio
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, END_SESSION_TOOL_NAME, FILE_TOOL_NAME
from app.core.session_reply_queue.child_delivery import resume_child_work_for_parent
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork


@pytest.mark.parametrize(
    ("completion_order", "image_count"),
    [
        ("image_first", 1),
        ("image_first", 2),
        ("child_first", 1),
        ("child_first", 2),
        ("image_child_image", 2),
    ],
    ids=["image_first_1", "image_first_2", "child_first_1", "child_first_2", "image_child_image_2"],
)
@pytest.mark.asyncio
async def test_agent_work_and_image_completion_resumes_parent_per_batch_and_keeps_both_results(
    image_chat_runtime,
    completion_order: str,
    image_count: int,
) -> None:
    child_instruction = "Independently finish the child task and return its final result marker."
    child_content = "child-result-marker"
    interim_parent_content = f"parent-interim-{completion_order}-{image_count}"
    image_prompts = [f"agent-work-image-regression-unique-prompt-{index + 1}" for index in range(image_count)]
    async with image_chat_runtime(goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()
        create_call = InternalToolCall(id="mixed-agent-work-create-call", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "create", "name": "图片并行任务", "message": child_instruction})
        image_calls = [
            InternalToolCall(
                id=f"mixed-agent-work-image-call-{index + 1}",
                name="generate_image",
                arguments={"prompt": prompt},
            )
            for index, prompt in enumerate(image_prompts)
        ]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call, *image_calls]))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "mixed-agent-work-image-request",
                "message": "Create the independent child and generate the requested images.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        waiting = await runtime.run_next()
        assert waiting is not None and waiting.id == parent_work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK, SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK, SessionReplyWorkItem.source_id == str(parent_work_id)))).scalars().one()
            assert parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert child.id is not None and child.status == SessionReplyWorkStatus.READY_FOR_LLM
            child_work_id = child.id
            round_row = await db.get(SubagentWork, child_work_id)
            assert round_row is not None and round_row.result is None
            image_tasks = (
                (
                    await db.execute(
                        select(BackgroundTask)
                        .where(
                            BackgroundTask.session_id == runtime.session_id,
                            BackgroundTask.tool_name == "generate_image",
                        )
                        .order_by(BackgroundTask.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(image_tasks) == image_count
            image_task_specs = {}
            for image_task in image_tasks:
                assert image_task.id is not None
                assert isinstance(image_task.tool_call_id, str) and image_task.tool_call_id
                assert image_task.arguments["prompt"] in image_prompts
                assert image_task.status == BackgroundTaskStatus.PENDING
                assert image_task.attempt_count == 0
                assert image_task.extra.get("origin_work_id") == parent_work_id
                image_task_specs[image_task.id] = {
                    "tool_call_id": image_task.tool_call_id,
                    "arguments": deepcopy(image_task.arguments),
                }
            image_task_ids = list(image_task_specs)
            assert {spec["arguments"]["prompt"] for spec in image_task_specs.values()} == set(image_prompts)
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            assert call.arguments == create_call.arguments
            assert call.child_work_id == child_work_id
            assert call.delivered is False
            assert call.tool_result_message_id is not None
            queued_child_result_id = call.tool_result_message_id
            create_call_id = call.tool_call_id
            assert isinstance(create_call_id, str) and create_call_id
            result_rows = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.role == MessageRole.TOOL, Message.type == MessageType.TOOL_RESULT).order_by(Message.id))).scalars().all()
            assert len(result_rows) == image_count + 1
            queued_by_call_id = {}
            for row in result_rows:
                result = InternalMessage.model_validate_json(row.content or "{}")
                assert result.tool_call_id is not None
                queued_by_call_id[result.tool_call_id] = json.loads(result.content or "{}")
            image_call_ids = {spec["tool_call_id"] for spec in image_task_specs.values()}
            assert set(queued_by_call_id) == {create_call_id, *image_call_ids}
            assert queued_by_call_id[create_call_id] == {
                "status": "queued",
                "work_id": child_work_id,
                "subagent_id": call.subagent_id,
            }
            for task_id, spec in image_task_specs.items():
                assert queued_by_call_id[spec["tool_call_id"]]["status"] == "queued"
                assert queued_by_call_id[spec["tool_call_id"]]["task_id"] == task_id
                assert queued_by_call_id[spec["tool_call_id"]]["tool_name"] == "generate_image"

        if completion_order == "image_first":
            completion_steps = [("image", task_id) for task_id in image_task_ids]
            completion_steps.append(("child", None))
        elif completion_order == "child_first":
            completion_steps = [("child", None), *[("image", task_id) for task_id in image_task_ids]]
        else:
            completion_steps = [("image", image_task_ids[0]), ("child", None), ("image", image_task_ids[1])]

        completed_task_ids = []
        image_results = {}
        image_files = {}
        child_completed = False
        for step_index, (result_type, result_id) in enumerate(completion_steps):
            if result_type == "child":
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_content))
                child_done = await runtime.run_next(independent_only=True)
                assert child_done is not None and child_done.id == child_work_id
                assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
                child_completed = True
            else:
                await runtime.complete_images(task_ids=[result_id])
                completed_task_ids.append(result_id)

            async with runtime.factory() as db:
                image_tasks = (
                    (
                        await db.execute(
                            select(BackgroundTask)
                            .where(
                                BackgroundTask.session_id == runtime.session_id,
                                BackgroundTask.tool_name == "generate_image",
                            )
                            .order_by(BackgroundTask.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            expected_parent_status = SessionReplyWorkStatus.READY_FOR_LLM
            assert parent is not None and parent.status == expected_parent_status
            assert call.delivered is child_completed
            pending_image_ids = set(image_task_ids) - set(completed_task_ids)
            pending_tool_ids = {slot.get("task_id") for slot in parent.execution_state.get("pending_tools", []) if isinstance(slot, dict)}
            assert pending_tool_ids == pending_image_ids
            child_wait_ids = {slot.get("child_work_id") for slot in parent.execution_state.get("child_work_waits", []) if isinstance(slot, dict)}
            assert child_wait_ids == ({child_work_id} if not child_completed else set())
            resume_task_ids = []
            for payload in parent.execution_state.get("external_resume_context", []):
                if isinstance(payload, dict):
                    resume_task_ids.append(payload.get("task_id"))
            expected_resume_task_ids = [result_id] if result_type == "image" else []
            assert resume_task_ids == expected_resume_task_ids

            if step_index < len(completion_steps) - 1:
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=f"{interim_parent_content}-step-{step_index}"))
                interim_done = await runtime.run_next(independent_only=False)
                assert interim_done is not None and interim_done.id == parent_work_id
                assert interim_done.status == SessionReplyWorkStatus.SUCCEEDED
                async with runtime.factory() as db:
                    interim_parent = await db.get(SessionReplyWorkItem, parent_work_id)
                assert interim_parent is not None and interim_parent.status == SessionReplyWorkStatus.SUCCEEDED
                pending_image_ids = set(image_task_ids) - set(completed_task_ids)
                pending_tool_ids = {slot.get("task_id") for slot in interim_parent.execution_state.get("pending_tools", []) if isinstance(slot, dict)}
                assert pending_tool_ids == pending_image_ids

            tasks_by_id = {}
            for image_task in image_tasks:
                assert image_task.id is not None
                tasks_by_id[image_task.id] = image_task
            for completed_id in completed_task_ids:
                image_task = tasks_by_id[completed_id]
                assert image_task.status == BackgroundTaskStatus.SUCCEEDED
                assert image_task.attempt_count == 1
                assert image_task.result is not None
                image_result = deepcopy(image_task.result)
                if completed_id in image_results:
                    assert image_result == image_results[completed_id]
                image_results[completed_id] = image_result
                image_file = image_result["content"]["send_file_to_user"]["files"][0]
                assert set(image_file) == {"path", "display_name", "description", "mime_type"}
                image_path = Path(image_file["path"])
                assert image_path.is_file() and image_path.read_bytes() == runtime.image_bytes
                image_files[completed_id] = deepcopy(image_file)
            for pending_id in set(image_task_ids) - set(completed_task_ids):
                assert tasks_by_id[pending_id].status == BackgroundTaskStatus.PENDING
                assert tasks_by_id[pending_id].attempt_count == 0
                assert tasks_by_id[pending_id].result is None

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            round_row = await db.get(SubagentWork, child_work_id)
            assert parent is not None and child is not None and round_row is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert child.status == SessionReplyWorkStatus.SUCCEEDED
            assert round_row.result is not None
            assert round_row.result["result"]["content"] == child_content
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            assert call.delivered is True
            assert call.tool_result_message_id == queued_child_result_id
            result_message = await db.get(Message, queued_child_result_id)
            assert result_message is not None
            child_result = InternalMessage.model_validate_json(result_message.content or "{}")
            assert child_result.tool_execution_status == "succeeded"
            assert json.loads(child_result.content or "{}") == {
                "status": "queued",
                "subagent_id": call.subagent_id,
                "work_id": child_work_id,
            }
        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="parent-final-marker"))
        parent_done = await runtime.run_next()
        assert parent_done is not None and parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        parent_request_dump = json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)
        assert child_content in parent_request_dump
        for image_file in image_files.values():
            assert image_file["display_name"] in parent_request_dump
        if completion_steps[-1][0] == "image":
            assert "[Subagent board review guidance]" not in parent_request_dump
        else:
            assert "[Subagent board review guidance]" in parent_request_dump
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.SUCCEEDED
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 1 and calls[0].delivered is True
            image_tasks = (
                (
                    await db.execute(
                        select(BackgroundTask)
                        .where(
                            BackgroundTask.session_id == runtime.session_id,
                            BackgroundTask.tool_name == "generate_image",
                        )
                        .order_by(BackgroundTask.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(image_tasks) == image_count
            for image_task in image_tasks:
                assert image_task.id in image_results
                assert image_task.status == BackgroundTaskStatus.SUCCEEDED
                assert image_task.attempt_count == 1
                assert image_task.result == image_results[image_task.id]
        assert Counter(request["prompt"] for request in runtime.image_requests) == Counter(image_prompts)


@pytest.mark.asyncio
async def test_agent_work_image_wakeup_queues_user_before_child_completion(image_chat_runtime) -> None:
    child_instruction = "Independently finish the child task and return its final result marker."
    child_content = "image-wakeup-child-result"
    user_message = "queued-user-message-after-image"
    user_reply = "queued-user-independent-reply"
    parent_reply = "parent-board-review-reply"
    parent_request_id = "image-wakeup-queue-parent-request"
    user_request_id = "image-wakeup-queue-user-request"

    async with image_chat_runtime(goal_mode=False) as runtime:

        async def _submit(request_id: str, message: str, expected_status: str) -> int:
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
            assert data["request_id"] == request_id
            assert data["submission_status"] == expected_status
            return data["work_id"]

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        create_call = InternalToolCall(
            id="image-wakeup-queue-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "图片唤醒后的子任务", "message": child_instruction},
        )
        image_call = InternalToolCall(
            id="image-wakeup-queue-image-call",
            name="generate_image",
            arguments={"prompt": "image-wakeup-queue-prompt"},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call, image_call]))
        parent_work_id = await _submit(parent_request_id, "Create the child task and generate the image.", "accepted")

        waiting = await runtime.run_next()
        assert waiting is not None and waiting.id == parent_work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        await runtime.complete_images()
        async with runtime.factory() as db:
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
            assert image_task.status == BackgroundTaskStatus.SUCCEEDED
            assert image_task.result is not None
            image_display_name = image_task.result["content"]["send_file_to_user"]["files"][0]["display_name"]

        user_work_id = await _submit(user_request_id, user_message, "queued")
        assert user_work_id != parent_work_id

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_content))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None
        assert child_done.source_id == str(parent_work_id)
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        user_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_reply))
        user_done = await runtime.run_next()
        assert user_done is not None and user_done.id == user_work_id
        assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
        user_status = await runtime.client.get(f"/api/v1/chat/reply-works/{user_work_id}")
        assert user_status.status_code == 200
        user_data = user_status.json()["data"]
        assert user_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert user_data["request_ids"] == [user_request_id]
        assert user_data["response"]["choices"][0]["message"]["content"] == user_reply
        user_provider_payloads = runtime.provider_payloads[user_provider_start:]
        assert image_display_name not in json.dumps(user_provider_payloads, ensure_ascii=False)

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_reply))
        parent_done = await runtime.run_next()
        assert parent_done is not None and parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        parent_provider_payloads = runtime.provider_payloads[parent_provider_start:]
        parent_request_dump = json.dumps(parent_provider_payloads, ensure_ascii=False)
        assert image_display_name in parent_request_dump
        assert child_content in parent_request_dump


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
@pytest.mark.parametrize(
    "late_parent_state",
    ["ready", "running", "succeeded"],
    ids=["parent-ready", "parent-running", "parent-succeeded"],
)
@pytest.mark.asyncio
async def test_agent_work_two_children_are_delivered_in_batches(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: str,
    late_parent_state: str,
) -> None:
    from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer

    first_instruction = "Independently complete child one and return its unique first result marker."
    second_instruction = "Independently complete child two and return its unique second result marker."
    child_instructions = [first_instruction, second_instruction]
    child_contents = ["two-child-first-result", "two-child-second-result"]
    first_parent_reply = f"two-child-parent-first-reply-{protocol}-{late_parent_state}"
    final_parent_reply = f"two-child-parent-final-reply-{protocol}-{late_parent_state}"
    private_file_path = (tmp_path / f"private-child-process-{protocol}-{late_parent_state}.txt").resolve()
    private_file_content = "private-child-file-tool-process\n"
    parent_recovery_task: asyncio.Task | None = None
    release_parent_request = asyncio.Event()

    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        try:
            async with runtime.factory() as db:
                profile = await db.get(Profile, runtime.profile.id)
                assert profile is not None
                configs = deepcopy(profile.configs)
                configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
                profile.configs = configs
                await db.commit()

            create_calls = [
                InternalToolCall(
                    id="two-child-create-call-one",
                    name=AGENT_WORK_TOOL_NAME,
                    arguments={"operation": "create", "name": "子任务一", "message": first_instruction},
                ),
                InternalToolCall(
                    id="two-child-create-call-two",
                    name=AGENT_WORK_TOOL_NAME,
                    arguments={"operation": "create", "name": "子任务二", "message": second_instruction},
                ),
            ]
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls))
            accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "two-child-batched-delivery-request",
                    "message": "Create both independent children and wait for their results.",
                },
            )
            assert accepted.status_code == 200
            parent_work_id = accepted.json()["data"]["work_id"]

            initial_done = await runtime.run_next(independent_only=False)
            assert initial_done is not None
            assert initial_done.id == parent_work_id
            assert initial_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
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
                first_child, second_child = children
                assert first_child.id is not None and second_child.id is not None
                assert first_child.id != second_child.id
                assert first_child.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert second_child.status == SessionReplyWorkStatus.READY_FOR_LLM
                first_child_work_id, second_child_work_id = first_child.id, second_child.id
                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                assert len(calls) == 2
                assert {call.arguments["message"] for call in calls} == set(child_instructions)
                calls_by_child_id = {call.child_work_id: call for call in calls}
                assert set(calls_by_child_id) == {first_child_work_id, second_child_work_id}
                assert all(call.delivered is False for call in calls)
                rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([first_child_work_id, second_child_work_id])).order_by(SubagentWork.child_work_id))).scalars().all()
                assert len(rounds) == 2
                rounds_by_child_id = {round_row.child_work_id: round_row for round_row in rounds}
                assert set(rounds_by_child_id) == {first_child_work_id, second_child_work_id}
                assert all(round_row.result is None for round_row in rounds)
                waits = parent.execution_state.get("child_work_waits")
                assert isinstance(waits, list)
                assert {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)} == {first_child_work_id, second_child_work_id}
                initial_parent_sequence = parent.sequence_no

            runtime.outputs.extend(
                [
                    InternalMessage(
                        role=MessageRole.ASSISTANT,
                        tool_calls=[
                            InternalToolCall(
                                id="two-child-private-file-call",
                                name=FILE_TOOL_NAME,
                                arguments={
                                    "operation": "write",
                                    "path": str(private_file_path),
                                    "content": private_file_content,
                                },
                            )
                        ],
                    ),
                    InternalMessage(role=MessageRole.ASSISTANT, content=child_contents[0]),
                ]
            )
            first_child_done = await runtime.run_next(independent_only=True)
            assert first_child_done is not None
            assert first_child_done.id == first_child_work_id
            assert first_child_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert private_file_path.is_file()
            assert private_file_path.read_text(encoding="utf-8") == private_file_content

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                second_child = await db.get(SessionReplyWorkItem, second_child_work_id)
                first_round = await db.get(SubagentWork, first_child_work_id)
                second_round = await db.get(SubagentWork, second_child_work_id)
                assert parent is not None and second_child is not None and first_round is not None and second_round is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert second_child.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert first_round.result is not None
                assert first_round.result["result"]["content"] == child_contents[0]
                assert second_round.result is None
                waits = parent.execution_state.get("child_work_waits")
                assert isinstance(waits, list)
                assert {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)} == {second_child_work_id}
                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                assert len(calls) == 2
                calls_by_child_id = {call.child_work_id: call for call in calls}
                first_call = calls_by_child_id[first_child_work_id]
                second_call = calls_by_child_id[second_child_work_id]
                assert first_call.delivered is True
                assert second_call.delivered is False
                assert second_call.tool_result_message_id is not None
                markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
                assert len(markers) == 1
                assert child_contents[0] in (markers[0].content or "")
                ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid).order_by(SessionEvent.id))).scalars().all()
                ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
                assert len(ready_events) == 1
                first_ready_event_snapshot = [(event.id, event.dedupe_key, deepcopy(event.event)) for event in ready_events]
                parent_sequence_after_first = parent.sequence_no
                assert parent_sequence_after_first != initial_parent_sequence

            previous_parent_result_id: int | None = None
            if late_parent_state == "ready":
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_contents[1]))
                second_child_done = await runtime.run_next(independent_only=True)
                assert second_child_done is not None
                assert second_child_done.id == second_child_work_id
                assert second_child_done.status == SessionReplyWorkStatus.SUCCEEDED

                async with runtime.factory() as db:
                    parent = await db.get(SessionReplyWorkItem, parent_work_id)
                    second_child = await db.get(SessionReplyWorkItem, second_child_work_id)
                    assert parent is not None and second_child is not None
                    assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                    assert second_child.status == SessionReplyWorkStatus.SUCCEEDED
                    assert parent.sequence_no == parent_sequence_after_first
                    waits = parent.execution_state.get("child_work_waits")
                    assert not waits
                    calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                    assert len(calls) == 2
                    assert all(call.delivered is True for call in calls)
                    ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid).order_by(SessionEvent.id))).scalars().all()
                    ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
                    assert [(event.id, event.dedupe_key, event.event) for event in ready_events] == first_ready_event_snapshot

                parent_provider_start = len(runtime.provider_payloads)
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=final_parent_reply))
                parent_done = await runtime.run_next(independent_only=False)
                assert parent_done is not None
                assert parent_done.id == parent_work_id
                assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
                parent_payloads = runtime.provider_payloads[parent_provider_start:]
                assert len(parent_payloads) == 1
                parent_request_dump = json.dumps(parent_payloads[0], ensure_ascii=False)
                assert all(child_content in parent_request_dump for child_content in child_contents)
            elif late_parent_state == "succeeded":
                first_parent_provider_start = len(runtime.provider_payloads)
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_parent_reply))
                first_parent_done = await runtime.run_next(independent_only=False)
                assert first_parent_done is not None
                assert first_parent_done.id == parent_work_id
                assert first_parent_done.status == SessionReplyWorkStatus.SUCCEEDED
                first_parent_payloads = runtime.provider_payloads[first_parent_provider_start:]
                assert len(first_parent_payloads) == 1
                first_parent_request_dump = json.dumps(first_parent_payloads[0], ensure_ascii=False)
                assert child_contents[0] in first_parent_request_dump
                assert child_contents[1] not in first_parent_request_dump

                async with runtime.factory() as db:
                    parent = await db.get(SessionReplyWorkItem, parent_work_id)
                    assert parent is not None
                    previous_parent_result_id = parent.result_message_id
                    assert previous_parent_result_id is not None
                    previous_parent_result = await db.get(Message, previous_parent_result_id)
                    assert previous_parent_result is not None
                    assert previous_parent_result.content == first_parent_reply

                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_contents[1]))
                second_child_done = await runtime.run_next(independent_only=True)
                assert second_child_done is not None
                assert second_child_done.id == second_child_work_id
                assert second_child_done.status == SessionReplyWorkStatus.SUCCEEDED

                async with runtime.factory() as db:
                    parent = await db.get(SessionReplyWorkItem, parent_work_id)
                    assert parent is not None
                    assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                    assert parent.sequence_no != parent_sequence_after_first
                    assert parent.result_message_id is None
                    previous_parent_result = await db.get(Message, previous_parent_result_id)
                    assert previous_parent_result is not None
                    assert previous_parent_result.content == first_parent_reply
                    calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                    assert len(calls) == 2
                    assert all(call.delivered is True for call in calls)
                    markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
                    assert len(markers) == 2
                    ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid).order_by(SessionEvent.id))).scalars().all()
                    ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
                    assert len(ready_events) == 2

                parent_provider_start = len(runtime.provider_payloads)
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=final_parent_reply))
                parent_done = await runtime.run_next(independent_only=False)
                assert parent_done is not None
                assert parent_done.id == parent_work_id
                assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
                parent_payloads = runtime.provider_payloads[parent_provider_start:]
                assert len(parent_payloads) == 1
                parent_request_dump = json.dumps(parent_payloads[0], ensure_ascii=False)
                assert all(child_content in parent_request_dump for child_content in child_contents)
            else:
                transformer = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
                fixture_post_json = transformer._post_json
                parent_request_started = asyncio.Event()

                async def hold_parent_post_json(_transformer, **kwargs):
                    response = await fixture_post_json(_transformer, **kwargs)
                    if not parent_request_started.is_set():
                        parent_request_started.set()
                        await release_parent_request.wait()
                    return response

                monkeypatch.setattr(transformer, "_post_json", hold_parent_post_json)
                runtime.outputs.append(
                    InternalMessage(
                        role=MessageRole.ASSISTANT,
                        tool_calls=[
                            InternalToolCall(
                                id="two-child-review-end-session",
                                name=END_SESSION_TOOL_NAME,
                                arguments={"skip_reply": True},
                            )
                        ],
                    )
                )
                parent_recovery_task = asyncio.create_task(runtime.run_next(independent_only=False))
                await asyncio.wait_for(parent_request_started.wait(), timeout=5)
                held_parent_payload = runtime.provider_payloads[-1]
                held_parent_request_dump = json.dumps(held_parent_payload, ensure_ascii=False)
                assert child_contents[0] in held_parent_request_dump
                assert child_contents[1] not in held_parent_request_dump

                async with runtime.factory() as db:
                    parent = await db.get(SessionReplyWorkItem, parent_work_id)
                    assert parent is not None
                    assert parent.status == SessionReplyWorkStatus.RUNNING
                    assert parent.locked_by is not None
                    assert parent.sequence_no == parent_sequence_after_first
                    waits = parent.execution_state.get("child_work_waits")
                    assert isinstance(waits, list)
                    assert {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)} == {second_child_work_id}
                    calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                    assert len(calls) == 2
                    calls_by_child_id = {call.child_work_id: call for call in calls}
                    first_call = calls_by_child_id[first_child_work_id]
                    second_call = calls_by_child_id[second_child_work_id]
                    assert first_call.delivered is True
                    assert second_call.delivered is False
                    second_round = await db.get(SubagentWork, second_child_work_id)
                    assert second_round is not None and second_round.result is None

                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_contents[1]))
                second_child_done = await runtime.run_next(independent_only=True)
                assert second_child_done is not None
                assert second_child_done.id == second_child_work_id
                assert second_child_done.status == SessionReplyWorkStatus.SUCCEEDED

                async with runtime.factory() as db:
                    parent = await db.get(SessionReplyWorkItem, parent_work_id)
                    second_round = await db.get(SubagentWork, second_child_work_id)
                    assert parent is not None and second_round is not None
                    assert parent.status == SessionReplyWorkStatus.RUNNING
                    assert second_round.result is not None
                    assert second_round.result["result"]["content"] == child_contents[1]
                    calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                    assert len(calls) == 2
                    calls_by_child_id = {call.child_work_id: call for call in calls}
                    assert calls_by_child_id[second_child_work_id].delivered is False
                    markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
                    assert len(markers) == 1
                    ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid).order_by(SessionEvent.id))).scalars().all()
                    ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
                    assert [(event.id, event.dedupe_key, event.event) for event in ready_events] == first_ready_event_snapshot

                release_parent_request.set()
                recovered_parent = await parent_recovery_task
                parent_recovery_task = None
                assert recovered_parent is not None
                assert recovered_parent.id == parent_work_id
                assert recovered_parent.status == SessionReplyWorkStatus.READY_FOR_LLM

                async with runtime.factory() as db:
                    parent = await db.get(SessionReplyWorkItem, parent_work_id)
                    assert parent is not None
                    assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                    assert parent.sequence_no != parent_sequence_after_first
                    assert parent.result_message_id is None
                    calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                    assert len(calls) == 2
                    assert all(call.delivered is True for call in calls)
                    markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
                    assert len(markers) == 2
                    ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid).order_by(SessionEvent.id))).scalars().all()
                    ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
                    assert len(ready_events) == 2
                    previous_parent_messages = (
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
                    assert previous_parent_messages == []

                parent_provider_start = len(runtime.provider_payloads)
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=final_parent_reply))
                parent_done = await runtime.run_next(independent_only=False)
                assert parent_done is not None
                assert parent_done.id == parent_work_id
                assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
                parent_payloads = runtime.provider_payloads[parent_provider_start:]
                assert len(parent_payloads) == 1
                parent_request_dump = json.dumps(parent_payloads[0], ensure_ascii=False)
                assert all(child_content in parent_request_dump for child_content in child_contents)

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.SUCCEEDED
                assert parent.result_message_id is not None
                final_message = await db.get(Message, parent.result_message_id)
                assert final_message is not None
                assert final_message.content == final_parent_reply
                if previous_parent_result_id is not None:
                    assert parent.result_message_id != previous_parent_result_id
                    previous_parent_result = await db.get(Message, previous_parent_result_id)
                    assert previous_parent_result is not None
                    assert previous_parent_result.content == first_parent_reply

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
                assert all(child.status == SessionReplyWorkStatus.SUCCEEDED for child in children)
                subagents = (
                    (
                        await db.execute(
                            select(Subagent).where(
                                Subagent.uid == parent.uid,
                                Subagent.session_id == parent.session_id,
                                Subagent.profile_id == parent.profile_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(subagents) == 2
                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                assert len(calls) == 2
                assert {call.arguments["message"] for call in calls} == set(child_instructions)
                assert all(call.delivered is True for call in calls)
                assert {subagent.id for subagent in subagents} == {call.subagent_id for call in calls}
                calls_by_child_id = {call.child_work_id: call for call in calls}
                expected_child_contents = {
                    first_child_work_id: child_contents[0],
                    second_child_work_id: child_contents[1],
                }
                assert set(calls_by_child_id) == set(expected_child_contents)
                rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([child.id for child in children if child.id is not None])).order_by(SubagentWork.child_work_id))).scalars().all()
                assert len(rounds) == 2
                rounds_by_child_id = {round_row.child_work_id: round_row for round_row in rounds}
                assert set(rounds_by_child_id) == set(expected_child_contents)
                for child_work_id, child_content in expected_child_contents.items():
                    round_row = rounds_by_child_id[child_work_id]
                    assert round_row.result is not None
                    assert round_row.result["result"]["content"] == child_content
                for child_work_id, child_content in expected_child_contents.items():
                    call = calls_by_child_id[child_work_id]
                    assert call.tool_result_message_id is not None
                    result_row = await db.get(Message, call.tool_result_message_id)
                    assert result_row is not None
                    result_message = InternalMessage.model_validate_json(result_row.content or "{}")
                    result_payload = json.loads(result_message.content or "{}")
                    assert result_message.tool_execution_status == "succeeded"
                    assert result_payload == {
                        "status": "queued",
                        "subagent_id": call.subagent_id,
                        "work_id": call.child_work_id,
                    }
                markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
                assert len(markers) == 2
                assert len({marker.dedupe_key for marker in markers}) == 2
                marker_contents = [marker.content or "" for marker in markers]
                assert all(sum(child_content in marker_content for marker_content in marker_contents) == 1 for child_content in child_contents)
                ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid).order_by(SessionEvent.id))).scalars().all()
                ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
                expected_ready_event_count = 1 if late_parent_state == "ready" else 2
                assert len(ready_events) == expected_ready_event_count
                marker_snapshot = [(marker.id, marker.dedupe_key, marker.content) for marker in markers]
                ready_event_snapshot = [(event.id, event.dedupe_key, deepcopy(event.event)) for event in ready_events]

            parent_payloads = []
            for payload in runtime.provider_payloads:
                payload_dump = json.dumps(payload, ensure_ascii=False)
                if any(child_content in payload_dump for child_content in child_contents):
                    parent_payloads.append(payload)
            expected_parent_request_count = 1 if late_parent_state == "ready" else 2
            assert len(parent_payloads) == expected_parent_request_count
            for payload in parent_payloads:
                provider_items = payload.get("messages")
                if provider_items is None:
                    provider_items = payload.get("input")
                assert isinstance(provider_items, list) and provider_items
                last_item = provider_items[-1]
                assert not (isinstance(last_item, dict) and last_item.get("role") == "assistant")
                provider_messages_dump = json.dumps(provider_items, ensure_ascii=False)
                assert FILE_TOOL_NAME not in provider_messages_dump
                assert str(private_file_path) not in provider_messages_dump
            final_parent_request_dump = json.dumps(parent_payloads[-1], ensure_ascii=False)
            assert all(child_content in final_parent_request_dump for child_content in child_contents)
            assert not runtime.outputs

            async with runtime.factory() as db:
                assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False
                assert await reconcile_child_work(db) == 0

            async with runtime.factory() as db:
                markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
                ready_events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id, SessionEvent.uid == runtime.uid).order_by(SessionEvent.id))).scalars().all()
                ready_events = [event for event in ready_events if event.event.get("type") == "work_ready" and event.event.get("work_id") == parent_work_id]
                assert [(marker.id, marker.dedupe_key, marker.content) for marker in markers] == marker_snapshot
                assert [(event.id, event.dedupe_key, event.event) for event in ready_events] == ready_event_snapshot
        finally:
            release_parent_request.set()
            if parent_recovery_task is not None:
                if not parent_recovery_task.done():
                    parent_recovery_task.cancel()
                await asyncio.gather(parent_recovery_task, return_exceptions=True)
