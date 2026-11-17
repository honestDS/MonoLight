import asyncio
import copy
import json
import os
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.dispatchers.background as background_dispatcher_module
import app.core.tools.send_file_to_user as send_file_to_user_module
from app.api.v1 import files as files_module
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.paths import get_user_temp_dir
from app.core.security import get_current_user
from app.core.tools.image_generation import ImageGenerationExecutor
from app.core.utils.background_task_result import build_background_task_success_result
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.llm_request_params import build_session_cache_key
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.image_generation import ImageGenerationClient
from app.transformers.openai.responses import OpenAIResponsesTransformer
from tests.integration.files import test_generated_image_reply_workflow as generated_image_reply_workflow
from tests.integration.files.test_generated_image_reply_workflow import (
    CHANNEL_ID,
    MODEL_ID,
    PROFILE_ID,
    UID,
    _assert_file_entries,
    _generated_image_owner,
    _image_fixture,
    _materialized_user_envelopes,
    _patch_runtime_database,
    _responses_function_item_maps,
    _seed_conversation,
)
from tests.unit.response_test_support import image_generation_payload

session_factory = generated_image_reply_workflow.session_factory


async def _complete_single_round_image_tasks(
    runtime: SimpleNamespace,
    pending_tasks: list[BackgroundTask],
    image_gates: dict[str, asyncio.Event],
) -> None:
    task_ids = sorted(task.id for task in pending_tasks if task.id is not None)
    assert len(task_ids) == len(pending_tasks) and len(set(task_ids)) == len(task_ids)
    prompts = {task.id: (task.arguments or {}).get("prompt") for task in pending_tasks if task.id is not None}
    assert all(isinstance(prompt, str) and prompt in image_gates for prompt in prompts.values())
    assert all(not gate.is_set() for gate in image_gates.values())
    work_ids = {task.extra.get("origin_work_id") for task in pending_tasks if isinstance(task.extra, dict)}
    assert len(work_ids) == 1 and isinstance(work_id := work_ids.pop(), int)
    assert ProfileConfig.model_validate(runtime.profile.configs).tool.background_task_max_concurrency == 2

    async def load_state() -> tuple[list[BackgroundTask], list[SessionReplyWorkItem], list[Message], list[SessionEvent]]:
        async with runtime.factory() as db:
            tasks = list((await db.execute(select(BackgroundTask).where(BackgroundTask.id.in_(task_ids)).order_by(BackgroundTask.id))).scalars().all())
            works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == runtime.session_id).order_by(SessionReplyWorkItem.id))).scalars().all())
            messages = list((await db.execute(select(Message).where(Message.session_id == runtime.session_id).order_by(Message.id))).scalars().all())
            events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id).order_by(SessionEvent.id))).scalars().all())
        return tasks, works, messages, events

    tasks, works, messages, _events = await load_state()
    assert len(tasks) == len(task_ids) and len(works) == 1
    work = works[0]
    assert work.id == work_id and work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
    assert work.source_type == SessionReplySourceType.USER_MESSAGE and work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert all(task.status == BackgroundTaskStatus.PENDING and task.tool_name == "generate_image" and task.auto_reply is False and task.extra.get("origin_work_id") == work_id for task in tasks)

    call_ids = {task.tool_call_id for task in tasks}
    immutable_messages: dict[int, tuple[MessageRole, MessageType, str | None]] = {}
    queued_ids: set[int] = set()
    for message in messages:
        if message.id is None:
            continue
        if message.role == MessageRole.USER:
            immutable_messages[message.id] = (message.role, message.type, message.content)
        elif message.role == MessageRole.TOOL and message.type == MessageType.TOOL_RESULT:
            stored = InternalMessage.model_validate_json(message.content or "{}")
            if stored.tool_call_id in call_ids:
                queued_payload = json.loads(stored.content or "{}")
                assert queued_payload["status"] == "queued" and queued_payload["task_id"] in task_ids
                queued_ids.add(message.id)
                immutable_messages[message.id] = (message.role, message.type, message.content)
    assert len(queued_ids) == len(task_ids)

    remaining = list(task_ids)
    while remaining:
        wave_size = min(2, len(remaining))
        request_count = len(runtime.image_requests)
        await runtime.manager.schedule(runtime.profile)
        started = [await asyncio.wait_for(runtime.image_started.get(), timeout=10) for _ in range(wave_size)]
        tasks, _works, _messages, _events = await load_state()
        running_ids = {task.id for task in tasks if task.status == BackgroundTaskStatus.RUNNING}
        pending_ids = {task.id for task in tasks if task.status == BackgroundTaskStatus.PENDING}
        assert len(running_ids) == wave_size
        assert len(running_ids) <= 2
        assert pending_ids == set(remaining) - running_ids
        assert Counter(started) == Counter(prompts[task_id] for task_id in running_ids)
        await runtime.manager.schedule(runtime.profile)
        assert len(runtime.image_requests) == request_count + wave_size and runtime.image_started.empty()
        running_handles = tuple(runtime.manager._running_by_profile.get(runtime.profile.id, set()))
        for prompt in started:
            image_gates[prompt].set()
        await asyncio.wait_for(asyncio.gather(*running_handles, return_exceptions=True), timeout=10)
        tasks, _works, _messages, _events = await load_state()
        assert all(task.status == BackgroundTaskStatus.SUCCEEDED and task.result is not None for task in tasks if task.id in running_ids)
        remaining = [task_id for task_id in remaining if task_id not in running_ids]

    tasks, works, messages, events = await load_state()
    task_by_id = {task.id: task for task in tasks}
    generated_paths: list[Path] = []
    result_snapshot: dict[int, dict[str, Any]] = {}
    for task_id in task_ids:
        task = task_by_id[task_id]
        assert task.status == BackgroundTaskStatus.SUCCEEDED and task.tool_name == "generate_image" and task.auto_reply is False and task.extra.get("origin_work_id") == work_id
        assert task.result is not None
        result_snapshot[task_id] = copy.deepcopy(task.result)
        files = task.result["content"]["send_file_to_user"]["files"]
        assert isinstance(files, list) and len(files) == 1
        path = Path(files[0]["path"]).resolve()
        assert path.is_file() and path.read_bytes() == runtime.image_bytes
        generated_paths.append(path)
    assert len(generated_paths) == len(set(generated_paths)) == len(task_ids)
    assert Counter(request["prompt"] for request in runtime.image_requests) == Counter(prompts.values())
    assert len(runtime.image_requests) == len(task_ids) and all(request["n"] == 1 for request in runtime.image_requests)

    file_arguments = [{"path": str(path)} for path in generated_paths]
    assert {Path(item["path"]).resolve() for item in file_arguments} == set(generated_paths)
    runtime.outputs.extend(
        [
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[InternalToolCall(id="send-all-images", name="send_file_to_user", arguments={"files": file_arguments})]),
            InternalMessage(role=MessageRole.ASSISTANT, content="批量图片已发送"),
        ]
    )
    resumed = await runtime.run_next()
    assert resumed is not None and resumed.id == work_id and resumed.status == SessionReplyWorkStatus.SUCCEEDED
    tasks, works, messages, events = await load_state()

    reply = await runtime.client.get(f"/api/v1/chat/reply-works/{work_id}")
    assert reply.status_code == 200
    reply_data = reply.json()["data"]
    response = reply_data["response"]
    response_files = response["files"]
    assert reply_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert isinstance(response_files, list) and len(response_files) == len(task_ids)
    assert len({file["id"] for file in response_files}) == len(response_files)
    response_content = json.loads(response["choices"][0]["message"]["content"])
    assert response_content == {"type": "assistant_files", "text": "批量图片已发送", "files": response_files}
    for file in response_files:
        download = await runtime.client.get(file["download_url"])
        assert download.status_code == 200 and download.content == runtime.image_bytes

    def assistant_file_items(history: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        items = []
        for item in history:
            content = item.get("content")
            if item.get("role") != MessageRole.ASSISTANT.value:
                continue
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    continue
            if isinstance(content, dict) and content.get("type") == "assistant_files":
                items.append((item, content))
        return items

    history_response = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": runtime.session_id})
    assert history_response.status_code == 200
    history = history_response.json()["data"]
    history_files = assistant_file_items(history)
    assert len(history_files) == 1 and history_files[0][0]["id"] == response["message_id"]
    assert history_files[0][1]["files"] == response_files
    repeated_history_response = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": runtime.session_id})
    assert repeated_history_response.status_code == 200
    repeated_history = repeated_history_response.json()["data"]
    assert assistant_file_items(repeated_history) == history_files

    proactive_events = [event for event in events if event.event.get("type") == "proactive_reply"]
    assert len(proactive_events) == 1
    assert proactive_events[0].event["message_id"] == response["message_id"] and proactive_events[0].event["files"] == response_files
    assert len(works) == 1 and works[0].id == work_id and works[0].status == SessionReplyWorkStatus.SUCCEEDED
    assert works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY
    assert {task.id: copy.deepcopy(task.result) for task in tasks} == result_snapshot
    current_messages = {message.id: (message.role, message.type, message.content) for message in messages if message.id is not None}
    assert all(current_messages.get(message_id) == snapshot for message_id, snapshot in immutable_messages.items())

    event_snapshot = [(event.id, event.dedupe_key, copy.deepcopy(event.event)) for event in events]
    request_count = len(runtime.image_requests)
    await runtime.complete_images()
    assert await runtime.run_next() is None
    assert len(runtime.image_requests) == request_count
    _tasks, _works, _messages, repeated_events = await load_state()
    assert [(event.id, event.dedupe_key, event.event) for event in repeated_events] == event_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_count",
    [
        pytest.param(1, id="requested-1"),
        pytest.param(3, id="requested-3"),
        pytest.param(5, id="requested-5"),
    ],
)
async def test_batch_image_generation_within_limit_is_queued_and_completed(
    image_chat_runtime,
    requested_count: int,
) -> None:
    prompts = [f"Batch image prompt {index + 1}" for index in range(requested_count)]
    gates = {prompt: asyncio.Event() for prompt in prompts}
    tool_calls = [
        InternalToolCall(
            id=f"batch-generate-image-{requested_count}-{index + 1}",
            name="generate_image",
            arguments={"prompt": prompt},
        )
        for index, prompt in enumerate(prompts)
    ]

    async with image_chat_runtime(goal_mode=False, image_gates=gates) as runtime:
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": f"batch-image-request-{requested_count}",
                "message": f"Queue {requested_count} background image tasks.",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        work_id = accepted_data["work_id"]
        assert isinstance(work_id, int)

        waiting = await runtime.run_next()
        assert waiting is not None and waiting.id == work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert runtime.image_requests == []

        async with runtime.factory() as db:
            task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id).order_by(BackgroundTask.id.asc()))
            pending_tasks = list(task_result.scalars().all())
            assistant_result = await db.execute(
                select(Message)
                .where(
                    Message.session_id == runtime.session_id,
                    Message.role == MessageRole.ASSISTANT,
                    Message.type == MessageType.TOOL_CALL,
                )
                .order_by(Message.id.asc())
            )
            assistant_tool_messages = list(assistant_result.scalars().all())
            message_result = await db.execute(
                select(Message)
                .where(
                    Message.session_id == runtime.session_id,
                    Message.role == MessageRole.TOOL,
                    Message.type == MessageType.TOOL_RESULT,
                )
                .order_by(Message.id.asc())
            )
            tool_messages = list(message_result.scalars().all())

        assert len(pending_tasks) == requested_count
        assert len(assistant_tool_messages) == 1
        persisted_assistant = InternalMessage.model_validate_json(assistant_tool_messages[0].content or "{}")
        persisted_calls = persisted_assistant.tool_calls or []
        assert len(persisted_calls) == requested_count
        assert len({tool_call.id for tool_call in persisted_calls}) == requested_count
        assert [(tool_call.name, tool_call.arguments) for tool_call in persisted_calls] == [(tool_call.name, tool_call.arguments) for tool_call in tool_calls]
        expected_calls = {tool_call.id: tool_call for tool_call in persisted_calls}
        task_by_call_id = {task.tool_call_id: task for task in pending_tasks}
        assert set(task_by_call_id) == set(expected_calls)
        for task in pending_tasks:
            assert task.status == BackgroundTaskStatus.PENDING
            assert task.tool_name == "generate_image"
            assert task.arguments == expected_calls[task.tool_call_id].arguments
            assert task.attempt_count == 0
            assert task.auto_reply is False
            assert task.reply_status == BackgroundTaskReplyStatus.NONE
            assert task.extra.get("origin_work_id") == work_id

        assert len(tool_messages) == requested_count
        queued_by_call_id: dict[str, dict[str, Any]] = {}
        for message in tool_messages:
            internal_message = InternalMessage.model_validate_json(message.content or "{}")
            assert internal_message.role == MessageRole.TOOL
            assert internal_message.tool_call_id is not None
            assert internal_message.tool_call_id not in queued_by_call_id
            assert internal_message.tool_call_id in task_by_call_id
            payload = json.loads(internal_message.content or "{}")
            assert payload["status"] == "queued"
            assert payload["tool_name"] == "generate_image"
            queued_by_call_id[internal_message.tool_call_id] = payload

        assert set(queued_by_call_id) == set(task_by_call_id)
        for call_id, payload in queued_by_call_id.items():
            assert payload["task_id"] == task_by_call_id[call_id].id

        await _complete_single_round_image_tasks(runtime, pending_tasks, gates)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_count",
    [
        pytest.param(5, id="requested-5"),
        pytest.param(7, id="requested-7"),
    ],
)
async def test_mixed_tools_share_round_image_submission_limit(
    image_chat_runtime,
    tmp_path: Path,
    requested_count: int,
) -> None:
    input_paths = [tmp_path / "mixed-input-1.txt", tmp_path / "mixed-input-2.txt"]
    input_contents = ["mixed-file-marker-1", "mixed-file-marker-2"]
    for path, content in zip(input_paths, input_contents):
        path.write_text(content, encoding="utf-8")
    rejected_write_path = tmp_path / "must-not-be-written.txt"
    rejected_write_content = "must-not-be-written-marker"
    image_prompts = [
        "Mixed round image prompt 1",
        "Mixed round image prompt 2",
        "Mixed round image prompt 3",
    ]
    rejected_image_prompt = "Mixed round rejected image prompt"
    tool_calls = [
        InternalToolCall(
            id="mixed-file-read-1",
            name="file_tool",
            arguments={"operation": "read", "path": str(input_paths[0])},
        ),
        InternalToolCall(
            id="mixed-generate-image-1",
            name="generate_image",
            arguments={"prompt": image_prompts[0]},
        ),
        InternalToolCall(
            id="mixed-file-read-2",
            name="file_tool",
            arguments={"operation": "read", "path": str(input_paths[1])},
        ),
        InternalToolCall(
            id="mixed-generate-image-2",
            name="generate_image",
            arguments={"prompt": image_prompts[1]},
        ),
        InternalToolCall(
            id="mixed-generate-image-3",
            name="generate_image",
            arguments={"prompt": image_prompts[2]},
        ),
    ]
    if requested_count == 7:
        tool_calls.extend(
            [
                InternalToolCall(
                    id="mixed-generate-image-rejected",
                    name="generate_image",
                    arguments={"prompt": rejected_image_prompt},
                ),
                InternalToolCall(
                    id="mixed-file-write-rejected",
                    name="file_tool",
                    arguments={
                        "operation": "write",
                        "path": str(rejected_write_path),
                        "content": rejected_write_content,
                    },
                ),
            ]
        )

    gates = {prompt: asyncio.Event() for prompt in image_prompts + ([rejected_image_prompt] if requested_count == 7 else [])}
    async with image_chat_runtime(goal_mode=False, image_gates=gates) as runtime:
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": f"mixed-image-request-{requested_count}",
                "message": f"Submit {requested_count} mixed tools in one round.",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        work_id = accepted_data["work_id"]
        assert isinstance(work_id, int)

        waiting = await runtime.run_next()
        assert waiting is not None and waiting.id == work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert runtime.image_requests == []
        assert len(runtime.provider_payloads) == 1

        async with runtime.factory() as db:
            assistant_result = await db.execute(
                select(Message)
                .where(
                    Message.session_id == runtime.session_id,
                    Message.role == MessageRole.ASSISTANT,
                    Message.type == MessageType.TOOL_CALL,
                )
                .order_by(Message.id.asc())
            )
            assistant_messages = list(assistant_result.scalars().all())
            tool_result = await db.execute(
                select(Message)
                .where(
                    Message.session_id == runtime.session_id,
                    Message.role == MessageRole.TOOL,
                    Message.type == MessageType.TOOL_RESULT,
                )
                .order_by(Message.id.asc())
            )
            tool_messages = list(tool_result.scalars().all())
            task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id).order_by(BackgroundTask.id.asc()))
            pending_tasks = list(task_result.scalars().all())

        assert len(assistant_messages) == 1
        persisted_assistant = InternalMessage.model_validate_json(assistant_messages[0].content or "{}")
        persisted_calls = persisted_assistant.tool_calls or []
        assert len(persisted_calls) == requested_count
        assert len({tool_call.id for tool_call in persisted_calls}) == requested_count
        assert [(tool_call.name, tool_call.arguments) for tool_call in persisted_calls] == [(tool_call.name, tool_call.arguments) for tool_call in tool_calls]
        assert all(persisted.id != original.id for persisted, original in zip(persisted_calls, tool_calls))

        call_ids = [tool_call.id for tool_call in persisted_calls]
        assert len(tool_messages) == requested_count
        result_by_call_id: dict[str, dict[str, Any]] = {}
        for message in tool_messages:
            internal_message = InternalMessage.model_validate_json(message.content or "{}")
            assert internal_message.role == MessageRole.TOOL
            assert internal_message.tool_call_id is not None
            assert internal_message.tool_call_id not in result_by_call_id
            result_by_call_id[internal_message.tool_call_id] = json.loads(internal_message.content or "{}")
        assert set(result_by_call_id) == set(call_ids)

        for index, path, marker in ((0, input_paths[0], input_contents[0]), (2, input_paths[1], input_contents[1])):
            result_payload = result_by_call_id[persisted_calls[index].id]
            assert result_payload["status"] == "success"
            assert result_payload["operation"] == "read"
            assert marker in result_payload["content"]
            assert path.read_text(encoding="utf-8") == marker

        image_indexes = (1, 3, 4)
        image_calls = [persisted_calls[index] for index in image_indexes]
        assert len(pending_tasks) == 3
        task_by_call_id = {task.tool_call_id: task for task in pending_tasks}
        assert set(task_by_call_id) == {tool_call.id for tool_call in image_calls}
        for tool_call in image_calls:
            task = task_by_call_id[tool_call.id]
            assert task.id is not None
            assert task.status == BackgroundTaskStatus.PENDING
            assert task.tool_name == "generate_image"
            assert task.arguments == tool_call.arguments
            assert task.attempt_count == 0
            assert task.auto_reply is False
            assert task.reply_status == BackgroundTaskReplyStatus.NONE
            assert task.extra.get("origin_work_id") == work_id
            queued_payload = result_by_call_id[tool_call.id]
            assert queued_payload["status"] == "queued"
            assert queued_payload["tool_name"] == "generate_image"
            assert queued_payload["task_id"] == task.id

        if requested_count == 7:
            rejected_calls = persisted_calls[5:]
            assert [(tool_call.name, tool_call.arguments) for tool_call in rejected_calls] == [
                ("generate_image", {"prompt": rejected_image_prompt}),
                (
                    "file_tool",
                    {"operation": "write", "path": str(rejected_write_path), "content": rejected_write_content},
                ),
            ]
            for tool_call in rejected_calls:
                rejected_payload = result_by_call_id[tool_call.id]
                assert rejected_payload["status"] == "failed"
                assert rejected_payload["error"] == "parallel_limit_exceeded"
                assert rejected_payload["requested"] == requested_count
                assert rejected_payload["limit"] == 5
                assert rejected_payload["executed"] is False
                assert "task_id" not in rejected_payload
            assert {tool_call.id for tool_call in rejected_calls}.isdisjoint(task_by_call_id)
            assert rejected_image_prompt not in {task.arguments.get("prompt") for task in pending_tasks}
            assert not rejected_write_path.exists()

        await _complete_single_round_image_tasks(runtime, pending_tasks, gates)

        requested_prompts = [request["prompt"] for request in runtime.image_requests]
        assert Counter(requested_prompts) == Counter(image_prompts)
        assert len(requested_prompts) == 3
        assert rejected_image_prompt not in requested_prompts
        assert all(path.read_text(encoding="utf-8") == content for path, content in zip(input_paths, input_contents))
        assert not rejected_write_path.exists()


@pytest.mark.asyncio
async def test_batch_image_generation_over_limit_rejects_excess_without_running(
    image_chat_runtime,
) -> None:
    prompts = [f"Over-limit background image prompt {index + 1}" for index in range(8)]
    gates = {prompt: asyncio.Event() for prompt in prompts}
    tool_calls = [
        InternalToolCall(
            id=f"batch-generate-image-over-limit-{index + 1}",
            name="generate_image",
            arguments={"prompt": prompt},
        )
        for index, prompt in enumerate(prompts)
    ]

    async with image_chat_runtime(goal_mode=False, image_gates=gates) as runtime:
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "batch-image-over-limit-request",
                "message": "Queue eight background image tasks over the parallel limit.",
            },
        )
        assert accepted.status_code == 200
        work_id = accepted.json()["data"]["work_id"]
        assert isinstance(work_id, int)

        waiting = await runtime.run_next()
        assert waiting is not None and waiting.id == work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert runtime.image_requests == []

        async with runtime.factory() as db:
            assistant_result = await db.execute(
                select(Message)
                .where(
                    Message.session_id == runtime.session_id,
                    Message.role == MessageRole.ASSISTANT,
                    Message.type == MessageType.TOOL_CALL,
                )
                .order_by(Message.id.asc())
            )
            assistant_messages = list(assistant_result.scalars().all())
            tool_result = await db.execute(
                select(Message)
                .where(
                    Message.session_id == runtime.session_id,
                    Message.role == MessageRole.TOOL,
                    Message.type == MessageType.TOOL_RESULT,
                )
                .order_by(Message.id.asc())
            )
            tool_messages = list(tool_result.scalars().all())
            task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id).order_by(BackgroundTask.id.asc()))
            pending_tasks = list(task_result.scalars().all())

        assert len(assistant_messages) == 1
        persisted_assistant = InternalMessage.model_validate_json(assistant_messages[0].content or "{}")
        persisted_calls = persisted_assistant.tool_calls or []
        assert len(persisted_calls) == len(tool_calls) == 8
        assert len({tool_call.id for tool_call in persisted_calls}) == 8
        assert [(tool_call.name, tool_call.arguments) for tool_call in persisted_calls] == [(tool_call.name, tool_call.arguments) for tool_call in tool_calls]
        call_ids = [tool_call.id for tool_call in persisted_calls]

        assert len(tool_messages) == 8
        result_by_call_id: dict[str, dict[str, Any]] = {}
        for message in tool_messages:
            internal_message = InternalMessage.model_validate_json(message.content or "{}")
            assert internal_message.role == MessageRole.TOOL
            assert internal_message.tool_call_id is not None
            assert internal_message.tool_call_id not in result_by_call_id
            result_by_call_id[internal_message.tool_call_id] = json.loads(internal_message.content or "{}")
        assert set(result_by_call_id) == set(call_ids)

        assert len(pending_tasks) == 5
        task_by_call_id = {task.tool_call_id: task for task in pending_tasks}
        assert set(task_by_call_id) == set(call_ids[:5])
        for tool_call in persisted_calls[:5]:
            task = task_by_call_id[tool_call.id]
            assert task.status == BackgroundTaskStatus.PENDING
            assert task.tool_name == "generate_image"
            assert task.attempt_count == 0
            assert task.auto_reply is False
            assert task.arguments == tool_call.arguments
            assert task.extra.get("origin_work_id") == work_id
            queued_payload = result_by_call_id[tool_call.id]
            assert queued_payload["status"] == "queued"
            assert queued_payload["tool_name"] == "generate_image"
            assert queued_payload["task_id"] == task.id

        assert set(call_ids[5:]).isdisjoint(task_by_call_id)
        for tool_call in persisted_calls[5:]:
            rejected_payload = result_by_call_id[tool_call.id]
            assert rejected_payload["status"] == "failed"
            assert rejected_payload["error"] == "parallel_limit_exceeded"
            assert rejected_payload["requested"] == 8
            assert rejected_payload["limit"] == 5
            assert rejected_payload["executed"] is False
            assert "task_id" not in rejected_payload

        await _complete_single_round_image_tasks(runtime, pending_tasks, gates)

        async with runtime.factory() as db:
            final_task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id).order_by(BackgroundTask.id.asc()))
            final_tasks = list(final_task_result.scalars().all())
        assert len(final_tasks) == 5
        assert {task.tool_call_id for task in final_tasks} == set(call_ids[:5])
        assert all(task.status == BackgroundTaskStatus.SUCCEEDED for task in final_tasks)

        requested_prompts = [request["prompt"] for request in runtime.image_requests]
        assert len(requested_prompts) == 5
        assert Counter(requested_prompts) == Counter(prompts[:5])
        assert set(prompts[5:]).isdisjoint(requested_prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_image_count", (1, 2))
async def test_completed_background_image_tasks_do_not_regenerate_images_in_final_reply(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_image_count: int,
) -> None:
    session_id = f"background-generated-images-{requested_image_count}"
    request_text = f"Generate exactly {requested_image_count} native image{'s' if requested_image_count != 1 else ''}."
    expected_bytes, expected_image_data = _image_fixture("png")
    _, red_image_data = _image_fixture("png", color=(224, 32, 32))
    image_generation_calls: list[dict[str, Any]] = []
    gateway_requests: list[dict[str, Any]] = []
    gateway_responses: list[dict[str, Any]] = []
    merged_files: list[dict[str, Any]] = []

    async def fake_generate_image(**kwargs: Any) -> dict[str, Any]:
        image_generation_calls.append(copy.deepcopy(kwargs))
        return image_generation_payload("background-image-model", b64_json=expected_image_data)

    async def fake_post(_transformer, **kwargs: Any) -> dict[str, Any]:
        payload = copy.deepcopy(kwargs["payload"])
        gateway_requests.append(payload)
        input_items = payload.get("input") if isinstance(payload.get("input"), list) else []
        has_send_file_call = any(isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "send_file_to_user" for item in input_items)
        if not has_send_file_call:
            output = [
                {
                    "type": "function_call",
                    "id": "fc-send-background-files",
                    "call_id": "call-send-background-files",
                    "name": "send_file_to_user",
                    "arguments": json.dumps({"files": merged_files}, ensure_ascii=False),
                    "status": "completed",
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "The completed background images are ready."}],
                }
            ]
            request_tools = payload.get("tools")
            if payload.get("tool_choice") != "none" and isinstance(request_tools, list) and any(isinstance(tool, dict) and tool.get("type") == "image_generation" for tool in request_tools):
                output.append(
                    {
                        "type": "image_generation_call",
                        "status": "completed",
                        "id": "native-red-extra-image",
                        "result": red_image_data,
                        "output_format": "png",
                    }
                )
        response = {
            "id": f"background-response-{len(gateway_responses) + 1}",
            "object": "response",
            "status": "completed",
            "model": MODEL_ID,
            "output": output,
            "usage": {"input_tokens": 19, "output_tokens": 11, "total_tokens": 30},
            "tools": [{"type": "image_generation"}],
        }
        gateway_responses.append(copy.deepcopy(response))
        return copy.deepcopy(response)

    monkeypatch.setattr(ImageGenerationClient, "generate_image", fake_generate_image)
    monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", fake_post)
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.setattr(background_dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.chdir(tmp_path)

    initial_message = await _seed_conversation(session_factory, session_id)
    task_ids: list[int] = []
    async with session_factory() as db:
        user_message = await db.get(Message, initial_message.id)
        profile = await db.get(Profile, PROFILE_ID)
        channel = await db.get(ModelChannel, CHANNEL_ID)
        assert user_message is not None
        assert profile is not None
        assert channel is not None
        user_message.content = request_text
        user_message.is_processed = True
        profile_configs = copy.deepcopy(profile.configs)
        profile_configs["channel"]["image_generation_channel"] = {
            "rules": [
                {
                    "channel_id": CHANNEL_ID,
                    "model_id": "background-image-model",
                    "priority": 1,
                    "weight": 1,
                }
            ]
        }
        profile_configs["tool"]["enabled_tools"] = ["generate_image", "send_file_to_user"]
        profile_configs["tool"]["allowed_operation_dirs"] = [str(tmp_path.resolve())]
        profile.configs = profile_configs
        channel.model_ids = [
            *channel.model_ids,
            {
                "model_id": "background-image-model",
                "usage": "IMAGE_GENERATION",
                "protocol": "OPENAI_IMAGE",
                "size": "1024x1024",
                "quality": "auto",
            },
        ]
        await db.commit()
        await db.refresh(profile)

        cfg = ProfileConfig.model_validate(profile.configs)
        for index in range(requested_image_count):
            tool_call_id = f"generate-image-{requested_image_count}-{index + 1}"
            arguments = {
                "prompt": f"Background image {index + 1}",
                "size": "1024x1024",
                "quality": "auto",
            }
            executor = ImageGenerationExecutor(project_root=os.getcwd(), uid=UID)
            executor.set_config(cfg)
            executor.set_runtime_context(
                db=db,
                profile=profile,
                session_id=session_id,
                source_message_id=initial_message.id,
            )
            raw_result = await executor.execute(**arguments)
            parsed_result = json.loads(raw_result)
            assert parsed_result["status"] == "success"
            successful_files = parsed_result["send_file_to_user"]["files"]
            assert isinstance(successful_files, list)
            merged_files.extend(copy.deepcopy(successful_files))

            assistant_tool_call = InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id=tool_call_id,
                        name="generate_image",
                        arguments=arguments,
                    )
                ],
            )
            await save_assistant_message(
                db,
                session_id,
                UID,
                PROFILE_ID,
                assistant_tool_call,
            )
            tool_response = InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call_id,
                content=raw_result,
            )
            await save_tool_response(
                db,
                session_id,
                UID,
                PROFILE_ID,
                tool_response,
                [],
                [],
            )
            task = BackgroundTask(
                uid=UID,
                session_id=session_id,
                profile_id=PROFILE_ID,
                tool_call_id=tool_call_id,
                tool_name="generate_image",
                status=BackgroundTaskStatus.SUCCEEDED,
                arguments=arguments,
                result=build_background_task_success_result("generate_image", parsed_result),
                attempt_count=1,
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.PENDING,
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            task_ids.append(task.id)
        await db.commit()

    assert len(merged_files) == requested_image_count

    dispatch_result = await ChatDispatcher.dispatch_proactive_reply(task_ids[-1])

    assert len(image_generation_calls) == requested_image_count
    assert all(call["n"] == 1 for call in image_generation_calls)
    assert all(call["model_id"] == "background-image-model" for call in image_generation_calls)
    assert len(gateway_requests) == 2
    expected_cache_key = build_session_cache_key(uid=UID, session_id=session_id)
    assert [request["prompt_cache_key"] for request in gateway_requests] == [expected_cache_key, expected_cache_key]
    assert gateway_requests[0]["tools"]
    assert gateway_requests[0]["tool_choice"] == "auto"
    assert gateway_requests[1]["tools"] == gateway_requests[0]["tools"]
    assert gateway_requests[1]["tool_choice"] == "auto"
    assert all(not any(isinstance(tool, dict) and tool.get("type") == "image_generation" for tool in request.get("tools", [])) for request in gateway_requests)
    assert not any(isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "send_file_to_user" for item in gateway_requests[0].get("input", []))
    assert any(isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "send_file_to_user" for item in gateway_requests[1].get("input", []))
    assert len(gateway_responses) == 2
    for response in gateway_responses:
        assert response["tools"] == [{"type": "image_generation"}]
        assert [item for item in response["output"] if item.get("type") == "image_generation_call"] == []

    returned_files = dispatch_result["files"]
    assert isinstance(returned_files, list)
    assert len(returned_files) == requested_image_count

    async with session_factory() as db:
        message_result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        messages = list(message_result.scalars().all())
        task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == session_id).order_by(BackgroundTask.id.asc()))
        tasks = list(task_result.scalars().all())

    assert len(tasks) == requested_image_count
    assert all(task.status == BackgroundTaskStatus.SUCCEEDED for task in tasks)
    assert all(task.attempt_count == 1 for task in tasks)
    assert all(task.tool_name == "generate_image" for task in tasks)
    assert all(task.uid == UID and task.profile_id == PROFILE_ID and task.tool_call_id for task in tasks)

    send_file_results: list[dict[str, Any]] = []
    assistant_files_payloads: list[dict[str, Any]] = []
    for message in messages:
        try:
            content = InternalMessage.model_validate_json(message.content or "{}").content if message.type == MessageType.TOOL_RESULT else message.content
            payload = json.loads(content or "{}")
        except (TypeError, ValueError):
            continue
        if message.role == MessageRole.TOOL and payload.get("type") == "files_to_user":
            send_file_results.append(payload)
        if message.role == MessageRole.ASSISTANT and payload.get("type") == "assistant_files":
            assistant_files_payloads.append(payload)

    assert len(send_file_results) == 1
    sent_files = send_file_results[0]["files"]
    assert len(sent_files) == requested_image_count
    assert [file_entry["id"] for file_entry in returned_files] == [file_entry["id"] for file_entry in sent_files]
    assert len(assistant_files_payloads) == 1
    assert len(assistant_files_payloads[0]["files"]) == requested_image_count
    assert [file_entry["id"] for file_entry in assistant_files_payloads[0]["files"]] == [file_entry["id"] for file_entry in sent_files]

    expected_dir = get_user_temp_dir(os.getcwd(), UID) / "generated_images"
    for file_entry in sent_files:
        _assert_file_entries(
            [file_entry],
            expected_bytes=expected_bytes,
            expected_dir=expected_dir,
            expected_mime_type="image/png",
            expected_suffix="png",
        )

    temp_root = tmp_path / "temp"
    generated_files = [path for path in temp_root.rglob("*") if path.is_file() and not path.name.startswith(".")]
    assert len(generated_files) == requested_image_count
    assert all(path.read_bytes() == expected_bytes for path in generated_files)

    app = FastAPI()
    app.include_router(files_module.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = _generated_image_owner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for file_entry in sent_files:
            download_response = await client.get(file_entry["download_url"])
            assert download_response.status_code == 200
            assert download_response.content == expected_bytes
            assert download_response.headers["content-type"].split(";", 1)[0] == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("round_two_count", "completion_order"),
    [
        pytest.param(3, (0, 1, 2, 3, 4, 5, 6, 7), id="fifo"),
        pytest.param(3, (1, 2, 3, 4, 5, 6, 7, 0), id="later-round-first"),
        pytest.param(3, (1, 2, 3, 4, 5, 0, 7, 6), id="interleaved"),
        pytest.param(5, (1, 2, 3, 4, 5, 6, 7, 8, 9, 0), id="later-round-first-full-limit"),
    ],
)
async def test_overlapping_image_rounds_keep_limits_and_replies_isolated(
    image_chat_runtime,
    round_two_count: int,
    completion_order: tuple[int, ...],
) -> None:
    round_one_prompts = [f"round-one image {index}" for index in range(1, 6)]
    round_two_prompts = [f"round-two image {index}" for index in range(1, round_two_count + 1)]
    all_prompts = round_one_prompts + round_two_prompts
    round_one_text, round_two_text = "round-one batch request", "round-two batch request"
    gates = {prompt: asyncio.Event() for prompt in all_prompts}

    def calls(prefix: str, prompts: list[str]) -> list[InternalToolCall]:
        return [InternalToolCall(id=f"{prefix}-image-{index}", name="generate_image", arguments={"prompt": prompt}) for index, prompt in enumerate(prompts, start=1)]

    async with image_chat_runtime(goal_mode=False, image_gates=gates) as runtime:

        async def state() -> tuple[list[BackgroundTask], list[SessionReplyWorkItem], list[Message], list[SessionEvent]]:
            async with runtime.factory() as db:
                tasks = list((await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id).order_by(BackgroundTask.id))).scalars().all())
                works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == runtime.session_id).order_by(SessionReplyWorkItem.id))).scalars().all())
                messages = list((await db.execute(select(Message).where(Message.session_id == runtime.session_id).order_by(Message.id))).scalars().all())
                events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == runtime.session_id).order_by(SessionEvent.id))).scalars().all())
            return tasks, works, messages, events

        def assert_tool_binding(messages: list[Message], tasks: list[BackgroundTask]) -> None:
            assistant = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TOOL_CALL]
            persisted_calls = [call for message in assistant for call in (InternalMessage.model_validate_json(message.content or "{}").tool_calls or [])]
            by_call_id = {call.id: call for call in persisted_calls}
            assert len(persisted_calls) == len(tasks) and len(by_call_id) == len(tasks)
            for task in tasks:
                assert task.tool_call_id in by_call_id
                persisted_call = by_call_id[task.tool_call_id]
                assert persisted_call.name == task.tool_name == "generate_image"
                assert persisted_call.arguments == task.arguments
            queued: dict[int, dict[str, Any]] = {}
            for message in messages:
                if message.role != MessageRole.TOOL or message.type != MessageType.TOOL_RESULT:
                    continue
                stored = InternalMessage.model_validate_json(message.content or "{}")
                payload = json.loads(stored.content or "{}")
                if payload.get("tool_name") == "generate_image":
                    assert payload["status"] == "queued" and payload["task_id"] in {task.id for task in tasks}
                    queued[payload["task_id"]] = payload
            assert set(queued) == {task.id for task in tasks}

        async def submit(tool_calls: list[InternalToolCall], request_id: str, text: str) -> int:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls))
            accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={"session_id": runtime.session_id, "request_id": request_id, "message": text},
            )
            assert accepted.status_code == 200
            data = accepted.json()["data"]
            assert data["submission_status"] == "accepted" and isinstance(data["work_id"], int)
            work_id = data["work_id"]
            resumed = await runtime.run_next()
            assert resumed is not None and resumed.id == work_id and resumed.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            return work_id

        round_one_calls = calls("round-one", round_one_prompts)
        round_one_work_id = await submit(round_one_calls, "round-one", round_one_text)
        assert len(runtime.provider_payloads) == 1
        tasks, works, messages, _events = await state()
        assert len(tasks) == 5 and len(works) == 1
        round_one_work = works[0]
        assert round_one_work.id == round_one_work_id and round_one_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
        user_one = [message for message in messages if message.role == MessageRole.USER and message.content == round_one_text]
        assert len(user_one) == 1 and user_one[0].id is not None
        user_one_context = [{"id": user_one[0].id, "role": MessageRole.USER.value, "content": round_one_text}]
        assert set(round_one_work.input_message_ids or []) == {user_one[0].id}
        round_one_tasks = [task for task in tasks if task.extra.get("origin_work_id") == round_one_work_id]
        assert Counter(task.arguments["prompt"] for task in round_one_tasks) == Counter(round_one_prompts)
        assert all(task.status == BackgroundTaskStatus.PENDING and task.attempt_count == 0 and task.auto_reply is False for task in round_one_tasks)
        assert all(task.extra.get("request_context") == user_one_context for task in round_one_tasks)
        assert_tool_binding(messages, round_one_tasks)

        await runtime.manager.schedule(runtime.profile)
        initial_started_prompts = [await asyncio.wait_for(runtime.image_started.get(), timeout=10) for _ in range(2)]
        round_one_tasks_by_creation = sorted(round_one_tasks, key=lambda task: (task.created_at, task.id))
        assert set(initial_started_prompts) == {task.arguments["prompt"] for task in round_one_tasks_by_creation[:2]}
        started_ids = {task.id for task in round_one_tasks if task.arguments["prompt"] in initial_started_prompts}
        assert len(started_ids) == 2 and all(not gates[prompt].is_set() for prompt in initial_started_prompts)
        assert len(runtime.image_requests) == 2
        await runtime.manager.schedule(runtime.profile)
        assert len(runtime.image_requests) == 2 and runtime.image_started.empty()
        initial_tasks, _works, _messages, _events = await state()
        assert {task.id for task in initial_tasks if task.status == BackgroundTaskStatus.RUNNING} == started_ids
        assert {task.id for task in initial_tasks if task.status == BackgroundTaskStatus.PENDING} == {task.id for task in initial_tasks} - started_ids

        round_two_calls = calls("round-two", round_two_prompts)
        round_two_work_id = await submit(round_two_calls, "round-two", round_two_text)
        assert round_two_work_id != round_one_work_id and len(runtime.provider_payloads) == 2
        tasks, works, messages, _events = await state()
        assert len(tasks) == 5 + round_two_count and {work.id for work in works} == {round_one_work_id, round_two_work_id}
        work_by_id = {work.id: work for work in works}
        assert all(work_by_id[work_id].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK for work_id in work_by_id)
        user_two = [message for message in messages if message.role == MessageRole.USER and message.content == round_two_text]
        assert len(user_two) == 1 and user_two[0].id is not None
        user_two_context = [{"id": user_two[0].id, "role": MessageRole.USER.value, "content": round_two_text}]
        assert set(work_by_id[round_one_work_id].input_message_ids or []) == {user_one[0].id}
        assert set(work_by_id[round_two_work_id].input_message_ids or []) == {user_two[0].id}
        task_by_prompt = {task.arguments["prompt"]: task for task in tasks}
        task_by_id = {task.id: task for task in tasks}
        assert set(task_by_prompt) == set(all_prompts) and all(isinstance(task_id, int) for task_id in task_by_id)
        origin_by_id = {task.id: task.extra.get("origin_work_id") for task in tasks}
        context_by_id = {task.id: (user_one_context if origin_by_id[task.id] == round_one_work_id else user_two_context) for task in tasks}
        assert {origin_by_id[task.id] for task in tasks if task.arguments["prompt"] in round_one_prompts} == {round_one_work_id}
        assert {origin_by_id[task.id] for task in tasks if task.arguments["prompt"] in round_two_prompts} == {round_two_work_id}
        assert all(task.extra.get("request_context") == context_by_id[task.id] for task in tasks)
        assert all(task.extra.get("origin_work_identity") == work_by_id[origin_by_id[task.id]].dedupe_key for task in tasks)
        assert {task.id for task in tasks if task.status == BackgroundTaskStatus.RUNNING} == started_ids
        assert_tool_binding(messages, tasks)
        await runtime.manager.schedule(runtime.profile)
        assert len(runtime.image_requests) == 2 and runtime.image_started.empty()
        tasks_after_schedule, _works, _messages, _events = await state()
        assert {task.id for task in tasks_after_schedule if task.status == BackgroundTaskStatus.RUNNING} == started_ids
        assert {task.id for task in tasks_after_schedule if task.status == BackgroundTaskStatus.PENDING} == set(task_by_id) - started_ids

        async def assert_task_state(completed_ids: set[int], active_ids: set[int]) -> None:
            persisted, _works, _messages, _events = await state()
            by_id = {task.id: task for task in persisted}
            assert set(by_id) == set(task_by_id)
            assert {task.id for task in persisted if task.status == BackgroundTaskStatus.RUNNING} == active_ids - completed_ids
            assert {task.id for task in persisted if task.status == BackgroundTaskStatus.PENDING} == set(task_by_id) - active_ids
            assert {task.id for task in persisted if task.status == BackgroundTaskStatus.SUCCEEDED} == completed_ids
            assert len(active_ids - completed_ids) <= 2
            for task_id, task in by_id.items():
                assert task.extra.get("origin_work_id") == origin_by_id[task_id]
                assert task.extra.get("request_context") == context_by_id[task_id]
                assert task.attempt_count == (1 if task_id in active_ids else 0)

        await assert_task_state(set(), started_ids)
        ordered_tasks = sorted(tasks, key=lambda task: (task.created_at, task.id))
        assert len(completion_order) == len(ordered_tasks) and set(completion_order) == set(range(len(ordered_tasks)))
        completed_ids: set[int] = set()
        started_prompts = {task.arguments["prompt"] for task in tasks if task.id in started_ids}
        generated_paths: dict[int, Path] = {}
        result_snapshots: dict[int, dict[str, Any]] = {}
        for task_index in completion_order:
            task = ordered_tasks[task_index]
            assert task.id in started_ids
            prompt = task.arguments["prompt"]
            assert prompt in started_prompts and not gates[prompt].is_set()
            current_tasks, _works, _messages, _events = await state()
            assert next(row for row in current_tasks if row.id == task.id).status == BackgroundTaskStatus.RUNNING
            running = tuple(runtime.manager._running_by_profile.get(runtime.profile.id, set()))
            assert 0 < len(running) <= 2
            gates[prompt].set()
            done, _pending = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED, timeout=10)
            assert len(done) == 1
            await asyncio.gather(*done)
            current_tasks, _works, _messages, _events = await state()
            completed = next(row for row in current_tasks if row.id == task.id)
            assert completed.status == BackgroundTaskStatus.SUCCEEDED and completed.attempt_count == 1 and completed.result is not None
            assert completed.extra.get("origin_work_id") == origin_by_id[task.id] and completed.extra.get("request_context") == context_by_id[task.id]
            result_snapshots[task.id] = copy.deepcopy(completed.result)
            files = completed.result["content"]["send_file_to_user"]["files"]
            assert isinstance(files, list) and len(files) == 1
            path = Path(files[0]["path"]).resolve()
            assert path.is_file() and path.read_bytes() == runtime.image_bytes and path not in set(generated_paths.values())
            generated_paths[task.id] = path
            completed_ids.add(task.id)
            await assert_task_state(completed_ids, started_ids)
            await runtime.manager.schedule(runtime.profile)
            unstarted = [candidate for candidate in ordered_tasks if candidate.id not in started_ids]
            if unstarted:
                next_prompt = await asyncio.wait_for(runtime.image_started.get(), timeout=10)
                assert next_prompt in {candidate.arguments["prompt"] for candidate in unstarted}
                started_ids.add(task_by_prompt[next_prompt].id)
                started_prompts.add(next_prompt)
            else:
                assert runtime.image_started.empty()
            await assert_task_state(completed_ids, started_ids)

        assert completed_ids == set(task_by_id) and len(generated_paths) == len(all_prompts)
        assert len(runtime.image_requests) == len(all_prompts)
        assert Counter(request["prompt"] for request in runtime.image_requests) == Counter(all_prompts)
        assert all(request["n"] == 1 for request in runtime.image_requests)
        assert all(gates[prompt].is_set() for prompt in all_prompts)
        assert not runtime.manager._running_by_profile.get(runtime.profile.id)
        tasks, works, messages, events = await state()
        assert {task.id: copy.deepcopy(task.result) for task in tasks} == result_snapshots
        assert all(task.status == BackgroundTaskStatus.SUCCEEDED and task.attempt_count == 1 for task in tasks)

        files_for_user = [{"path": str(generated_paths[task_by_prompt[prompt].id])} for prompt in all_prompts]
        provider_start = len(runtime.provider_payloads)
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[InternalToolCall(id="send-all-images", name="send_file_to_user", arguments={"files": files_for_user})]),
                InternalMessage(role=MessageRole.ASSISTANT, content="所有图片已发送"),
            ]
        )
        resumed = await runtime.run_next()
        assert resumed is not None and resumed.status == SessionReplyWorkStatus.SUCCEEDED and resumed.id in {round_one_work_id, round_two_work_id}
        resume_payloads = runtime.provider_payloads[provider_start:]
        assert resume_payloads
        assert round_one_text in json.dumps(resume_payloads, ensure_ascii=False) and round_two_text in json.dumps(resume_payloads, ensure_ascii=False)
        tasks, works, messages, events = await state()
        task_by_id = {task.id: task for task in tasks}

        for payload in resume_payloads:
            external_tasks: list[dict[str, Any]] = []
            image_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for envelope in _materialized_user_envelopes(payload):
                raw = envelope.get("user_message")
                if not isinstance(raw, str):
                    continue
                try:
                    resume = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(resume, dict) and resume.get("type") == "external_work_resume":
                    external_tasks.extend(item for item in resume.get("tasks", []) if isinstance(item, dict))
            calls_by_id, outputs_by_id = _responses_function_item_maps(payload)
            image_pairs.extend((call, json.loads(outputs_by_id[call_id]["output"])["tool_result"]) for call_id, call in calls_by_id.items() if call.get("name") == "generate_image")
            assert {item.get("task_id") for item in external_tasks} == set(task_by_id) and len(external_tasks) == len(task_by_id)
            for item in external_tasks:
                task = task_by_id[item["task_id"]]
                assert item["tool_call"] == {"id": task.tool_call_id, "name": task.tool_name, "arguments": task.arguments}
                assert item["task"] == task.result["content"] and item["request_context"] == context_by_id[task.id]
            assert len(image_pairs) == len(tasks) and {call["call_id"] for call, _output in image_pairs} == {task.tool_call_id for task in tasks}
            for call, output in image_pairs:
                task = next(task for task in tasks if task.tool_call_id == call["call_id"])
                assert json.loads(call["arguments"]) == task.arguments and output["status"] == "queued" and output["task_id"] == task.id

        reply_data = {}
        for work_id in (round_one_work_id, round_two_work_id):
            reply = await runtime.client.get(f"/api/v1/chat/reply-works/{work_id}")
            assert reply.status_code == 200
            reply_data[work_id] = reply.json()["data"]
            assert reply_data[work_id]["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert len({data["resolved_work_id"] for data in reply_data.values()}) == 1 == len({json.dumps(data["response"], sort_keys=True) for data in reply_data.values()})
        assert all(set(data["request_ids"]) >= {"round-one", "round-two"} for data in reply_data.values())
        response = reply_data[round_one_work_id]["response"]
        response_files = response["files"]
        assert len(response_files) == len(all_prompts) and len({file["id"] for file in response_files}) == len(all_prompts)
        assert json.loads(response["choices"][0]["message"]["content"]) == {"type": "assistant_files", "text": "所有图片已发送", "files": response_files}
        assert {send_file_to_user_module.resolve_file_token(file["id"]).resolve() for file in response_files} == set(generated_paths.values())
        for file in response_files:
            download = await runtime.client.get(file["download_url"])
            assert download.status_code == 200 and download.content == runtime.image_bytes

        history_response = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": runtime.session_id})
        assert history_response.status_code == 200
        history = history_response.json()["data"]
        assistant_files = []
        for item in history:
            content = item.get("content")
            if item.get("role") == MessageRole.ASSISTANT.value and isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    continue
            if item.get("role") == MessageRole.ASSISTANT.value and isinstance(content, dict) and content.get("type") == "assistant_files":
                assistant_files.append((item, content))
        assert len(assistant_files) == 1 and assistant_files[0][0]["id"] == response["message_id"] and assistant_files[0][1]["files"] == response_files
        history_user_texts = [item.get("content") for item in history if item.get("role") == MessageRole.USER.value]
        assert round_one_text in history_user_texts and round_two_text in history_user_texts

        root_id = next(iter({data["resolved_work_id"] for data in reply_data.values()}))
        works_by_id = {work.id: work for work in works}
        assert len(works_by_id) == 2 and all(work.work_type == SessionReplyWorkType.FOREGROUND_REPLY for work in works_by_id.values())
        assert works_by_id[root_id].status == SessionReplyWorkStatus.SUCCEEDED
        merged_id = round_two_work_id if root_id == round_one_work_id else round_one_work_id
        assert works_by_id[merged_id].status == SessionReplyWorkStatus.MERGED and works_by_id[merged_id].merged_into_id == root_id
        assert works_by_id[root_id].result_message_id == response["message_id"]
        db_files = []
        for message in messages:
            if message.role != MessageRole.ASSISTANT:
                continue
            try:
                content = json.loads(message.content or "")
            except json.JSONDecodeError:
                continue
            if isinstance(content, dict) and content.get("type") == "assistant_files":
                db_files.append(message)
        assert len(db_files) == 1 and db_files[0].id == response["message_id"]
        proactive = [event.event for event in events if event.event.get("type") == "proactive_reply"]
        assert len(proactive) == 1 and proactive[0]["work_id"] == root_id and proactive[0]["message_id"] == response["message_id"]
        assert proactive[0]["content"] == "所有图片已发送" and proactive[0]["files"] == response_files

        task_snapshot = {task.id: (task.status, task.attempt_count, copy.deepcopy(task.result), copy.deepcopy(task.extra)) for task in tasks}
        work_snapshot = {work.id: (work.status, work.merged_into_id, work.result_message_id, copy.deepcopy(work.execution_state)) for work in works}
        message_snapshot = {message.id: (message.role, message.type, message.content) for message in messages if message.id is not None}
        event_snapshot = [(event.id, event.dedupe_key, copy.deepcopy(event.event)) for event in events]
        image_count, provider_count = len(runtime.image_requests), len(runtime.provider_payloads)
        await runtime.complete_images()
        assert await runtime.run_next() is None
        assert len(runtime.image_requests) == image_count == len(all_prompts) and len(runtime.provider_payloads) == provider_count
        repeated_tasks, repeated_works, repeated_messages, repeated_events = await state()
        assert {task.id: (task.status, task.attempt_count, task.result, task.extra) for task in repeated_tasks} == task_snapshot
        assert {work.id: (work.status, work.merged_into_id, work.result_message_id, work.execution_state) for work in repeated_works} == work_snapshot
        assert {message.id: (message.role, message.type, message.content) for message in repeated_messages if message.id is not None} == message_snapshot
        assert [(event.id, event.dedupe_key, event.event) for event in repeated_events] == event_snapshot
        repeated_history = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": runtime.session_id})
        assert repeated_history.status_code == 200 and repeated_history.json()["data"] == history
