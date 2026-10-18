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
import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
from app.core.constants import MANAGE_SCHEDULED_TASKS_TOOL_NAME
from app.core.dispatcher import ChatDispatcher
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_SUMMARY_PROMPT
from app.core.tools.scheduled_tasks import ManageScheduledTasksExecutor
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord, AuditRecordStatus, AuditToolConclusion, AuditToolDetail
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User

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


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
async def test_llm_workflow_creates_lists_and_deletes_scheduled_task(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
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
                    }
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
            tool_call = InternalToolCall(
                id="scheduled-create-call",
                name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                arguments={
                    "operation": "create",
                    "name": "Pipeline reminder",
                    "message": "Review deployment status",
                    "interval_seconds": 60,
                },
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
    listed_tasks = observed_results[1]["tasks"]
    assert len(listed_tasks) == 1
    listed_task = listed_tasks[0]
    assert listed_task["id"] == created_task["id"]
    assert (listed_task["uid"], listed_task["session_id"], listed_task["profile_id"]) == (UID, SESSION_ID, PROFILE_ID)
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
@pytest.mark.parametrize("operation", ["create", "delete"])
async def test_scheduled_task_mutation_waits_for_audit_confirmation(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
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

        arguments = (
            {
                "operation": "create",
                "name": "Pipeline reminder",
                "message": "Review deployment status",
                "interval_seconds": 60,
            }
            if operation == "create"
            else {"operation": "delete", "task_id": 1}
        )
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
        assert json.loads(detail.arguments_summary)["operation"] == operation

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
