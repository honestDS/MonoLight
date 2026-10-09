import asyncio
import base64
import copy
import io
import json
import os
from collections import Counter, deque
from collections.abc import AsyncGenerator
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.background_tasks.manager as manager_module
import app.core.background_tasks.reply_trigger as reply_trigger_module
import app.core.background_tasks.runner as runner_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.crud.session.reply_work_item as reply_work_item_module
import app.core.dispatcher as dispatcher_module
import app.core.dispatchers.background as background_dispatcher_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.tools.send_file_to_user as send_file_to_user_module
import app.tasks as tasks_module
import app.transformers.openai.image_generation as image_generation_transformer_module
from app.api.v1 import files as files_module
from app.core.dispatchers import ChatDispatcher
from app.core.paths import get_user_temp_dir
from app.core.security import get_current_user
from app.core.tools import get_tools_for_profile
from app.core.tools.image_generation import ImageGenerationExecutor
from app.core.utils.background_task_result import build_background_task_success_result
from app.core.utils.dispatcher.process_single_tool import process_single_tool
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.handler import register_handlers
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.user import User
from app.providers.database import get_db
from app.providers.image_generation import ImageGenerationClient
from app.schemas.response import SentFile
from app.transformers.openai.responses import OpenAIResponsesTransformer
from tests.database_support import clone_sqlite_schema

UID = "generated-image-user"
PROFILE_ID = 1
CHANNEL_ID = 1
MODEL_ID = "image-reply-model"
USER_MESSAGE = "Generate a native image reply."
IMAGE_TEXT = "Here is the generated image."

_FORMAT_DETAILS = {
    "png": ("PNG", "image/png", "png"),
    "jpeg": ("JPEG", "image/jpeg", "jpg"),
    "webp": ("WEBP", "image/webp", "webp"),
}
_GENERATED_IMAGE_CASES = [
    pytest.param(
        False,
        with_text,
        output_format,
        "terminal-only",
        id=f"nonstream-{output_format}-{'text' if with_text else 'image'}",
    )
    for with_text in (False, True)
    for output_format in _FORMAT_DETAILS
] + [
    pytest.param(
        True,
        with_text,
        output_format,
        delivery,
        id=f"stream-{delivery}-{output_format}-{'text' if with_text else 'image'}",
    )
    for with_text in (False, True)
    for output_format in _FORMAT_DETAILS
    for delivery in ("done-terminal", "terminal-only")
]
_GENERATE_IMAGE_CASES = [
    pytest.param(
        with_reference,
        output_format,
        id=f"openai-image-{'reference' if with_reference else 'no-reference'}-{output_format}",
    )
    for with_reference in (False, True)
    for output_format in _FORMAT_DETAILS
]


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "generated-image-workflow.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    await clone_sqlite_schema(database_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _profile_configs() -> dict[str, Any]:
    return {
        "channel": {
            "chat_channel": {
                "chat_timeout": 60,
                "rules": [
                    {
                        "channel_id": CHANNEL_ID,
                        "model_id": MODEL_ID,
                        "priority": 1,
                        "weight": 1,
                    }
                ],
            }
        },
        "security": {"audit_threshold": 0},
        "tool": {"enabled_tools": []},
        "other": {"context_summary_threshold_percent": 90},
        "memory": {"enabled": False, "precheck_enabled": False},
    }


def _model_channel() -> ModelChannel:
    return ModelChannel(
        id=CHANNEL_ID,
        name="generated-image-test-channel",
        api_key="enc:v1:stored-test-key",
        base_url="https://llm.invalid",
        model_ids=[
            {
                "model_id": MODEL_ID,
                "usage": "CHAT",
                "protocol": "OPENAI_RESPONSES",
                "context_window_k": 32,
                "max_tokens": 512,
                "temperature": 0,
                "top_p": 1,
            }
        ],
    )


def _patch_runtime_database(monkeypatch: pytest.MonkeyPatch, factory: async_sessionmaker[AsyncSession]) -> None:
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"g" * 32)


async def _seed_conversation(factory: async_sessionmaker[AsyncSession], session_id: str) -> InternalMessage:
    async with factory() as db:
        db.add(_model_channel())
        db.add(
            Profile(
                id=PROFILE_ID,
                uid=UID,
                name=f"generated-image-profile-{session_id}",
                configs=_profile_configs(),
            )
        )
        db.add(User(uid=UID, username="generated_image_user"))
        await db.flush()
        db.add(
            ChatSession(
                session_id=session_id,
                uid=UID,
                profile_id=PROFILE_ID,
                goal_mode=False,
                max_turns=5,
                source="http",
            )
        )
        await db.flush()
        initial_message = Message(
            session_id=session_id,
            uid=UID,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=USER_MESSAGE,
            profile_id=PROFILE_ID,
            is_processed=False,
        )
        db.add(initial_message)
        await db.commit()
        await db.refresh(initial_message)
        assert initial_message.id is not None
        return InternalMessage(
            id=initial_message.id,
            role=MessageRole.USER,
            content=initial_message.content,
            created_at=initial_message.created_at.timestamp(),
        )


def _image_fixture(output_format: str, *, color: tuple[int, int, int] = (32, 128, 224)) -> tuple[bytes, str]:
    image = Image.new("RGB", (2, 2), color=color)
    buffer = io.BytesIO()
    try:
        image.save(buffer, format=_FORMAT_DETAILS[output_format][0])
    finally:
        image.close()
    raw = buffer.getvalue()
    return raw, base64.b64encode(raw).decode("ascii")


def _raw_response(*, image_id: str, image_data: str, output_format: str, with_text: bool) -> dict[str, Any]:
    image_item = {
        "type": "image_generation_call",
        "status": "completed",
        "id": image_id,
        "result": image_data,
        "output_format": output_format,
        "metadata": {"provider_only": "opaque"},
    }
    output: list[dict[str, Any]] = []
    if with_text:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": IMAGE_TEXT}],
            }
        )
    output.append(image_item)
    return {
        "id": f"resp-{image_id}",
        "object": "response",
        "status": "completed",
        "model": MODEL_ID,
        "output": output,
        "usage": {"input_tokens": 9, "output_tokens": 7, "total_tokens": 16},
        "metadata": {"provider_only": "opaque"},
    }


def _stream_events(raw_response: dict[str, Any], *, with_text: bool, delivery: str) -> list[dict[str, Any]]:
    image_item = raw_response["output"][-1]
    events: list[dict[str, Any]] = []
    if with_text:
        events.append(
            {
                "type": "response.output_text.delta",
                "delta": IMAGE_TEXT,
                "output_index": 0,
                "content_index": 0,
            }
        )
    if delivery == "done-terminal":
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": 1 if with_text else 0,
                "item": image_item,
            }
        )
    events.append({"type": "response.completed", "response": copy.deepcopy(raw_response)})
    return events


def _patch_responses_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_response: dict[str, Any],
    stream: bool,
    stream_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if stream:

        async def fake_stream(_transformer, **kwargs: Any):
            calls.append(kwargs)
            normalize_event = kwargs["normalize_event"]
            for official_event in stream_events:
                normalized, _has_payload = normalize_event(official_event)
                if normalized is not None:
                    yield normalized

        monkeypatch.setattr(OpenAIResponsesTransformer, "_stream_sse_json", fake_stream)
    else:

        async def fake_post(_transformer, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return copy.deepcopy(raw_response)

        monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", fake_post)
    return calls


def _assert_file_entries(
    files: list[dict[str, Any]],
    *,
    expected_bytes: bytes,
    expected_dir: Path,
    expected_mime_type: str,
    expected_suffix: str,
) -> dict[str, Any]:
    assert len(files) == 1
    file_entry = files[0]
    assert file_entry["mime_type"] == expected_mime_type
    assert file_entry["previewable"] is True
    assert file_entry["size"] == len(expected_bytes)
    assert file_entry["name"].endswith(f".{expected_suffix}")
    resolved_path = send_file_to_user_module.resolve_file_token(file_entry["id"])
    assert resolved_path.parent == expected_dir.resolve()
    assert resolved_path.read_bytes() == expected_bytes
    return file_entry


async def _serialize_multipart_form(form: Any) -> bytes:
    chunks = bytearray()

    class _Writer:
        async def write(self, data: bytes) -> None:
            chunks.extend(data)

        async def write_eof(self) -> None:
            return None

        async def drain(self) -> None:
            return None

    await form.write(_Writer())
    return bytes(chunks)


@pytest_asyncio.fixture
async def batch_image_workflow(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[SimpleNamespace]:
    session_id = "batch-image-workflow"
    expected_bytes, image_data = _image_fixture("png")
    model_outputs: deque[list[dict[str, Any]]] = deque()
    gateway_requests: list[dict[str, Any]] = []
    image_requests: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    active_prompts: set[str] = set()
    gates: dict[str, asyncio.Event] = {}
    started_queue: asyncio.Queue[str] = asyncio.Queue()
    run_handles: dict[int, asyncio.Task] = {}
    workflow = SimpleNamespace(peak_active=0)

    async def fake_generate_image(**kwargs: Any) -> dict[str, Any]:
        image_requests.append(copy.deepcopy(kwargs))
        prompt = kwargs.get("prompt")
        if not isinstance(prompt, str):
            raise AssertionError("image generation prompt must be a string")
        gate = gates.setdefault(prompt, asyncio.Event())
        active_prompts.add(prompt)
        workflow.peak_active = max(workflow.peak_active, len(active_prompts))
        await started_queue.put(prompt)
        try:
            await gate.wait()
            return {
                "created": 1,
                "data": [{"b64_json": image_data}],
                "model": kwargs.get("model_id", "background-image-model"),
            }
        finally:
            active_prompts.discard(prompt)

    async def fake_post(_transformer: OpenAIResponsesTransformer, **kwargs: Any) -> dict[str, Any]:
        payload = copy.deepcopy(kwargs["payload"])
        gateway_requests.append(payload)
        input_items = payload.get("input") if isinstance(payload.get("input"), list) else []
        function_ids: list[str] = []
        call_ids: list[str] = []
        output_call_ids: list[str] = []
        seen_call_ids: set[str] = set()
        seen_output_call_ids: set[str] = set()
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                function_id = item.get("id") or item.get("call_id")
                call_id = item.get("call_id") or item.get("id")
                assert isinstance(function_id, str) and function_id
                assert isinstance(call_id, str) and call_id
                assert function_id not in function_ids
                assert call_id not in seen_call_ids
                function_ids.append(function_id)
                call_ids.append(call_id)
                seen_call_ids.add(call_id)
            elif item.get("type") == "function_call_output":
                output_call_id = item.get("call_id") or item.get("id")
                assert isinstance(output_call_id, str) and output_call_id
                assert output_call_id not in seen_output_call_ids
                assert output_call_id in seen_call_ids
                output_call_ids.append(output_call_id)
                seen_output_call_ids.add(output_call_id)

        assert set(call_ids) == set(output_call_ids)
        if not model_outputs:
            raise AssertionError("no preset model response")
        output = copy.deepcopy(model_outputs.popleft())
        return {
            "id": f"batch-response-{len(gateway_requests)}",
            "object": "response",
            "status": "completed",
            "model": MODEL_ID,
            "output": output,
            "usage": {"input_tokens": 9, "output_tokens": 7, "total_tokens": 16},
        }

    async def capture_event(_uid: str, _session_id: str, event: dict[str, Any]) -> None:
        events.append(copy.deepcopy(event))

    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ImageGenerationClient, "generate_image", fake_generate_image)
    monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", fake_post)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_event)

    initial_message = await _seed_conversation(session_factory, session_id)
    async with session_factory() as db:
        profile = await db.get(Profile, PROFILE_ID)
        channel = await db.get(ModelChannel, CHANNEL_ID)
        assert profile is not None
        assert channel is not None

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
        profile_configs["tool"].update(
            {
                "enabled_tools": ["generate_image", "file_tool"],
                "max_parallel_tools": 5,
                "background_task_max_concurrency": 2,
                "executor_max_workers": 1,
                "allowed_operation_dirs": [str(tmp_path.resolve())],
            }
        )
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
        validated_config = ProfileConfig.model_validate(profile_configs)
        profile.configs = validated_config.model_dump(mode="json")
        await db.commit()
        await db.refresh(profile)

    manager = manager_module.BackgroundTaskManager()
    monkeypatch.setattr(manager_module, "background_task_manager", manager)

    async def observed_run_background_task(task_id: int) -> None:
        current_task = asyncio.current_task()
        if current_task is None:
            raise AssertionError("background task wrapper requires an asyncio task")
        run_handles[task_id] = current_task
        await runner_module.run_background_task(task_id)

    monkeypatch.setattr(manager_module, "run_background_task", observed_run_background_task)
    for module in (
        manager_module,
        runner_module,
        reply_trigger_module,
        background_dispatcher_module,
        executor_lifecycle_module,
        executor_metadata_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)

    first_dispatch = True

    async def dispatch_round(tool_calls: list[InternalToolCall], user_text: str) -> dict[str, Any]:
        nonlocal first_dispatch
        if first_dispatch:
            async with session_factory() as db:
                persisted = await db.get(Message, initial_message.id)
                assert persisted is not None
                persisted.content = user_text
                persisted.is_processed = False
                await db.commit()
                persisted_initial_message = InternalMessage.from_user_input(
                    id=persisted.id,
                    content=persisted.content,
                    attachments=persisted.attachments,
                    created_at=persisted.created_at.timestamp(),
                )
            first_dispatch = False
        else:
            async with session_factory() as db:
                persisted_initial_message = await save_message(
                    db,
                    session_id,
                    UID,
                    MessageRole.USER,
                    MessageType.TEXT,
                    InternalMessage.from_user_input(content=user_text),
                    PROFILE_ID,
                    is_processed=False,
                )

        if tool_calls:
            model_outputs.append(
                [
                    {
                        "type": "function_call",
                        "id": tool_call.id,
                        "call_id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                        "status": "completed",
                    }
                    for tool_call in tool_calls
                ]
            )
        model_outputs.append(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Image generation tasks accepted.",
                        }
                    ],
                }
            ]
        )

        assert persisted_initial_message.id is not None
        async with session_factory() as db:
            return await ChatDispatcher.dispatch(
                db,
                message=user_text,
                uid=UID,
                session_id=session_id,
                persisted_initial_message=persisted_initial_message,
                persisted_profile_id=PROFILE_ID,
                frozen_user_message_ids=[persisted_initial_message.id],
                history_before_id=persisted_initial_message.id,
                session_source="http",
            )

    async def tasks() -> list[BackgroundTask]:
        async with session_factory() as db:
            result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == session_id).order_by(BackgroundTask.id.asc()))
            return list(result.scalars().all())

    async def summarize(task_id: int) -> tuple[Any, BackgroundTask, Message]:
        async with session_factory() as db:
            task = await db.get(BackgroundTask, task_id)
            if task is None or task.session_id != session_id or task.status != BackgroundTaskStatus.SUCCEEDED:
                raise AssertionError("background image task is not completed")
            prompt = (task.arguments or {}).get("prompt")
            model_outputs.append(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"Completed: {prompt}"}],
                    }
                ]
            )

        await reply_trigger_module.trigger_background_task_reply(task_id)
        worker_id = "batch-image-summary-worker"
        async with session_factory() as db:
            work = await reply_work_item_module.session_reply_work_item_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
            )
            if work is None or work.work_type != SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY or work.source_id != str(task_id):
                raise AssertionError("background image summary work item was not claimed")
            assert work.id is not None
            work_id = work.id

        await executor_lifecycle_module.execute_session_reply_work(work_id, worker_id)
        async with session_factory() as db:
            work = await reply_work_item_module.session_reply_work_item_crud.get(db, work_id)
            task = await db.get(BackgroundTask, task_id)
            if work is None or task is None or work.result_message_id is None:
                raise AssertionError("background image summary result was not persisted")
            result_message = await db.get(Message, work.result_message_id)
            if result_message is None:
                raise AssertionError("background image summary message was not persisted")
            return work, task, result_message

    workflow.factory = session_factory
    workflow.session_id = session_id
    workflow.profile = profile
    workflow.manager = manager
    workflow.gates = gates
    workflow.started_queue = started_queue
    workflow.run_handles = run_handles
    workflow.gateway_requests = gateway_requests
    workflow.image_requests = image_requests
    workflow.events = events
    workflow.active_prompts = active_prompts
    workflow.expected_bytes = expected_bytes
    workflow.image_data = image_data
    workflow.model_outputs = model_outputs
    workflow.dispatch_round = dispatch_round
    workflow.tasks = tasks
    workflow.summarize = summarize

    try:
        yield workflow
    finally:
        await manager.stop()


async def _complete_single_round_image_tasks(
    workflow: SimpleNamespace,
    pending_tasks: list[BackgroundTask],
) -> None:
    task_ids = [task.id for task in pending_tasks]
    assert all(isinstance(task_id, int) for task_id in task_ids)
    prompts_by_task_id: dict[int, str] = {}
    submission_snapshots: dict[int, list[dict[str, Any]]] = {}
    for task in pending_tasks:
        assert task.id is not None
        prompt = (task.arguments or {}).get("prompt")
        assert isinstance(prompt, str)
        prompts_by_task_id[task.id] = prompt
        extra = task.extra
        assert isinstance(extra, dict)
        submission_context = extra.get("submission_context")
        assert isinstance(submission_context, list)
        submission_snapshots[task.id] = copy.deepcopy(submission_context)

    async with workflow.factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == workflow.session_id).order_by(Message.id.asc()))
        source_messages = {message.id: message.content for message in result.scalars().all() if message.id is not None}

    expected_running_count = min(2, len(pending_tasks))
    await workflow.manager.schedule(workflow.profile)
    started_prompts = [await asyncio.wait_for(workflow.started_queue.get(), timeout=10) for _ in range(expected_running_count)]
    assert len(set(started_prompts)) == expected_running_count
    assert set(started_prompts) == {prompts_by_task_id[task.id] for task in pending_tasks[:expected_running_count] if task.id is not None}
    assert len(workflow.active_prompts) == expected_running_count

    persisted_tasks = await workflow.tasks()
    assert len(persisted_tasks) == len(pending_tasks)
    running_ids = {task.id for task in persisted_tasks if task.status == BackgroundTaskStatus.RUNNING}
    pending_ids = {task.id for task in persisted_tasks if task.status == BackgroundTaskStatus.PENDING}
    assert running_ids == {next(task.id for task in pending_tasks if task.arguments.get("prompt") == prompt) for prompt in started_prompts}
    assert pending_ids == {task.id for task in pending_tasks if task.id not in running_ids}
    initial_task_states = {task.id: (task.status, task.attempt_count) for task in persisted_tasks}

    image_request_count = len(workflow.image_requests)
    await workflow.manager.schedule(workflow.profile)
    assert len(workflow.image_requests) == image_request_count
    assert workflow.started_queue.empty()
    persisted_tasks = await workflow.tasks()
    assert len(persisted_tasks) == len(pending_tasks)
    assert {task.id: (task.status, task.attempt_count) for task in persisted_tasks} == initial_task_states

    started_task_ids = {task.id for task in pending_tasks[:expected_running_count]}
    completed_task_ids: set[int] = set()
    generated_paths: set[Path] = set()
    completed_work_ids: set[int] = set()
    for task in pending_tasks:
        assert task.id is not None
        prompt = prompts_by_task_id[task.id]
        assert task.id in started_task_ids
        assert prompt in workflow.active_prompts
        gate = workflow.gates.get(prompt)
        assert gate is not None
        gate.set()
        run_handle = workflow.run_handles.get(task.id)
        assert run_handle is not None
        await asyncio.wait_for(asyncio.shield(run_handle), timeout=10)

        async with workflow.factory() as db:
            completed_task = await db.get(BackgroundTask, task.id)
            assert completed_task is not None
            assert completed_task.status == BackgroundTaskStatus.SUCCEEDED
            assert completed_task.attempt_count == 1
            assert completed_task.result is not None
            assert completed_task.extra.get("submission_context") == submission_snapshots[task.id]
            completed_result = copy.deepcopy(completed_task.result)

        assert completed_result["status"] == "succeeded"
        assert completed_result["tool_name"] == "generate_image"
        result_content = completed_result["content"]
        assert result_content["status"] == "success"
        send_file_arguments = result_content["send_file_to_user"]
        files = send_file_arguments["files"]
        assert isinstance(files, list)
        assert len(files) == 1
        generated_path = Path(files[0]["path"])
        expected_dir = (get_user_temp_dir(os.getcwd(), UID) / "generated_images").resolve()
        assert generated_path.parent == expected_dir
        assert generated_path.suffix.lower() == ".png"
        assert files[0]["mime_type"] == "image/png"
        assert generated_path.read_bytes() == workflow.expected_bytes
        assert generated_path.resolve() not in generated_paths
        generated_paths.add(generated_path.resolve())
        assert workflow.image_data not in json.dumps(completed_result, ensure_ascii=False)

        gateway_request_start = len(workflow.gateway_requests)
        event_start = len(workflow.events)
        work, replied_task, result_message = await workflow.summarize(task.id)
        assert work.status == SessionReplyWorkStatus.SUCCEEDED
        assert work.result_message_id == result_message.id
        assert replied_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        assert replied_task.extra.get("submission_context") == submission_snapshots[task.id]
        assert result_message.role == MessageRole.ASSISTANT
        assert result_message.type == MessageType.TEXT
        assert result_message.content == f"Completed: {prompt}"
        completed_work_ids.add(work.id)

        new_events = workflow.events[event_start:]
        assert len(new_events) == 1
        event_payload = new_events[0]
        assert event_payload["type"] == "proactive_reply"
        assert event_payload["task_id"] == task.id
        assert event_payload["message_id"] == result_message.id
        assert event_payload["content"] == f"Completed: {prompt}"

        new_gateway_requests = workflow.gateway_requests[gateway_request_start:]
        assert len(new_gateway_requests) == 1
        input_items = new_gateway_requests[0]["input"]
        function_calls = [item for item in input_items if item.get("type") == "function_call"]
        function_outputs = [item for item in input_items if item.get("type") == "function_call_output"]
        assert len(function_calls) == 1
        assert len(function_outputs) == 1
        assert function_calls[0]["call_id"] == task.tool_call_id
        assert function_outputs[0]["call_id"] == task.tool_call_id
        assert function_calls[0]["name"] == "generate_image"
        assert json.loads(function_calls[0]["arguments"]) == task.arguments
        assert json.loads(function_outputs[0]["output"]) == completed_result
        assert workflow.image_data not in json.dumps(new_gateway_requests[0], ensure_ascii=False)

        completed_task_ids.add(task.id)
        await workflow.manager.schedule(workflow.profile)
        unstarted_tasks = [candidate for candidate in pending_tasks if candidate.id not in started_task_ids]
        if unstarted_tasks:
            next_prompt = await asyncio.wait_for(workflow.started_queue.get(), timeout=10)
            assert next_prompt in {prompts_by_task_id[candidate.id] for candidate in unstarted_tasks}
            next_task = next(candidate for candidate in unstarted_tasks if prompts_by_task_id[candidate.id] == next_prompt)
            assert next_task.id not in started_task_ids
            started_task_ids.add(next_task.id)
        else:
            assert workflow.started_queue.empty()

    assert completed_task_ids == set(task_ids)
    assert len(completed_work_ids) == len(pending_tasks)
    persisted_tasks = await workflow.tasks()
    assert len(persisted_tasks) == len(pending_tasks)
    assert all(task.status == BackgroundTaskStatus.SUCCEEDED for task in persisted_tasks)
    assert all(task.attempt_count == 1 for task in persisted_tasks)
    assert all(task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED for task in persisted_tasks)
    for task in persisted_tasks:
        assert task.id is not None
        assert task.extra.get("submission_context") == submission_snapshots[task.id]

    assert Counter(request["prompt"] for request in workflow.image_requests) == Counter(prompts_by_task_id.values())
    assert len(workflow.image_requests) == len(pending_tasks)
    assert all(request["n"] == 1 for request in workflow.image_requests)
    assert all(request["model_id"] == "background-image-model" for request in workflow.image_requests)
    assert all(request["protocol"] == "openai_image" for request in workflow.image_requests)
    assert workflow.peak_active == min(2, len(pending_tasks))
    assert workflow.active_prompts == set()

    generated_dir = (get_user_temp_dir(os.getcwd(), UID) / "generated_images").resolve()
    generated_files = [path.resolve() for path in generated_dir.iterdir() if path.is_file() and not path.name.startswith(".")]
    assert len(generated_files) == len(pending_tasks)
    assert len(set(generated_files)) == len(generated_files)
    assert set(generated_files) == generated_paths
    assert all(path.suffix.lower() == ".png" and path.read_bytes() == workflow.expected_bytes for path in generated_files)

    async with workflow.factory() as db:
        for message_id, content in source_messages.items():
            source_message = await db.get(Message, message_id)
            assert source_message is not None
            assert source_message.content == content


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
    batch_image_workflow: SimpleNamespace,
    requested_count: int,
) -> None:
    tool_calls = [
        InternalToolCall(
            id=f"batch-generate-image-{requested_count}-{index + 1}",
            name="generate_image",
            arguments={"prompt": f"Batch image prompt {index + 1}"},
        )
        for index in range(requested_count)
    ]
    response = await batch_image_workflow.dispatch_round(
        tool_calls,
        f"Queue {requested_count} background image tasks.",
    )

    assert response["choices"][0]["message"]["content"] == "Image generation tasks accepted."
    assert batch_image_workflow.image_requests == []

    pending_tasks = await batch_image_workflow.tasks()
    assert len(pending_tasks) == requested_count
    async with batch_image_workflow.factory() as db:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == batch_image_workflow.session_id,
                Message.role == MessageRole.ASSISTANT,
                Message.type == MessageType.TOOL_CALL,
            )
            .order_by(Message.id.asc())
        )
        assistant_tool_messages = list(result.scalars().all())
    assert len(assistant_tool_messages) == 1
    persisted_assistant = InternalMessage.model_validate_json(assistant_tool_messages[0].content or "{}")
    persisted_calls = persisted_assistant.tool_calls or []
    assert len(persisted_calls) == requested_count
    assert len({tool_call.id for tool_call in persisted_calls}) == requested_count
    assert [(tool_call.name, tool_call.arguments) for tool_call in persisted_calls] == [(tool_call.name, tool_call.arguments) for tool_call in tool_calls]
    expected_calls = {tool_call.id: tool_call for tool_call in persisted_calls}
    assert {task.tool_call_id for task in pending_tasks} == set(expected_calls)
    for task in pending_tasks:
        assert task.status == BackgroundTaskStatus.PENDING
        assert task.tool_name == "generate_image"
        assert task.arguments == expected_calls[task.tool_call_id].arguments
        assert task.attempt_count == 0
        assert task.auto_reply is True
        assert task.reply_status == BackgroundTaskReplyStatus.PENDING

    async with batch_image_workflow.factory() as db:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == batch_image_workflow.session_id,
                Message.role == MessageRole.TOOL,
                Message.type == MessageType.TOOL_RESULT,
            )
            .order_by(Message.id.asc())
        )
        tool_messages = list(result.scalars().all())
    assert len(tool_messages) == requested_count
    queued_by_call_id: dict[str, dict[str, Any]] = {}
    for message in tool_messages:
        internal_message = InternalMessage.model_validate_json(message.content or "{}")
        assert internal_message.role == MessageRole.TOOL
        assert internal_message.tool_call_id is not None
        assert internal_message.tool_call_id not in queued_by_call_id
        payload = json.loads(internal_message.content or "{}")
        assert payload["status"] == "queued"
        assert payload["tool_name"] == "generate_image"
        queued_by_call_id[internal_message.tool_call_id] = payload

    assert set(queued_by_call_id) == set(expected_calls)
    task_by_call_id = {task.tool_call_id: task for task in pending_tasks}
    for call_id, payload in queued_by_call_id.items():
        assert payload["task_id"] == task_by_call_id[call_id].id

    await _complete_single_round_image_tasks(batch_image_workflow, pending_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_count",
    [
        pytest.param(5, id="requested-5"),
        pytest.param(7, id="requested-7"),
    ],
)
async def test_mixed_tools_share_round_image_submission_limit(
    batch_image_workflow: SimpleNamespace,
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

    response = await batch_image_workflow.dispatch_round(
        tool_calls,
        f"Submit {requested_count} mixed tools in one round.",
    )

    assert response["choices"][0]["message"]["content"] == "Image generation tasks accepted."
    assert batch_image_workflow.image_requests == []

    input_items = batch_image_workflow.gateway_requests[-1]["input"]
    function_calls = [item for item in input_items if item.get("type") == "function_call"]
    assert len(function_calls) == requested_count
    assert [(item["name"], json.loads(item["arguments"])) for item in function_calls] == [(tool_call.name, tool_call.arguments) for tool_call in tool_calls]
    call_ids = [item["call_id"] for item in function_calls]
    assert len(set(call_ids)) == requested_count

    async with batch_image_workflow.factory() as db:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == batch_image_workflow.session_id,
                Message.role == MessageRole.TOOL,
                Message.type == MessageType.TOOL_RESULT,
            )
            .order_by(Message.id.asc())
        )
        tool_messages = list(result.scalars().all())
    assert len(tool_messages) == requested_count
    result_by_call_id: dict[str, dict[str, Any]] = {}
    for message in tool_messages:
        internal_message = InternalMessage.model_validate_json(message.content or "{}")
        assert internal_message.role == MessageRole.TOOL
        assert internal_message.tool_call_id is not None
        assert internal_message.tool_call_id not in result_by_call_id
        result_by_call_id[internal_message.tool_call_id] = json.loads(internal_message.content or "{}")
    assert set(result_by_call_id) == set(call_ids)

    for call_id, path, marker in zip(call_ids[::2], input_paths, input_contents):
        result_payload = result_by_call_id[call_id]
        assert result_payload["status"] == "success"
        assert result_payload["operation"] == "read"
        assert marker in result_payload["content"]
        assert path.read_text(encoding="utf-8") == marker

    image_call_ids = [call_ids[index] for index in (1, 3, 4)]
    image_call_arguments = {call_id: tool_calls[index].arguments for call_id, index in zip(image_call_ids, (1, 3, 4))}
    pending_tasks = await batch_image_workflow.tasks()
    assert len(pending_tasks) == 3
    assert {task.tool_call_id for task in pending_tasks} == set(image_call_ids)
    task_by_call_id = {task.tool_call_id: task for task in pending_tasks}
    for call_id, arguments in image_call_arguments.items():
        task = task_by_call_id[call_id]
        assert task.status == BackgroundTaskStatus.PENDING
        assert task.tool_name == "generate_image"
        assert task.attempt_count == 0
        assert task.arguments == arguments
        queued_payload = result_by_call_id[call_id]
        assert queued_payload["status"] == "queued"
        assert queued_payload["tool_name"] == "generate_image"
        assert queued_payload["task_id"] == task.id

    if requested_count == 7:
        for call_id in call_ids[5:]:
            rejected_payload = result_by_call_id[call_id]
            assert rejected_payload["status"] == "failed"
            assert rejected_payload["error"] == "parallel_limit_exceeded"
            assert rejected_payload["requested"] == 7
            assert rejected_payload["limit"] == 5
            assert rejected_payload["executed"] is False
            assert "task_id" not in rejected_payload
        assert not rejected_write_path.exists()

    await _complete_single_round_image_tasks(batch_image_workflow, pending_tasks)

    await batch_image_workflow.manager.schedule(batch_image_workflow.profile)
    scheduled_tasks = await batch_image_workflow.tasks()
    assert len(scheduled_tasks) == 3
    assert {task.id for task in scheduled_tasks} == {task.id for task in pending_tasks}
    assert Counter(request["prompt"] for request in batch_image_workflow.image_requests) == Counter(image_prompts)
    assert len(batch_image_workflow.image_requests) == 3
    assert rejected_image_prompt not in {request["prompt"] for request in batch_image_workflow.image_requests}
    assert len(batch_image_workflow.events) == 3
    assert {event["task_id"] for event in batch_image_workflow.events} == {task.id for task in pending_tasks}
    assert batch_image_workflow.started_queue.empty()
    assert batch_image_workflow.active_prompts == set()
    assert all(path.read_text(encoding="utf-8") == content for path, content in zip(input_paths, input_contents))
    if requested_count == 7:
        assert not rejected_write_path.exists()


@pytest.mark.asyncio
async def test_batch_image_generation_over_limit_rejects_excess_without_running(
    batch_image_workflow: SimpleNamespace,
) -> None:
    tool_calls = [
        InternalToolCall(
            id=f"batch-generate-image-over-limit-{index + 1}",
            name="generate_image",
            arguments={"prompt": f"Over-limit background image prompt {index + 1}"},
        )
        for index in range(8)
    ]
    response = await batch_image_workflow.dispatch_round(
        tool_calls,
        "Queue eight background image tasks over the parallel limit.",
    )

    assert response["choices"][0]["message"]["content"] == "Image generation tasks accepted."
    assert batch_image_workflow.image_requests == []

    input_items = batch_image_workflow.gateway_requests[-1]["input"]
    function_calls = [item for item in input_items if item.get("type") == "function_call"]
    assert len(function_calls) == 8
    assert [(item["name"], json.loads(item["arguments"])) for item in function_calls] == [(tool_call.name, tool_call.arguments) for tool_call in tool_calls]
    call_ids = [item["call_id"] for item in function_calls]
    assert len(set(call_ids)) == 8

    async with batch_image_workflow.factory() as db:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == batch_image_workflow.session_id,
                Message.role == MessageRole.TOOL,
                Message.type == MessageType.TOOL_RESULT,
            )
            .order_by(Message.id.asc())
        )
        tool_messages = list(result.scalars().all())
    assert len(tool_messages) == 8
    result_by_call_id: dict[str, dict[str, Any]] = {}
    for message in tool_messages:
        internal_message = InternalMessage.model_validate_json(message.content or "{}")
        assert internal_message.role == MessageRole.TOOL
        assert internal_message.tool_call_id is not None
        assert internal_message.tool_call_id not in result_by_call_id
        result_by_call_id[internal_message.tool_call_id] = json.loads(internal_message.content or "{}")
    assert set(result_by_call_id) == set(call_ids)

    pending_tasks = await batch_image_workflow.tasks()
    assert len(pending_tasks) == 5
    assert [task.tool_call_id for task in pending_tasks] == call_ids[:5]
    for index, task in enumerate(pending_tasks):
        assert task.status == BackgroundTaskStatus.PENDING
        assert task.tool_name == "generate_image"
        assert task.attempt_count == 0
        assert task.arguments == tool_calls[index].arguments
        queued_payload = result_by_call_id[call_ids[index]]
        assert queued_payload["status"] == "queued"
        assert queued_payload["tool_name"] == "generate_image"
        assert queued_payload["task_id"] == task.id

    for call_id in call_ids[5:]:
        rejected_payload = result_by_call_id[call_id]
        assert rejected_payload["status"] == "failed"
        assert rejected_payload["error"] == "parallel_limit_exceeded"
        assert rejected_payload["requested"] == 8
        assert rejected_payload["limit"] == 5
        assert rejected_payload["executed"] is False
        assert "task_id" not in rejected_payload

    await _complete_single_round_image_tasks(batch_image_workflow, pending_tasks)

    await batch_image_workflow.manager.schedule(batch_image_workflow.profile)
    scheduled_tasks = await batch_image_workflow.tasks()
    assert len(scheduled_tasks) == 5
    assert [task.tool_call_id for task in scheduled_tasks] == call_ids[:5]
    assert len(batch_image_workflow.image_requests) == 5
    assert Counter(request["prompt"] for request in batch_image_workflow.image_requests) == Counter(tool_call.arguments["prompt"] for tool_call in tool_calls[:5])
    assert batch_image_workflow.started_queue.empty()
    assert batch_image_workflow.active_prompts == set()
    assert len(batch_image_workflow.events) == 5
    assert {event["task_id"] for event in batch_image_workflow.events} == {task.id for task in pending_tasks}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream", "with_text", "output_format", "stream_image_delivery"),
    _GENERATED_IMAGE_CASES,
)
async def test_native_generated_image_reply_round_trips_through_dispatch_persistence_and_download(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    with_text: bool,
    output_format: str,
    stream_image_delivery: str,
) -> None:
    session_id = f"generated-image-{output_format}-{'text' if with_text else 'image'}-{'stream' if stream else 'nonstream'}-{stream_image_delivery}"
    expected_bytes, image_data = _image_fixture(output_format)
    raw_response = _raw_response(
        image_id=f"image-{output_format}-{'text' if with_text else 'image'}",
        image_data=image_data,
        output_format=output_format,
        with_text=with_text,
    )
    network_calls = _patch_responses_transport(
        monkeypatch,
        raw_response=raw_response,
        stream=stream,
        stream_events=_stream_events(raw_response, with_text=with_text, delivery=stream_image_delivery),
    )
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.chdir(tmp_path)

    events: list[dict[str, Any]] = []
    turn_end_payloads: list[dict[str, Any]] = []
    expected_dir = get_user_temp_dir(os.getcwd(), UID) / "generated_images"
    expected_mime_type = _FORMAT_DETAILS[output_format][1]
    expected_suffix = _FORMAT_DETAILS[output_format][2]

    async def capture(event_payload: dict[str, Any]) -> None:
        events.append(event_payload)
        if event_payload.get("type") != "turn_end":
            return
        content = event_payload.get("content")
        assert isinstance(content, str)
        assistant_files = json.loads(content)
        assert assistant_files["type"] == "assistant_files"
        _assert_file_entries(
            assistant_files["files"],
            expected_bytes=expected_bytes,
            expected_dir=expected_dir,
            expected_mime_type=expected_mime_type,
            expected_suffix=expected_suffix,
        )
        turn_end_payloads.append(assistant_files)

    initial_message = await _seed_conversation(session_factory, session_id)
    async with session_factory() as db:
        response = await ChatDispatcher.dispatch(
            db,
            message=USER_MESSAGE,
            uid=UID,
            session_id=session_id,
            persisted_initial_message=initial_message,
            persisted_profile_id=PROFILE_ID,
            frozen_user_message_ids=[initial_message.id],
            history_before_id=initial_message.id,
            session_source="http",
            stream_event_callback=capture if stream else None,
        )

    assert len(network_calls) == 1
    assert network_calls[0]["url"] == "https://llm.invalid/responses"
    assert network_calls[0]["payload"]["model"] == MODEL_ID
    assert network_calls[0]["payload"]["stream"] is stream

    response_files = response["files"]
    assert isinstance(response_files, list)
    response_file = _assert_file_entries(
        response_files,
        expected_bytes=expected_bytes,
        expected_dir=expected_dir,
        expected_mime_type=expected_mime_type,
        expected_suffix=expected_suffix,
    )
    choice_message = response["choices"][0]["message"]
    assert response["choices"][0]["finish_reason"] == "stop"
    assert isinstance(choice_message["content"], str)
    response_content = json.loads(choice_message["content"])
    assert response_content["type"] == "assistant_files"
    assert response_content["text"] == (IMAGE_TEXT if with_text else "")
    assert [SentFile.model_validate(file_entry) for file_entry in response_content["files"]] == [SentFile.model_validate(file_entry) for file_entry in response_files]
    assert response["history"][-1]["content"] == choice_message["content"]
    assert response["history"][-1]["role"] == MessageRole.ASSISTANT

    if stream:
        assert len(turn_end_payloads) == 1
        assert turn_end_payloads[0] == response_content
    else:
        assert events == []

    generated_dir_files = [path for path in expected_dir.iterdir() if path.is_file() and not path.name.startswith(".")]
    assert len(generated_dir_files) == 1
    assert generated_dir_files[0].read_bytes() == expected_bytes
    assert not get_user_temp_dir(os.getcwd(), session_id).exists()

    async with session_factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        messages = list(result.scalars().all())
        assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
        assert len(assistant_messages) == 1
        assistant_message = assistant_messages[0]
        await db.refresh(assistant_message)

    assert assistant_message.type == MessageType.TEXT
    assert isinstance(assistant_message.content, str)
    assert json.loads(assistant_message.content) == response_content
    assert image_data not in assistant_message.content
    serialized_observations = json.dumps({"response": response, "events": events}, ensure_ascii=False)
    assert image_data not in serialized_observations
    assert image_data not in json.dumps(response_content, ensure_ascii=False)

    app = FastAPI()
    app.include_router(files_module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        download_response = await client.get(response_file["download_url"])
    assert download_response.status_code == 200
    assert download_response.content == expected_bytes
    assert download_response.headers["content-type"].split(";", 1)[0] == expected_mime_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_reference", "output_format"),
    _GENERATE_IMAGE_CASES,
)
async def test_generate_image_background_workflow_supports_openai_images_and_reference_edits(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_reference: bool,
    output_format: str,
) -> None:
    session_id = f"generated-image-openai-image-{'reference' if with_reference else 'generation'}-{output_format}"
    expected_bytes, image_data = _image_fixture(output_format)
    reference_bytes, _ = _image_fixture("png", color=(224, 64, 32))
    image_model_id = f"image-openai-image-{output_format}"
    prompt = f"Generate a {output_format} test image through openai-image."
    initial_message = await _seed_conversation(session_factory, session_id)

    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(files_module, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.chdir(tmp_path)

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as db:
            yield db

    async def override_get_current_user() -> User:
        return User(uid=UID, username="generated_image_user")

    files_app = FastAPI()
    files_app.include_router(files_module.router, prefix="/api/v1")
    files_app.dependency_overrides[get_db] = override_get_db
    files_app.dependency_overrides[get_current_user] = override_get_current_user

    reference_path: Path | None = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=files_app), base_url="http://test") as files_client:
        if with_reference:
            upload_response = await files_client.post(
                "/api/v1/upload",
                files={"file": ("reference.png", reference_bytes, "image/png")},
                data={"session_id": session_id},
            )
            assert upload_response.status_code == 200
            reference_path = Path(upload_response.json()["path"])
            assert reference_path.parent == get_user_temp_dir(os.getcwd(), UID).resolve()
            assert reference_path.read_bytes() == reference_bytes

        async with session_factory() as db:
            profile = await db.get(Profile, PROFILE_ID)
            channel = await db.get(ModelChannel, CHANNEL_ID)
            assert profile is not None
            assert channel is not None

            profile_configs = copy.deepcopy(profile.configs)
            profile_configs["channel"]["image_generation_channel"] = {
                "rules": [
                    {
                        "channel_id": CHANNEL_ID,
                        "model_id": image_model_id,
                        "priority": 1,
                        "weight": 1,
                    }
                ]
            }
            profile_configs["tool"]["enabled_tools"] = ["generate_image", "send_file_to_user"]
            profile_configs["tool"]["allowed_operation_dirs"] = [str(tmp_path.resolve())]
            profile.configs = profile_configs
            channel.base_url = "https://llm.invalid/v1"
            channel.model_ids = [
                *channel.model_ids,
                {
                    "model_id": image_model_id,
                    "usage": "IMAGE_GENERATION",
                    "protocol": "OPENAI_IMAGE",
                    "size": "1024x1024",
                    "quality": "auto",
                },
            ]
            await db.commit()
            await db.refresh(profile)
            await db.refresh(channel)

            cfg = ProfileConfig.model_validate(profile.configs)
            tools, _ = await get_tools_for_profile(db, profile)

            tool_by_name = {tool["function"]["name"]: tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
            assert {"generate_image", "send_file_to_user"}.issubset(tool_by_name)
            assert "reference_images" in tool_by_name["generate_image"]["function"]["parameters"]["properties"]
            assert any(item.get("usage") == "CHAT" and item.get("model_id") == MODEL_ID for item in channel.model_ids)
            assert any(item.get("usage") == "IMAGE_GENERATION" and item.get("model_id") == image_model_id and item.get("protocol") == "OPENAI_IMAGE" for item in channel.model_ids)

            arguments: dict[str, Any] = {"prompt": prompt}
            if reference_path is not None:
                arguments["reference_images"] = [str(reference_path)]

            tool_message = await process_single_tool(
                InternalToolCall(
                    id=f"generate-image-openai-image-{output_format}",
                    name="generate_image",
                    arguments=arguments,
                ),
                db,
                profile,
                cfg,
                [initial_message],
                "generated_image_user",
                session_id,
                1,
                UID,
                source_message_id=initial_message.id,
            )

        queued_payload = json.loads(tool_message.content or "{}")
        assert queued_payload["status"] == "queued"
        task_id = queued_payload["task_id"]
        assert isinstance(task_id, int)

        async with session_factory() as db:
            queued_task = await db.get(BackgroundTask, task_id)
            assert queued_task is not None
            assert queued_task.status == BackgroundTaskStatus.PENDING
            assert queued_task.arguments == arguments
            assert set(queued_task.arguments).issubset({"prompt", "reference_images"})
            assert "protocol" not in queued_task.arguments
            assert "mask" not in queued_task.arguments

        image_http_calls: list[dict[str, Any]] = []

        class _FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc_value, _traceback):
                return None

            async def text(self) -> str:
                return json.dumps({"created": 1, "data": [{"b64_json": image_data}]})

        class _FakeClientSession:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                image_http_calls.append({"session_args": args, "session_kwargs": kwargs})

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc_value, _traceback):
                return None

            def post(self, url: str, **kwargs: Any) -> _FakeResponse:
                image_http_calls.append({"url": url, "kwargs": kwargs})
                return _FakeResponse()

        monkeypatch.setattr(image_generation_transformer_module.aiohttp, "ClientSession", _FakeClientSession)
        monkeypatch.setattr(image_generation_transformer_module.aiohttp, "TCPConnector", lambda **_kwargs: object())

        await runner_module.run_background_task(task_id, worker_id=f"image-openai-image-{output_format}")

        async with session_factory() as db:
            completed_task = await db.get(BackgroundTask, task_id)
            assert completed_task is not None
            assert completed_task.status == BackgroundTaskStatus.SUCCEEDED
            assert completed_task.result is not None
            task_result = copy.deepcopy(completed_task.result)

        assert image_data not in json.dumps(task_result, ensure_ascii=False)
        assert task_result["status"] == "succeeded"
        assert task_result["tool_name"] == "generate_image"
        result_content = task_result["content"]
        send_arguments = result_content["send_file_to_user"]
        assert isinstance(send_arguments["files"], list)
        assert len(send_arguments["files"]) == 1
        generated_file = send_arguments["files"][0]
        generated_path = Path(generated_file["path"])
        assert generated_path.read_bytes() == expected_bytes
        assert generated_file["mime_type"] == _FORMAT_DETAILS[output_format][1]
        assert generated_path.suffix == f".{_FORMAT_DETAILS[output_format][2]}"

        send_executor = send_file_to_user_module.SendFileToUserExecutor(project_root=os.getcwd(), uid=UID)
        send_executor.set_config(cfg)
        sent_payload = json.loads(await send_executor.execute(**send_arguments))
        assert sent_payload["status"] == "success"
        sent_file = _assert_file_entries(
            sent_payload["files"],
            expected_bytes=expected_bytes,
            expected_dir=get_user_temp_dir(os.getcwd(), UID) / "generated_images",
            expected_mime_type=_FORMAT_DETAILS[output_format][1],
            expected_suffix=_FORMAT_DETAILS[output_format][2],
        )

        download_response = await files_client.get(sent_file["download_url"])
        assert download_response.status_code == 200
        assert download_response.content == expected_bytes
        assert download_response.headers["content-type"].split(";", 1)[0] == _FORMAT_DETAILS[output_format][1]

        assert len(image_http_calls) == 2
        request = image_http_calls[1]
        assert request["url"] == f"https://llm.invalid/v1/images/{'edits' if with_reference else 'generations'}"
        if with_reference:
            form = request["kwargs"].get("data")
            assert form is not None
            multipart = form()
            content_type = multipart.headers["Content-Type"]
            body = await _serialize_multipart_form(multipart)
            message = BytesParser(policy=policy.default).parsebytes(f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode() + body)
            assert message.is_multipart()
            fields: dict[str, str] = {}
            file_parts: list[tuple[str | None, str | None, str, bytes | None]] = []
            for part in message.iter_parts():
                field_name = part.get_param("name", header="content-disposition")
                field_data = part.get_payload(decode=True)
                if part.get_filename() is not None:
                    file_parts.append((field_name, part.get_filename(), part.get_content_type(), field_data))
                else:
                    assert field_name is not None
                    assert field_data is not None
                    fields[field_name] = field_data.decode()
            assert fields == {
                "model": image_model_id,
                "prompt": prompt,
                "n": "1",
                "size": "1024x1024",
                "quality": "auto",
            }
            assert len(file_parts) == 1
            field_name, filename, mime_type, file_data = file_parts[0]
            assert field_name == "image"
            assert filename is not None and filename.endswith(".png")
            assert mime_type == "image/png"
            assert file_data == reference_bytes
            assert "mask" not in fields
        else:
            payload = request["kwargs"].get("json")
            assert payload == {
                "model": image_model_id,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
                "quality": "auto",
            }
            assert "mask" not in payload


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
        return {
            "created": 1,
            "data": [{"b64_json": expected_image_data}],
            "model": "background-image-model",
        }

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
            if payload.get("tool_choice") != "none":
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
    assert gateway_requests[0]["tools"]
    assert gateway_requests[0]["tool_choice"] == "auto"
    assert "tools" not in gateway_requests[1]
    assert gateway_requests[1].get("tool_choice") == "none"
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for file_entry in sent_files:
            download_response = await client.get(file_entry["download_url"])
            assert download_response.status_code == 200
            assert download_response.content == expected_bytes
            assert download_response.headers["content-type"].split(";", 1)[0] == "image/png"


@pytest.mark.asyncio
async def test_mixed_image_sources_share_per_user_temp_size_quota(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_a = "mixed-image-quota-session-a"
    session_b = "mixed-image-quota-session-b"
    other_uid = "generated-image-other-user"
    expected_bytes, image_data = _image_fixture("png")
    raw_response = _raw_response(
        image_id="mixed-native-image",
        image_data=image_data,
        output_format="png",
        with_text=False,
    )
    network_calls = _patch_responses_transport(
        monkeypatch,
        raw_response=raw_response,
        stream=False,
        stream_events=[],
    )
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.chdir(tmp_path)

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(tasks_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", session_factory)

    initial_message = await _seed_conversation(session_factory, session_a)
    async with session_factory() as db:
        db.add(
            ChatSession(
                session_id=session_b,
                uid=UID,
                profile_id=PROFILE_ID,
                goal_mode=False,
                max_turns=5,
                source="http",
            )
        )
        await db.commit()

    async with session_factory() as db:
        response = await ChatDispatcher.dispatch(
            db,
            message=USER_MESSAGE,
            uid=UID,
            session_id=session_a,
            persisted_initial_message=initial_message,
            persisted_profile_id=PROFILE_ID,
            frozen_user_message_ids=[initial_message.id],
            history_before_id=initial_message.id,
            session_source="http",
        )

    assert len(network_calls) == 1
    native_files = response["files"]
    assert isinstance(native_files, list)
    user_dir = get_user_temp_dir(os.getcwd(), UID)
    generated_dir = user_dir / "generated_images"
    native_file = _assert_file_entries(
        native_files,
        expected_bytes=expected_bytes,
        expected_dir=generated_dir,
        expected_mime_type="image/png",
        expected_suffix="png",
    )
    native_path = send_file_to_user_module.resolve_file_token(native_file["id"])

    async def fake_generate_image(**_kwargs: Any) -> dict[str, Any]:
        return {
            "created": 1,
            "data": [{"b64_json": image_data}],
            "model": "background-image-model",
        }

    monkeypatch.setattr(ImageGenerationClient, "generate_image", fake_generate_image)

    task_arguments = {
        "prompt": "Background quota image",
        "size": "1024x1024",
        "quality": "auto",
    }
    async with session_factory() as db:
        profile = await db.get(Profile, PROFILE_ID)
        channel = await db.get(ModelChannel, CHANNEL_ID)
        assert profile is not None
        assert channel is not None
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
        profile_configs["tool"]["enabled_tools"] = ["generate_image"]
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
        task = BackgroundTask(
            uid=UID,
            session_id=session_b,
            profile_id=PROFILE_ID,
            tool_call_id="mixed-quota-generate-image",
            tool_name="generate_image",
            status=BackgroundTaskStatus.PENDING,
            arguments=task_arguments,
            auto_reply=False,
            reply_status=BackgroundTaskReplyStatus.NONE,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        assert task.id is not None
        task_id = task.id

    await runner_module.run_background_task(task_id, worker_id="mixed-quota-worker")

    async with session_factory() as db:
        completed_task = await db.get(BackgroundTask, task_id)
        assert completed_task is not None
        assert completed_task.status == BackgroundTaskStatus.SUCCEEDED
        assert completed_task.attempt_count == 1
        assert completed_task.result is not None
        background_path = Path(completed_task.result["content"]["send_file_to_user"]["files"][0]["path"])

    assert background_path.parent == generated_dir.resolve()
    assert background_path.read_bytes() == expected_bytes
    assert background_path != native_path

    deep_result_path = user_dir / "tool_results" / "deep" / "result.bin"
    deep_result_path.parent.mkdir(parents=True, exist_ok=True)
    deep_result_path.write_bytes(expected_bytes)

    upload_uid = UID

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as db:
            yield db

    async def override_get_current_user() -> User:
        return User(uid=upload_uid, username=f"{upload_uid}-user")

    app = FastAPI()
    register_handlers(app)
    app.include_router(files_module.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        async def upload_fixture(session_id: str | None) -> Path:
            request_data = {} if session_id is None else {"session_id": session_id}
            upload_response = await client.post(
                "/api/v1/upload",
                files={"file": ("fixture.png", expected_bytes, "image/png")},
                data=request_data,
            )
            assert upload_response.status_code == 200
            upload_path = Path(upload_response.json()["path"])
            expected_upload_dir = get_user_temp_dir(os.getcwd(), upload_uid)
            assert upload_path.parent == expected_upload_dir.resolve()
            assert upload_path.read_bytes() == expected_bytes
            return upload_path

        upload_paths = [await upload_fixture(session_a), await upload_fixture(session_b)]
        upload_uid = other_uid
        other_upload_paths = [await upload_fixture(None), await upload_fixture(None)]

        assert all(path.parent == user_dir.resolve() for path in upload_paths)
        other_user_dir = get_user_temp_dir(os.getcwd(), other_uid)
        assert all(path.parent == other_user_dir.resolve() for path in other_upload_paths)
        assert len([path for path in generated_dir.iterdir() if path.is_file()]) == 2
        assert len([path for path in user_dir.iterdir() if path.is_file()]) == 2
        assert deep_result_path.read_bytes() == expected_bytes

        temp_entries = list(temp_root.iterdir())
        assert all(path.is_dir() for path in temp_entries)
        assert {path.name for path in temp_entries} == {user_dir.name, other_user_dir.name}
        assert not get_user_temp_dir(os.getcwd(), session_a).exists()
        assert not get_user_temp_dir(os.getcwd(), session_b).exists()

        first_user_files = [native_path, background_path, *upload_paths, deep_result_path]
        assert len(first_user_files) == 5
        assert all(path.stat().st_size == len(expected_bytes) for path in first_user_files)
        assert sum(path.stat().st_size for path in generated_dir.iterdir() if path.is_file()) == 2 * len(expected_bytes)
        assert sum(path.stat().st_size for path in user_dir.iterdir() if path.is_file()) == 2 * len(expected_bytes)
        assert deep_result_path.stat().st_size == len(expected_bytes)
        assert {path.resolve() for path in user_dir.rglob("*") if path.is_file()} == {path.resolve() for path in first_user_files}

        for index, path in enumerate(first_user_files):
            timestamp = 1_000_000.0 + index
            os.utime(path, (timestamp, timestamp))

        native_download_before = await client.get(native_file["download_url"])
        assert native_download_before.status_code == 200
        assert native_download_before.content == expected_bytes

        max_size_bytes = 4 * len(expected_bytes)
        deleted_count, current_size = tasks_module._cleanup_temp_dir_by_size(max_size_bytes)
        assert deleted_count == 1
        assert current_size == max_size_bytes
        assert not native_path.exists()
        assert all(path.exists() for path in [background_path, *upload_paths, deep_result_path])
        assert all(path.exists() for path in other_upload_paths)

        remaining_first_user_files = [path for path in user_dir.rglob("*") if path.is_file()]
        other_user_files = [path for path in other_user_dir.rglob("*") if path.is_file()]
        assert len(remaining_first_user_files) == 4
        assert sum(path.stat().st_size for path in remaining_first_user_files) == max_size_bytes
        assert len(other_user_files) == 2
        assert sum(path.stat().st_size for path in other_user_files) == 2 * len(expected_bytes)
        project_temp_size = sum(path.stat().st_size for path in temp_root.rglob("*") if path.is_file())
        assert project_temp_size == max_size_bytes + 2 * len(expected_bytes)
        assert project_temp_size > max_size_bytes

        native_download_after = await client.get(native_file["download_url"])
        assert native_download_after.status_code == 404
        for path in [background_path, *upload_paths]:
            retained_download = await client.get("/api/v1/download", params={"path": str(path)})
            assert retained_download.status_code == 200
            assert retained_download.content == expected_bytes

        deleted_count, current_size = tasks_module._cleanup_temp_dir_by_size(max_size_bytes)
        assert deleted_count == 0
        assert current_size == max_size_bytes


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
    batch_image_workflow: SimpleNamespace,
    round_two_count: int,
    completion_order: tuple[int, ...],
) -> None:
    workflow = batch_image_workflow
    round_one_prompts = [f"round-one image {index}" for index in range(1, 6)]
    round_two_prompts = [f"round-two image {index}" for index in range(1, round_two_count + 1)]
    all_prompts = round_one_prompts + round_two_prompts
    total_task_count = 5 + round_two_count

    def build_tool_calls(prefix: str, prompts: list[str]) -> list[InternalToolCall]:
        return [
            InternalToolCall(
                id=f"{prefix}-image-{index}",
                name="generate_image",
                arguments={"prompt": prompt},
            )
            for index, prompt in enumerate(prompts, start=1)
        ]

    def input_item_maps(payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        input_items = payload.get("input")
        assert isinstance(input_items, list)
        function_calls: dict[str, dict[str, Any]] = {}
        function_outputs: dict[str, dict[str, Any]] = {}
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id")
                assert isinstance(call_id, str) and call_id
                assert call_id not in function_calls
                function_calls[call_id] = copy.deepcopy(item)
            elif item.get("type") == "function_call_output":
                call_id = item.get("call_id") or item.get("id")
                assert isinstance(call_id, str) and call_id
                assert call_id not in function_outputs
                function_outputs[call_id] = copy.deepcopy(item)
        assert set(function_calls) == set(function_outputs)
        return function_calls, function_outputs

    async def assert_persisted_state(completed_ids: set[int], started_ids: set[int], tasks: list[BackgroundTask]) -> None:
        async with workflow.factory() as db:
            result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == workflow.session_id).order_by(BackgroundTask.id.asc()))
            persisted_tasks = list(result.scalars().all())
        assert [task.id for task in persisted_tasks] == [task.id for task in tasks]
        persisted_by_id = {task.id: task for task in persisted_tasks}
        task_by_id = {task.id: task for task in tasks}
        assert all(isinstance(task_id, int) for task_id in persisted_by_id)
        expected_running_ids = started_ids - completed_ids
        expected_pending_ids = set(task_by_id) - started_ids
        assert {task.id for task in persisted_tasks if task.status == BackgroundTaskStatus.RUNNING} == expected_running_ids
        assert {task.id for task in persisted_tasks if task.status == BackgroundTaskStatus.PENDING} == expected_pending_ids
        assert {task.id for task in persisted_tasks if task.status == BackgroundTaskStatus.SUCCEEDED} == completed_ids
        assert len(expected_running_ids) <= 2
        assert workflow.active_prompts == {task_by_id[task_id].arguments["prompt"] for task_id in expected_running_ids}
        for task_id in completed_ids:
            task = persisted_by_id[task_id]
            assert task.attempt_count == 1
            assert task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        for task_id in set(task_by_id):
            assert persisted_by_id[task_id].attempt_count == (1 if task_id in started_ids else 0)

    round_one_response = await workflow.dispatch_round(
        build_tool_calls("round-one", round_one_prompts),
        "round-one batch request",
    )
    assert round_one_response["choices"][0]["message"]["content"] == "Image generation tasks accepted."
    assert len(workflow.gateway_requests) == 2
    round_one_payload = copy.deepcopy(workflow.gateway_requests[-1])
    round_one_calls, round_one_outputs = input_item_maps(round_one_payload)
    assert len(round_one_calls) == 5
    assert set(round_one_calls) == set(round_one_outputs)
    serialized_round_one = json.dumps(round_one_payload, ensure_ascii=False)
    assert "round-one batch request" in serialized_round_one
    assert "round-two batch request" not in serialized_round_one

    round_one_tasks = await workflow.tasks()
    assert len(round_one_tasks) == 5
    assert [task.arguments["prompt"] for task in round_one_tasks] == round_one_prompts
    assert all(task.status == BackgroundTaskStatus.PENDING for task in round_one_tasks)
    assert all(task.attempt_count == 0 for task in round_one_tasks)
    round_one_task_ids = [task.id for task in round_one_tasks]
    assert all(isinstance(task_id, int) for task_id in round_one_task_ids)
    round_one_submission_snapshots: dict[int, list[dict[str, Any]]] = {}
    round_one_queued_payloads: dict[str, dict[str, Any]] = {}
    for task in round_one_tasks:
        assert task.id is not None
        assert task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert isinstance(task.extra, dict)
        submission_context = task.extra.get("submission_context")
        assert isinstance(submission_context, list)
        round_one_submission_snapshots[task.id] = copy.deepcopy(submission_context)
        assert isinstance(task.tool_call_id, str)
        assert task.tool_call_id in round_one_calls
        call_item = round_one_calls[task.tool_call_id]
        assert call_item["name"] == "generate_image"
        assert json.loads(call_item["arguments"]) == task.arguments
        queued_item = round_one_outputs[task.tool_call_id]
        queued_payload = json.loads(queued_item["output"])
        assert queued_payload["status"] == "queued"
        assert queued_payload["tool_name"] == "generate_image"
        assert queued_payload["task_id"] == task.id
        round_one_queued_payloads[task.tool_call_id] = copy.deepcopy(queued_payload)

    await workflow.manager.schedule(workflow.profile)
    initial_started_prompts = [await asyncio.wait_for(workflow.started_queue.get(), timeout=10) for _ in range(2)]
    assert len(set(initial_started_prompts)) == 2
    initial_started_ids = {task.id for task in round_one_tasks if task.arguments["prompt"] in initial_started_prompts}
    assert initial_started_ids == set(round_one_task_ids[:2])
    assert set(workflow.gates) == set(initial_started_prompts)
    assert all(not workflow.gates[prompt].is_set() for prompt in initial_started_prompts)
    assert len(workflow.image_requests) == 2
    await assert_persisted_state(set(), initial_started_ids, round_one_tasks)

    round_two_response = await workflow.dispatch_round(
        build_tool_calls("round-two", round_two_prompts),
        "round-two batch request",
    )
    assert round_two_response["choices"][0]["message"]["content"] == "Image generation tasks accepted."
    assert len(workflow.gateway_requests) == 4
    round_two_payload = copy.deepcopy(workflow.gateway_requests[-1])
    round_two_calls, round_two_outputs = input_item_maps(round_two_payload)
    assert len(round_two_calls) == total_task_count
    assert set(round_two_calls) == set(round_two_outputs)
    assert set(round_one_calls).issubset(round_two_calls)
    serialized_round_two = json.dumps(round_two_payload, ensure_ascii=False)
    assert "round-one batch request" in serialized_round_two
    assert "round-two batch request" in serialized_round_two

    tasks = await workflow.tasks()
    assert len(tasks) == total_task_count
    assert [task.id for task in tasks[:5]] == round_one_task_ids
    assert [task.arguments["prompt"] for task in tasks] == all_prompts
    round_two_tasks = tasks[5:]
    assert len(round_two_tasks) == round_two_count
    round_two_task_ids = [task.id for task in round_two_tasks]
    assert all(isinstance(task_id, int) for task_id in round_two_task_ids)
    assert not set(round_one_task_ids) & set(round_two_task_ids)
    round_two_submission_snapshots: dict[int, list[dict[str, Any]]] = {}
    round_two_call_ids: set[str] = set()
    for task in tasks:
        assert task.id is not None
        assert isinstance(task.tool_call_id, str)
        assert task.tool_call_id in round_two_calls
        call_item = round_two_calls[task.tool_call_id]
        assert call_item["name"] == "generate_image"
        assert json.loads(call_item["arguments"]) == task.arguments
        output_item = round_two_outputs[task.tool_call_id]
        output_payload = json.loads(output_item["output"])
        assert output_payload["status"] == "queued"
        assert output_payload["tool_name"] == "generate_image"
        assert output_payload["task_id"] == task.id
        assert isinstance(task.extra, dict)
        submission_context = task.extra.get("submission_context")
        assert isinstance(submission_context, list)
        if task.id in round_one_submission_snapshots:
            assert task.extra.get("submission_context") == round_one_submission_snapshots[task.id]
            assert output_payload == round_one_queued_payloads[task.tool_call_id]
        else:
            round_two_call_ids.add(task.tool_call_id)
            round_two_submission_snapshots[task.id] = copy.deepcopy(submission_context)
    assert len(round_two_call_ids) == round_two_count
    assert round_two_call_ids == {task.tool_call_id for task in round_two_tasks if task.tool_call_id is not None}
    await workflow.manager.schedule(workflow.profile)
    assert len(workflow.image_requests) == 2
    assert workflow.active_prompts == set(initial_started_prompts)
    assert workflow.started_queue.empty()
    await assert_persisted_state(set(), initial_started_ids, tasks)

    async with workflow.factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == workflow.session_id).order_by(Message.id.asc()))
        source_messages = {message.id: message.content for message in result.scalars().all() if message.id is not None}

    prompt_by_task_id = {task.id: task.arguments["prompt"] for task in tasks}
    completed_ids: set[int] = set()
    started_ids = set(initial_started_ids)
    generated_paths: set[Path] = set()
    completed_work_ids: dict[int, int] = {}
    result_message_ids: set[int] = set()
    summary_payloads: list[dict[str, Any]] = []

    for task_index in completion_order:
        task = tasks[task_index]
        assert task.id is not None
        assert task.id in started_ids
        prompt = prompt_by_task_id[task.id]
        assert prompt in workflow.active_prompts
        gate = workflow.gates.get(prompt)
        assert gate is not None
        assert not gate.is_set()
        run_handle = workflow.run_handles.get(task.id)
        assert run_handle is not None
        assert not run_handle.done()
        gate.set()
        await asyncio.wait_for(asyncio.shield(run_handle), timeout=10)

        async with workflow.factory() as db:
            completed_task = await db.get(BackgroundTask, task.id)
            assert completed_task is not None
            assert completed_task.status == BackgroundTaskStatus.SUCCEEDED
            assert completed_task.attempt_count == 1
            assert completed_task.result is not None
            completed_result = copy.deepcopy(completed_task.result)
            assert completed_task.extra.get("submission_context") == (round_one_submission_snapshots.get(task.id, round_two_submission_snapshots.get(task.id)))

        assert completed_result["status"] == "succeeded"
        assert completed_result["tool_name"] == "generate_image"
        result_content = completed_result["content"]
        assert result_content["status"] == "success"
        files = result_content["send_file_to_user"]["files"]
        assert isinstance(files, list)
        assert len(files) == 1
        generated_path = Path(files[0]["path"])
        expected_dir = (get_user_temp_dir(os.getcwd(), UID) / "generated_images").resolve()
        assert generated_path.parent == expected_dir
        assert generated_path.suffix.lower() == ".png"
        assert files[0]["mime_type"] == "image/png"
        assert generated_path.read_bytes() == workflow.expected_bytes
        assert generated_path.resolve() not in generated_paths
        generated_paths.add(generated_path.resolve())
        assert workflow.image_data not in json.dumps(completed_result, ensure_ascii=False)

        gateway_request_start = len(workflow.gateway_requests)
        event_start = len(workflow.events)
        work, replied_task, result_message = await workflow.summarize(task.id)
        assert work.status == SessionReplyWorkStatus.SUCCEEDED
        assert work.result_message_id == result_message.id
        assert replied_task.id == task.id
        assert replied_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
        assert replied_task.extra.get("submission_context") == (round_one_submission_snapshots.get(task.id, round_two_submission_snapshots.get(task.id)))
        assert result_message.id is not None
        assert result_message.role == MessageRole.ASSISTANT
        assert result_message.type == MessageType.TEXT
        assert result_message.content == f"Completed: {prompt}"
        assert work.id is not None
        completed_work_ids[task.id] = work.id
        result_message_ids.add(result_message.id)

        new_events = workflow.events[event_start:]
        assert len(new_events) == 1
        event_payload = new_events[0]
        assert event_payload["type"] == "proactive_reply"
        assert event_payload["task_id"] == task.id
        assert event_payload["message_id"] == result_message.id
        assert event_payload["content"] == f"Completed: {prompt}"

        new_gateway_requests = workflow.gateway_requests[gateway_request_start:]
        assert len(new_gateway_requests) == 1
        summary_payload = new_gateway_requests[0]
        summary_payloads.append(copy.deepcopy(summary_payload))
        summary_calls, summary_outputs = input_item_maps(summary_payload)
        if task_index < 5:
            expected_call_ids = {task.tool_call_id}
            serialized_summary = json.dumps(summary_payload, ensure_ascii=False)
            assert "round-one batch request" in serialized_summary
            assert "round-two batch request" not in serialized_summary
        else:
            expected_call_ids = set(round_one_calls) | {task.tool_call_id}
            serialized_summary = json.dumps(summary_payload, ensure_ascii=False)
            assert "round-one batch request" in serialized_summary
            assert "round-two batch request" in serialized_summary
            assert not (set(round_two_call_ids) - {task.tool_call_id}) & set(summary_calls)
        assert set(summary_calls) == expected_call_ids
        assert set(summary_outputs) == expected_call_ids
        for call_id in expected_call_ids:
            call_item = summary_calls[call_id]
            assert call_item["name"] == "generate_image"
            expected_task = task if call_id == task.tool_call_id else next(candidate for candidate in round_one_tasks if candidate.tool_call_id == call_id)
            assert json.loads(call_item["arguments"]) == expected_task.arguments
            output_payload = json.loads(summary_outputs[call_id]["output"])
            if call_id == task.tool_call_id:
                assert output_payload == completed_result
            else:
                assert output_payload == round_one_queued_payloads[call_id]
        assert workflow.image_data not in json.dumps(summary_payload, ensure_ascii=False)

        completed_ids.add(task.id)
        await workflow.manager.schedule(workflow.profile)
        unstarted_tasks = [candidate for candidate in tasks if candidate.id not in started_ids]
        if unstarted_tasks:
            next_prompt = await asyncio.wait_for(workflow.started_queue.get(), timeout=10)
            assert next_prompt in {prompt_by_task_id[candidate.id] for candidate in unstarted_tasks}
            next_task = next(candidate for candidate in unstarted_tasks if prompt_by_task_id[candidate.id] == next_prompt)
            assert next_task.id is not None
            assert next_task.id not in started_ids
            started_ids.add(next_task.id)
        else:
            assert workflow.started_queue.empty()
        await assert_persisted_state(completed_ids, started_ids, tasks)

    assert completed_ids == {task.id for task in tasks}
    assert len(completed_work_ids) == total_task_count
    assert len(result_message_ids) == total_task_count
    assert [event["task_id"] for event in workflow.events] == [tasks[index].id for index in completion_order]
    assert len(summary_payloads) == total_task_count
    assert len(workflow.gateway_requests) == 4 + total_task_count
    assert len(workflow.image_requests) == total_task_count
    assert Counter(request["prompt"] for request in workflow.image_requests) == Counter(all_prompts)
    assert all(request["n"] == 1 for request in workflow.image_requests)
    assert all(request["model_id"] == "background-image-model" for request in workflow.image_requests)
    assert all(request["protocol"] == "openai_image" for request in workflow.image_requests)
    assert workflow.peak_active == 2
    assert workflow.active_prompts == set()
    assert workflow.started_queue.empty()

    generated_dir = (get_user_temp_dir(os.getcwd(), UID) / "generated_images").resolve()
    generated_files = [path.resolve() for path in generated_dir.iterdir() if path.is_file() and not path.name.startswith(".")]
    assert len(generated_files) == total_task_count
    assert set(generated_files) == generated_paths
    assert all(path.suffix.lower() == ".png" and path.read_bytes() == workflow.expected_bytes for path in generated_files)

    async with workflow.factory() as db:
        result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == workflow.session_id).order_by(BackgroundTask.id.asc()))
        persisted_tasks = list(result.scalars().all())
        result = await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id.asc()))
        work_items = list(result.scalars().all())
        result = await db.execute(select(Message).where(Message.session_id == workflow.session_id).order_by(Message.id.asc()))
        messages = list(result.scalars().all())
    assert len(persisted_tasks) == total_task_count
    assert all(task.status == BackgroundTaskStatus.SUCCEEDED for task in persisted_tasks)
    assert all(task.attempt_count == 1 for task in persisted_tasks)
    assert all(task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED for task in persisted_tasks)
    for task in persisted_tasks:
        assert task.id is not None
        expected_snapshot = round_one_submission_snapshots.get(task.id, round_two_submission_snapshots.get(task.id))
        assert task.extra.get("submission_context") == expected_snapshot
    assert len(work_items) == total_task_count
    assert all(work_item.status == SessionReplyWorkStatus.SUCCEEDED for work_item in work_items)
    assert {work_item.id for work_item in work_items} == set(completed_work_ids.values())
    assert {work_item.source_id for work_item in work_items} == {str(task.id) for task in tasks}
    result_messages = [message for message in messages if message.id in result_message_ids]
    assert len(result_messages) == total_task_count
    assert {message.content for message in result_messages} == {f"Completed: {prompt}" for prompt in all_prompts}
    for message_id, content in source_messages.items():
        source_message = next(message for message in messages if message.id == message_id)
        assert source_message.content == content

    work_snapshot = {work_item.id: (work_item.status, work_item.work_type, work_item.source_id, work_item.result_message_id) for work_item in work_items}
    message_snapshot = {message.id: (message.role, message.type, message.content) for message in messages if message.id is not None}
    gateway_count = len(workflow.gateway_requests)
    event_count = len(workflow.events)
    for task in tasks:
        assert task.id is not None
        await reply_trigger_module.trigger_background_task_reply(task.id)
        await executor_lifecycle_module.execute_session_reply_work(
            completed_work_ids[task.id],
            "batch-image-summary-worker",
        )

    assert len(workflow.gateway_requests) == gateway_count
    assert len(workflow.events) == event_count
    async with workflow.factory() as db:
        result = await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id.asc()))
        repeated_work_items = list(result.scalars().all())
        result = await db.execute(select(Message).where(Message.session_id == workflow.session_id).order_by(Message.id.asc()))
        repeated_messages = list(result.scalars().all())
        leftover = await reply_work_item_module.session_reply_work_item_crud.claim_next(
            db,
            worker_id="batch-image-summary-worker",
            lease_seconds=300,
        )
    repeated_work_snapshot = {work_item.id: (work_item.status, work_item.work_type, work_item.source_id, work_item.result_message_id) for work_item in repeated_work_items}
    repeated_message_snapshot = {message.id: (message.role, message.type, message.content) for message in repeated_messages if message.id is not None}
    assert repeated_work_snapshot == work_snapshot
    assert repeated_message_snapshot == message_snapshot
    assert leftover is None
