import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.audit.persistence as audit_persistence_module
import app.core.audit.service as audit_service_module
import app.core.background_tasks.external_work as external_work_module
import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.session_reply_queue.executor_replies as executor_replies_module
import app.core.tools.send_file_to_user as send_file_to_user_module
import app.models  # noqa: F401
from app.core.background_tasks.external_work import resume_external_work_for_task
from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    END_SESSION_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    MANAGE_TODO_TOOL_NAME,
    SEND_FILE_TO_USER_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_SUMMARY_PROMPT
from app.core.tools.scheduled_tasks import ManageScheduledTasksExecutor
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord, AuditRecordStatus, AuditToolConclusion, AuditToolDetail
from app.models.background_task import BackgroundTask, BackgroundTaskReplyMode, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.llm.client import LLMClient

UID = "scheduled-owner"
SESSION_ID = "scheduled-workflow"
PROFILE_ID = 1
MODEL_ID = "scheduled-model"
FINAL_REPLY = "管理操作已完成"
USAGE = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


@pytest_asyncio.fixture
async def session_factory(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(foreign_keys=True, name="scheduled-task-workflow.sqlite3") as factory:
        yield factory


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession]:
    async with session_factory() as db:
        yield db


async def _create_due_run_once_scheduled_task(db: AsyncSession) -> ScheduledTask:
    profile = Profile(
        id=PROFILE_ID,
        uid=UID,
        name="scheduled-task-run-once-profile",
        configs=ProfileConfig.model_validate({}).model_dump(),
    )
    db.add_all([User(uid=UID, username=UID), profile])
    await db.flush()
    db.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    await db.flush()

    scheduled_task = ScheduledTask(
        name="One-time reminder",
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        message="Run this reminder once",
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db.add(scheduled_task)
    await db.commit()
    await db.refresh(scheduled_task)
    return scheduled_task


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("run_once", [False, True], ids=["recurring", "one-time"])
async def test_llm_workflow_creates_lists_and_deletes_scheduled_task(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    run_once: bool,
) -> None:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-workflow-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    },
                    {
                        "model_id": "scheduled-image-model",
                        "usage": "IMAGE_GENERATION",
                        "protocol": "OPENAI_IMAGE",
                        "size": "1024x1024",
                        "quality": "auto",
                    },
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-workflow-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.flush()

    persisted_user_message = Message(
        session_id=SESSION_ID,
        uid=UID,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="请设置提醒并确认管理完成",
        profile_id=PROFILE_ID,
        is_processed=False,
    )
    db_session.add(persisted_user_message)
    await db_session.commit()
    await db_session.refresh(persisted_user_message)
    persisted_initial_message = InternalMessage(
        id=persisted_user_message.id,
        role=MessageRole.USER,
        content=persisted_user_message.content,
        created_at=persisted_user_message.created_at.timestamp(),
    )

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)

    calls: list[dict[str, Any]] = []
    observed_results: list[dict[str, Any]] = []

    def tool_result(messages: list[InternalMessage], call_id: str) -> dict[str, Any]:
        for message in reversed(messages):
            if message.role == MessageRole.TOOL and message.tool_call_id == call_id:
                assert isinstance(message.content, str)
                result = json.loads(message.content)
                assert isinstance(result, dict)
                return result
        raise AssertionError(f"No tool result found for {call_id}")

    def response_for(kwargs: dict[str, Any]) -> InternalResponse:
        calls.append(kwargs)
        tools = kwargs["tools"]
        scheduled_tool = next(tool for tool in tools if tool["function"]["name"] == MANAGE_SCHEDULED_TASKS_TOOL_NAME)
        operation_schema = scheduled_tool["function"]["parameters"]["properties"]["operation"]
        assert {"create", "list", "delete"} <= set(operation_schema["enum"])

        turn = len(calls)
        if turn == 1:
            create_arguments = {
                "operation": "create",
                "name": "Pipeline reminder",
                "message": "Review deployment status",
                "interval_seconds": 60,
            }
            if run_once:
                create_arguments["run_once"] = True
            tool_call = InternalToolCall(
                id="scheduled-create-call",
                name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                arguments=create_arguments,
            )
        elif turn == 2:
            created = tool_result(kwargs["messages"], "scheduled-create-call")
            assert created["status"] == "success"
            assert created["operation"] == "create"
            observed_results.append(created)
            tool_call = InternalToolCall(
                id="scheduled-list-call",
                name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                arguments={"operation": "list"},
            )
        elif turn == 3:
            listed = tool_result(kwargs["messages"], "scheduled-list-call")
            assert listed["status"] == "success"
            assert listed["operation"] == "list"
            observed_results.append(listed)
            assert len(listed["tasks"]) == 1
            task_id = listed["tasks"][0]["id"]
            tool_call = InternalToolCall(
                id="scheduled-delete-call",
                name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                arguments={"operation": "delete", "task_id": task_id},
            )
        elif turn == 4:
            deleted = tool_result(kwargs["messages"], "scheduled-delete-call")
            assert deleted["status"] == "success"
            assert deleted["operation"] == "delete"
            observed_results.append(deleted)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=FINAL_REPLY),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        else:
            raise AssertionError(f"Unexpected LLM request {turn}")

        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="tool_calls",
        )

    async def generate(**kwargs: Any) -> InternalResponse:
        assert not stream
        return response_for(kwargs)

    async def generate_with_stream_callback(**kwargs: Any) -> InternalResponse:
        assert stream
        response = response_for(kwargs)
        if response.message.content:
            await kwargs["on_content"](response.message.content)
        return response

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "generate_with_stream_callback",
        generate_with_stream_callback,
    )

    if stream:
        result = [
            event
            async for event in ChatDispatcher.dispatch_stream(
                db=db_session,
                message=persisted_user_message.content,
                uid=UID,
                session_id=SESSION_ID,
                request_id="scheduled-task-workflow-request",
                session_source="ws",
                persisted_initial_message=persisted_initial_message,
                frozen_user_message_ids=[persisted_initial_message.id],
                persisted_profile_id=PROFILE_ID,
                show_tool_calls=True,
            )
        ]
        done_events = [event for event in result if event.get("type") == "done"]
        assert len(done_events) == 1
        response = done_events[0]["response"]
    else:
        response = await ChatDispatcher.dispatch(
            db=db_session,
            message=persisted_user_message.content,
            uid=UID,
            session_id=SESSION_ID,
            session_source="http",
            persisted_initial_message=persisted_initial_message,
            frozen_user_message_ids=[persisted_initial_message.id],
            persisted_profile_id=PROFILE_ID,
            show_tool_calls=True,
        )

    assert len(calls) == 4
    assert response["choices"][0]["message"]["content"] == FINAL_REPLY
    assert [result["operation"] for result in observed_results] == ["create", "list", "delete"]

    created_task = observed_results[0]["task"]
    assert (created_task["uid"], created_task["session_id"], created_task["profile_id"]) == (UID, SESSION_ID, PROFILE_ID)
    assert created_task["run_once"] is run_once
    listed_tasks = observed_results[1]["tasks"]
    assert len(listed_tasks) == 1
    listed_task = listed_tasks[0]
    assert listed_task["id"] == created_task["id"]
    assert (listed_task["uid"], listed_task["session_id"], listed_task["profile_id"]) == (UID, SESSION_ID, PROFILE_ID)
    assert listed_task["run_once"] is run_once
    assert observed_results[2]["task_id"] == created_task["id"]

    async with session_factory() as verification_db:
        remaining_tasks = list((await verification_db.execute(select(ScheduledTask))).scalars().all())
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id.asc()))).scalars().all())

    assert remaining_tasks == []
    tool_call_messages = [message for message in persisted_messages if message.type == MessageType.TOOL_CALL]
    tool_result_messages = [message for message in persisted_messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_messages = [message for message in persisted_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_call_messages) == 3
    assert all(message.role == MessageRole.ASSISTANT for message in tool_call_messages)
    assert len(tool_result_messages) == 3
    assert all(message.role == MessageRole.TOOL for message in tool_result_messages)
    assert len(assistant_text_messages) == 1
    assert assistant_text_messages[0].content == FINAL_REPLY
    assert persisted_messages[-1].role == MessageRole.ASSISTANT
    assert persisted_messages[-1].type == MessageType.TEXT


@pytest.mark.asyncio
async def test_scheduled_task_dispatch_enqueues_once_and_delete_preserves_queued_work(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(
        id=PROFILE_ID,
        uid=UID,
        name="scheduled-task-dispatch-profile",
        configs=ProfileConfig.model_validate({}).model_dump(),
    )
    db_session.add_all([User(uid=UID, username=UID), profile])
    await db_session.flush()
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    await db_session.commit()

    executor = ManageScheduledTasksExecutor(project_root=".", uid=UID)
    executor.set_runtime_context(db=db_session, profile=profile, session_id=SESSION_ID)
    created = json.loads(
        await executor.execute(
            "create",
            name="Pipeline reminder",
            message="Review deployment status",
            interval_seconds=60,
        )
    )
    assert created["status"] == "success"
    task_id = created["task"]["id"]

    scheduled_task = await db_session.get(ScheduledTask, task_id)
    assert scheduled_task is not None
    scheduled_task.next_run_at = get_local_time() - timedelta(seconds=1)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    due_time = scheduled_task.next_run_at

    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(task_id)
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                        Message.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.run_count == 1
        assert persisted_task.last_run_at is not None
        assert persisted_task.last_run_at >= due_time
        assert persisted_task.next_run_at > persisted_task.last_run_at
        assert persisted_task.last_message_id is not None
        assert len(trigger_messages) == 1
        trigger_message = trigger_messages[0]
        assert trigger_message.id == persisted_task.last_message_id
        assert trigger_message.uid == UID
        assert trigger_message.session_id == SESSION_ID
        assert trigger_message.profile_id == PROFILE_ID
        assert trigger_message.content == "Review deployment status"
        assert trigger_message.role == MessageRole.USER
        assert trigger_message.is_processed is True

        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_message.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(work_items) == 1
        queued_work = work_items[0]
        assert queued_work.uid == UID
        assert queued_work.session_id == SESSION_ID
        assert queued_work.profile_id == PROFILE_ID
        assert queued_work.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY
        assert queued_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert isinstance(queued_work.execution_state.get("frozen_context_messages"), list)

    deleted = json.loads(await executor.execute("delete", task_id=task_id))
    assert deleted["status"] == "success"
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        assert await reader.get(ScheduledTask, task_id) is None
        remaining_triggers = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                        Message.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        remaining_work = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_message.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining_triggers) == 1
    assert len(remaining_work) == 1
    assert remaining_work[0].status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_run_once_scheduled_task_is_not_redispatched_from_stale_snapshot_or_later_poll(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    assert scheduled_task.id is not None
    task_id = scheduled_task.id
    due_time = scheduled_task.next_run_at

    async with session_factory() as snapshot_db:
        stale_snapshot = await snapshot_db.get(ScheduledTask, task_id)
        assert stale_snapshot is not None
        assert stale_snapshot.status == ScheduledTaskStatus.ENABLED
        snapshot_db.expunge(stale_snapshot)

    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(task_id)

    async with session_factory() as stale_db:
        await scheduler._dispatch_one_with_db(stale_db, stale_snapshot)

    monkeypatch.setattr(
        scheduler_module,
        "get_local_time",
        lambda: due_time + timedelta(seconds=scheduled_task.interval_seconds + 1),
    )
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.status == ScheduledTaskStatus.DISABLED
        assert persisted_task.run_count == 1
        assert persisted_task.next_run_at == due_time
        assert persisted_task.last_message_id is not None
        assert len(trigger_messages) == 1

        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_messages[0].id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(work_items) == 1
        assert work_items[0].work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY
        assert work_items[0].status == SessionReplyWorkStatus.READY_FOR_LLM
        assert work_items[0].uid == UID
        assert work_items[0].session_id == SESSION_ID
        assert work_items[0].profile_id == PROFILE_ID
        assert all(task.id != task_id for task in await scheduled_task_crud.list_due(reader))


@pytest.mark.asyncio
async def test_run_once_scheduled_task_enqueue_failure_rolls_back_before_retry(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    assert scheduled_task.id is not None
    task_id = scheduled_task.id
    due_time = scheduled_task.next_run_at

    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    scheduler = scheduler_module.ScheduledTaskScheduler()
    queue_manager = scheduler_module.session_reply_queue_manager
    original_enqueue = queue_manager.enqueue_scheduled_summary

    async def enqueue_then_fail(*args: Any, **kwargs: Any) -> None:
        await original_enqueue(*args, **kwargs)
        raise RuntimeError("simulated scheduled summary enqueue failure")

    monkeypatch.setattr(queue_manager, "enqueue_scheduled_summary", enqueue_then_fail)
    with pytest.raises(RuntimeError, match="simulated scheduled summary enqueue failure"):
        await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                    )
                )
            )
            .scalars()
            .all()
        )
        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.status == ScheduledTaskStatus.ENABLED
        assert persisted_task.run_count == 0
        assert persisted_task.last_run_at is None
        assert persisted_task.last_message_id is None
        assert persisted_task.next_run_at == due_time
        assert trigger_messages == []
        assert work_items == []

    monkeypatch.setattr(queue_manager, "enqueue_scheduled_summary", original_enqueue)
    await scheduler._dispatch_one(task_id)

    async with session_factory() as reader:
        persisted_task = await reader.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await reader.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert persisted_task is not None
        assert persisted_task.status == ScheduledTaskStatus.DISABLED
        assert persisted_task.run_count == 1
        assert persisted_task.last_message_id is not None
        assert len(trigger_messages) == 1

        work_items = list(
            (
                await reader.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.source_id == str(trigger_messages[0].id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(work_items) == 1
        assert work_items[0].work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY
        assert work_items[0].status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "run_once"),
    [("create", False), ("create", True), ("delete", False)],
    ids=["create-recurring", "create-one-time", "delete"],
)
async def test_scheduled_task_mutation_waits_for_audit_confirmation(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    run_once: bool,
) -> None:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["security"].update({"audit_channel_id": 1, "audit_model_id": MODEL_ID, "audit_threshold": 5})
    profile = Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-audit-profile", configs=configs)
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-audit-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            profile,
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.flush()

    scheduled_task_snapshot = None
    if operation == "delete":
        protected_task = ScheduledTask(
            id=1,
            name="Protected task",
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            message="Keep this scheduled request",
            interval_seconds=60,
            next_run_at=get_local_time() + timedelta(seconds=60),
        )
        db_session.add(protected_task)
        await db_session.flush()
        await db_session.refresh(protected_task)
        scheduled_task_snapshot = protected_task.model_dump(mode="json")

    persisted_user_message = Message(
        session_id=SESSION_ID,
        uid=UID,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="管理这个计划任务",
        profile_id=PROFILE_ID,
        is_processed=False,
    )
    db_session.add(persisted_user_message)
    await db_session.commit()
    await db_session.refresh(persisted_user_message)
    persisted_initial_message = InternalMessage(
        id=persisted_user_message.id,
        role=MessageRole.USER,
        content=persisted_user_message.content,
        created_at=persisted_user_message.created_at.timestamp(),
    )

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        audit_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    async def generate(**kwargs: Any) -> InternalResponse:
        prompt = kwargs["messages"][0].content
        if prompt == AUDIT_BATCH_PROMPT:
            content = json.dumps(
                {
                    "results": [
                        {
                            "tool_call_id": "scheduled-audited-call",
                            "score": 9,
                            "deny": False,
                            "reason": "Persistent scheduled action requires confirmation",
                            "file_checks": [],
                        }
                    ]
                }
            )
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        if prompt == AUDIT_SUMMARY_PROMPT.format(audit_report_language="zh"):
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="计划任务操作将在用户确认后执行。",
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )

        if operation == "create":
            arguments = {
                "operation": "create",
                "name": "Pipeline reminder",
                "message": "Review deployment status",
                "interval_seconds": 60,
            }
            if run_once:
                arguments["run_once"] = True
        else:
            arguments = {"operation": "delete", "task_id": 1}
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="scheduled-audited-call",
                        name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                        arguments=arguments,
                    )
                ],
            ),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    response = await ChatDispatcher.dispatch(
        db=db_session,
        message=persisted_user_message.content,
        uid=UID,
        session_id=SESSION_ID,
        session_source="http",
        persisted_initial_message=persisted_initial_message,
        frozen_user_message_ids=[persisted_initial_message.id],
        persisted_profile_id=PROFILE_ID,
        show_tool_calls=True,
    )

    confirmation_card = json.loads(response["choices"][0]["message"]["content"])
    assert confirmation_card["type"] == "audit_confirmation"
    assert confirmation_card["status"] == AuditRecordStatus.PENDING

    async with session_factory() as verification_db:
        audit_records = list((await verification_db.execute(select(AuditRecord).where(AuditRecord.uid == UID, AuditRecord.session_id == SESSION_ID))).scalars().all())
        assert len(audit_records) == 1
        audit_record = audit_records[0]
        assert audit_record.status == AuditRecordStatus.PENDING
        assert audit_record.source == "http"
        assert audit_record.source_assistant_message_id is not None

        details = list((await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record.id))).scalars().all())
        assert len(details) == 1
        detail = details[0]
        assert detail.tool_name == MANAGE_SCHEDULED_TASKS_TOOL_NAME
        assert detail.original_tool_call_id == "scheduled-audited-call"
        assert detail.conclusion == AuditToolConclusion.PENDING
        arguments_summary = json.loads(detail.arguments_summary)
        assert arguments_summary["operation"] == operation
        if operation == "create":
            assert arguments_summary.get("run_once", False) is run_once

        confirmation_messages = list(
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(confirmation_messages) == 1
        tool_results = list(
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.TOOL_RESULT,
                        Message.audit_tool_call_id == "scheduled-audited-call",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(tool_results) == 1
        assert tool_results[0].role == MessageRole.TOOL
        tool_result = InternalMessage.model_validate_json(tool_results[0].content or "")
        assert tool_result.tool_call_id == "scheduled-audited-call"
        assert json.loads(tool_result.content or "")["status"] == AuditRecordStatus.PENDING.value

        scheduled_tasks = list(
            (
                await verification_db.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.uid == UID,
                        ScheduledTask.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        if operation == "create":
            assert scheduled_tasks == []
        else:
            assert len(scheduled_tasks) == 1
            assert scheduled_tasks[0].model_dump(mode="json") == scheduled_task_snapshot


@pytest.mark.asyncio
async def test_scheduled_task_execution_supports_multiround_and_send_file(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_file = tmp_path / "scheduled_report.txt"
    report_file.write_text("scheduled task report content", encoding="utf-8")

    session_id = "scheduled-multiround-session"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["tool"]["enabled_tools"] = [SEND_FILE_TO_USER_TOOL_NAME, MANAGE_SCHEDULED_TASKS_TOOL_NAME]
    configs["tool"]["allowed_operation_dirs"] = [str(tmp_path)]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-multiround-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-multiround-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(send_file_to_user_module, "FILE_TOKEN_DIR", tmp_path / "file_download_tokens")
    monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"g" * 32)

    scheduled_task = ScheduledTask(
        name="Scheduled report task",
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        message="请生成报告并发送给用户",
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db_session.add(scheduled_task)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    assert scheduled_task.id is not None

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(scheduled_task.id)

    work_item = (
        (
            await db_session.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    SessionReplyWorkItem.session_id == session_id,
                )
            )
        )
        .scalars()
        .one()
    )

    worker_id = "worker-multiround"
    work_item.status = SessionReplyWorkStatus.RUNNING
    work_item.locked_by = worker_id
    work_item.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(work_item)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_item.id, worker_id)

    calls: list[dict[str, Any]] = []

    async def mock_create_chat_completion(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        turn = len(calls)
        if turn == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="call-send-file-1",
                            name=SEND_FILE_TO_USER_TOOL_NAME,
                            arguments={"files": [{"path": str(report_file.resolve())}]},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 2:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="call-end-session-2",
                            name=END_SESSION_TOOL_NAME,
                            arguments={"skip_reply": False},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 3:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="报告已生成并发送完成",
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected turn {turn}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "create_chat_completion",
        mock_create_chat_completion,
        raising=False,
    )
    monkeypatch.setattr(LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_create_chat_completion, raising=False)

    original_dispatch_interactive_work = executor_replies_module._dispatch_interactive_work
    captured_dispatch_kwargs: dict[str, Any] = {}

    async def spy_dispatch_interactive_work(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_dispatch_kwargs.update(kwargs)
        return await original_dispatch_interactive_work(*args, **kwargs)

    monkeypatch.setattr(executor_replies_module, "_dispatch_interactive_work", spy_dispatch_interactive_work)

    response = await executor_replies_module._execute_scheduled(db_session, work_item, worker_id)

    assert len(calls) == 3
    assert len(response["files"]) == 1
    sent_file_info = response["files"][0]
    assert sent_file_info["name"] == report_file.name
    assert "id" in sent_file_info

    async with session_factory() as verification_db:
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))).scalars().all())

    final_message = persisted_messages[-1]
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.type == MessageType.TEXT
    parsed_content = json.loads(final_message.content)
    assert parsed_content["type"] == "assistant_files"
    assert parsed_content["text"] == "报告已生成并发送完成"
    assert len(parsed_content["files"]) == 1
    assert parsed_content["files"][0]["id"] == sent_file_info["id"]

    assert captured_dispatch_kwargs.get("execution_scope") == END_SESSION_SCOPE_TASK
    control_messages = [msg for msg in persisted_messages if isinstance(msg.provider_metadata, dict) and msg.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME]
    assert len(control_messages) == 2
    assert all(msg.provider_metadata.get("execution_scope") == END_SESSION_SCOPE_TASK for msg in control_messages)


@pytest.mark.asyncio
async def test_scheduled_task_background_tool_resumes_same_work_with_task_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-background-resume-session"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["channel"]["image_generation_channel"] = {"rules": [{"channel_id": 1, "model_id": "scheduled-image-model", "priority": 1, "weight": 1}]}
    configs["tool"]["enabled_tools"] = [IMAGE_GENERATION_TOOL_NAME, MANAGE_SCHEDULED_TASKS_TOOL_NAME, MANAGE_TODO_TOOL_NAME]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-background-resume-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    },
                    {
                        "model_id": "scheduled-image-model",
                        "usage": "IMAGE_GENERATION",
                        "protocol": "OPENAI_IMAGE",
                        "size": "1024x1024",
                        "quality": "auto",
                    },
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-background-resume-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=False, max_turns=5))
    await db_session.flush()
    scheduled_task = ScheduledTask(
        name="Scheduled background resume",
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        message="run background shell and report",
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db_session.add(scheduled_task)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    assert scheduled_task.id is not None

    for module in (
        channel_cursor_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        external_work_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

    async def noop_send_session_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_send_session_event)

    calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        if len(calls) == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="scheduled-background-image",
                            name=IMAGE_GENERATION_TOOL_NAME,
                            arguments={"prompt": "scheduled background image"},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if len(calls) == 2:
            resumed_messages: list[InternalMessage] = kwargs["messages"]
            assert any(
                message.tool_calls
                and any(tool_call.id == "scheduled-background-image" for tool_call in message.tool_calls)
                for message in resumed_messages
            )
            assert any("image-1" in str(message.content) for message in resumed_messages if message.content is not None)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="scheduled background completed"),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected model call {len(calls)}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(scheduled_task.id)

    async with session_factory() as db:
        work = (
            await db.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.session_id == session_id,
                    SessionReplyWorkItem.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
                )
            )
        ).scalars().one()
        assert work.id is not None
        work_id = work.id
        claimed = await session_reply_work_item_crud.claim_next(db, worker_id="scheduled-background-worker-1", lease_seconds=300)
        assert claimed is not None and claimed.id == work_id

    await executor_lifecycle_module.execute_session_reply_work(work_id, "scheduled-background-worker-1")

    async with session_factory() as db:
        waiting = await db.get(SessionReplyWorkItem, work_id)
        assert waiting is not None
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())
        assert [item.id for item in works] == [work_id]
        task = (await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == session_id))).scalars().one()
        assert task.id is not None
        assert task.auto_reply is False
        assert task.extra["reply_mode"] == BackgroundTaskReplyMode.RESUME_ORIGIN.value
        task_id = task.id
        claimed_task = await background_task_crud.try_claim(db, task_id=task_id, worker_id="scheduled-background-task-worker", lease_seconds=300)
        assert claimed_task is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=task_id,
            worker_id="scheduled-background-task-worker",
            result={"status": "succeeded", "content": {"files": [{"id": "image-1", "name": "scheduled.png"}]}},
            auto_reply=False,
        )

    assert await resume_external_work_for_task(task_id) is True

    async with session_factory() as db:
        resumed = await db.get(SessionReplyWorkItem, work_id)
        assert resumed is not None
        assert resumed.status == SessionReplyWorkStatus.READY_FOR_LLM
        claimed = await session_reply_work_item_crud.claim_next(db, worker_id="scheduled-background-worker-2", lease_seconds=300)
        assert claimed is not None and claimed.id == work_id

    await executor_lifecycle_module.execute_session_reply_work(work_id, "scheduled-background-worker-2")

    async with session_factory() as db:
        finished = await db.get(SessionReplyWorkItem, work_id)
        assert finished is not None
        assert finished.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())) == 1
        result_message = await db.get(Message, finished.result_message_id)
        assert result_message is not None
        assert result_message.content == "scheduled background completed"

    assert len(calls) == 2
    for call in calls:
        tool_names = {tool["function"]["name"] for tool in call["tools"]}
        assert IMAGE_GENERATION_TOOL_NAME in tool_names
        assert MANAGE_SCHEDULED_TASKS_TOOL_NAME not in tool_names
        assert MANAGE_TODO_TOOL_NAME not in tool_names


@pytest.mark.asyncio
async def test_scheduled_task_execution_excludes_scheduled_and_todo_tools(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-tool-filter-session"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["tool"]["enabled_tools"] = [MANAGE_SCHEDULED_TASKS_TOOL_NAME]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-tool-filter-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-tool-filter-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    trigger_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=PROFILE_ID,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="检查定时任务工具过滤",
        is_processed=True,
    )
    db_session.add(trigger_message)
    await db_session.flush()
    await db_session.refresh(trigger_message)

    work_item, _ = await scheduler_module.session_reply_queue_manager.enqueue_scheduled_summary(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        scheduled_task_id=1,
        trigger_message_id=trigger_message.id,
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(work_item)

    worker_id = "worker-tool-filter"
    work_item.status = SessionReplyWorkStatus.RUNNING
    work_item.locked_by = worker_id
    work_item.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(work_item)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_item.id, worker_id)

    captured_tools: list[dict[str, Any]] = []

    async def mock_create_chat_completion(**kwargs: Any) -> InternalResponse:
        captured_tools.extend(kwargs.get("tools") or [])
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="定时任务检查已完成"),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "create_chat_completion",
        mock_create_chat_completion,
        raising=False,
    )
    monkeypatch.setattr(LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_create_chat_completion, raising=False)

    response = await executor_replies_module._execute_scheduled(db_session, work_item, worker_id)

    assert captured_tools
    captured_tool_names = {tool["function"]["name"] for tool in captured_tools}
    assert MANAGE_SCHEDULED_TASKS_TOOL_NAME not in captured_tool_names
    assert MANAGE_TODO_TOOL_NAME not in captured_tool_names
    assert END_SESSION_TOOL_NAME in captured_tool_names
    assert response["content"] == "定时任务检查已完成"


@pytest.mark.asyncio
async def test_scheduled_task_history_boundary_isolates_later_triggers(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-history-boundary-session"
    profile_id = 201
    model_id = "boundary-model"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="boundary-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=profile_id, uid=UID, name="boundary-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    prior_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="历史前置消息",
        is_processed=True,
    )
    db_session.add(prior_message)
    await db_session.flush()
    await db_session.refresh(prior_message)

    trigger_a = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="A任务触发指令",
        is_processed=True,
    )
    db_session.add(trigger_a)
    await db_session.flush()
    await db_session.refresh(trigger_a)

    trigger_b = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="B任务指令特征词",
        is_processed=True,
    )
    db_session.add(trigger_b)
    await db_session.flush()
    await db_session.refresh(trigger_b)

    assert trigger_a.id is not None and trigger_b.id is not None
    assert trigger_a.id < trigger_b.id

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    session.context_summary = "共享总结已覆盖B任务指令特征词"
    session.context_summary_message_id = trigger_b.id
    db_session.add(session)

    work_item_a, _ = await scheduler_module.session_reply_queue_manager.enqueue_scheduled_summary(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        scheduled_task_id=1,
        trigger_message_id=trigger_a.id,
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(work_item_a)

    worker_id = "worker-boundary"
    work_item_a.status = SessionReplyWorkStatus.RUNNING
    work_item_a.locked_by = worker_id
    work_item_a.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(work_item_a)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_item_a.id, worker_id)

    captured_calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        captured_calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="A任务处理完毕"),
            model=model_id,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "create_chat_completion", mock_generate, raising=False)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_generate, raising=False)

    response = await executor_replies_module._execute_scheduled(db_session, work_item_a, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert any("A任务触发指令" in content for content in message_contents)
    assert any("历史前置消息" in content for content in message_contents)
    assert not any("B任务指令" in content for content in message_contents)
    assert not any("共享总结已覆盖" in content for content in message_contents)
    for m in llm_messages:
        if m.id is not None:
            assert m.id <= trigger_a.id
    assert response["content"] == "A任务处理完毕"


@pytest.mark.asyncio
async def test_scheduled_task_history_boundary_isolates_later_triggers_on_reload_history_resume(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-history-boundary-reload-session"
    profile_id = 203
    model_id = "boundary-reload-model"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="boundary-reload-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=profile_id, uid=UID, name="boundary-reload-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    prior_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="历史前置消息",
        is_processed=True,
    )
    db_session.add(prior_message)
    await db_session.flush()
    await db_session.refresh(prior_message)

    trigger_a = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="A任务触发指令",
        is_processed=True,
    )
    db_session.add(trigger_a)
    await db_session.flush()
    await db_session.refresh(trigger_a)

    trigger_b = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="B任务指令特征词",
        is_processed=True,
    )
    db_session.add(trigger_b)
    await db_session.flush()
    await db_session.refresh(trigger_b)

    assert trigger_a.id is not None and trigger_b.id is not None
    assert trigger_a.id < trigger_b.id

    work, _ = await scheduler_module.session_reply_queue_manager.enqueue_scheduled_summary(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        scheduled_task_id=1,
        trigger_message_id=trigger_a.id,
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(work)

    worker_id = "worker-boundary-reload"
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = int(get_local_time().timestamp()) + 300
    work.execution_state = dict(work.execution_state or {})
    work.execution_state["dispatcher_checkpoint"] = {
        "messages": [],
        "turn_messages": [],
        "reload_history": True,
        "context_summary_trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE.value,
        "context_summary_fixed_upper_message_id": trigger_a.id,
    }
    await db_session.commit()
    await db_session.refresh(work)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)

    captured_calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        captured_calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="A任务处理完毕"),
            model=model_id,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "create_chat_completion", mock_generate, raising=False)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_generate, raising=False)

    response = await executor_replies_module._execute_scheduled(db_session, work, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert any("A任务触发指令" in content for content in message_contents)
    assert any("历史前置消息" in content for content in message_contents)
    assert not any("B任务指令特征词" in content for content in message_contents)
    assert not any(m.id == trigger_b.id for m in llm_messages if m.id is not None)
    for m in llm_messages:
        if m.id is not None:
            assert m.id <= trigger_a.id
    assert response["content"] == "A任务处理完毕"


@pytest.mark.asyncio
async def test_scheduled_task_does_not_hijack_foreground_external_results(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-no-hijack-external-results-session"
    profile_id = 202
    model_id = "no-hijack-model"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="no-hijack-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=profile_id, uid=UID, name="no-hijack-profile", configs=configs),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    fg_user_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="前台用户指令",
        is_processed=True,
    )
    db_session.add(fg_user_message)
    await db_session.flush()
    await db_session.refresh(fg_user_message)

    fg_tool_call_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=json.dumps({"tool_calls": [{"id": "bg-call-1", "name": "bg_tool", "arguments": {}}]}),
        is_processed=True,
    )
    db_session.add(fg_tool_call_message)
    await db_session.flush()
    await db_session.refresh(fg_tool_call_message)

    fg_tool_res_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=json.dumps({"tool_call_id": "bg-call-1", "content": json.dumps({"status": "queued", "task_id": 999})}),
        is_processed=True,
    )
    db_session.add(fg_tool_res_message)
    await db_session.flush()
    await db_session.refresh(fg_tool_res_message)

    fg_work, _ = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=str(fg_user_message.id),
        dedupe_key=f"foreground-message:{fg_user_message.id}",
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(fg_work)
    fg_work.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    fg_work.execution_state = {
        "pending_tools": [
            {
                "task_id": 999,
                "tool_call_id": "bg-call-1",
                "tool_name": "bg_tool",
                "tool_result_message_id": fg_tool_res_message.id,
            }
        ]
    }
    await db_session.commit()
    await db_session.refresh(fg_work)

    bg_task = BackgroundTask(
        id=999,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        tool_call_id="bg-call-1",
        tool_name="bg_tool",
        status=BackgroundTaskStatus.SUCCEEDED,
        result={"content": "前台专属后台任务结果特征词"},
        auto_reply=False,
        reply_status=BackgroundTaskReplyStatus.NONE,
        extra={
            "origin_work_id": fg_work.id,
            "origin_work_identity": fg_work.dedupe_key,
        },
    )
    db_session.add(bg_task)

    scheduled_trigger = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content="定时任务触发检查",
        is_processed=True,
    )
    db_session.add(scheduled_trigger)
    await db_session.flush()
    await db_session.refresh(scheduled_trigger)

    bg_result_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.BACKGROUND_TASK_RESULT,
        content=json.dumps(
            {
                "type": "background_tool_result",
                "task_id": 999,
                "tool_call_id": "bg-call-1",
                "instruction": "前台专用后台结果指令",
                "task": {"status": "succeeded", "result": "前台专属后台任务结果特征词"},
            }
        ),
        is_processed=True,
    )
    db_session.add(bg_result_message)
    await db_session.commit()
    await db_session.refresh(bg_result_message)

    assert scheduled_trigger.id is not None and bg_result_message.id is not None
    assert bg_result_message.id > scheduled_trigger.id

    scheduled_work, _ = await scheduler_module.session_reply_queue_manager.enqueue_scheduled_summary(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        scheduled_task_id=1,
        trigger_message_id=scheduled_trigger.id,
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(scheduled_work)

    worker_id = "worker-task-external"
    scheduled_work.status = SessionReplyWorkStatus.RUNNING
    scheduled_work.locked_by = worker_id
    scheduled_work.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(scheduled_work)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (scheduled_work.id, worker_id)

    captured_calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        captured_calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="定时任务正常回复"),
            model=model_id,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "create_chat_completion", mock_generate, raising=False)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_generate, raising=False)

    response = await executor_replies_module._execute_scheduled(db_session, scheduled_work, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert not any("前台专属后台任务结果特征词" in content for content in message_contents)
    assert not any("前台专用后台结果指令" in content for content in message_contents)
    assert not any(m.type == MessageType.BACKGROUND_TASK_RESULT for m in llm_messages if hasattr(m, "type"))
    assert not any(m.id == bg_result_message.id for m in llm_messages if m.id is not None)
    assert "前台专属后台任务结果特征词" not in response["content"]

    refreshed_bg_task = await db_session.get(BackgroundTask, 999)
    assert refreshed_bg_task is not None
    reply_owner = refreshed_bg_task.extra.get("reply_owner")
    assert reply_owner is None or reply_owner.get("work_id") != scheduled_work.id

    refreshed_fg_work = await db_session.get(SessionReplyWorkItem, fg_work.id)
    assert refreshed_fg_work is not None
    assert refreshed_fg_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
