import json
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.audit.persistence as audit_persistence_module
import app.core.background_tasks.scheduler as scheduler_module
import app.core.confirmation.service as confirmation_service_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
from app.core.constants import END_SESSION_SCOPE_TASK, MANAGE_SCHEDULED_TASKS_TOOL_NAME
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.prompts import AUDIT_BATCH_PROMPT
from app.core.tools.scheduled_tasks import ManageScheduledTasksExecutor
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.audit import AuditConfirmationClaim, AuditRecord, AuditRecordStatus, AuditToolDetail
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
from tests.integration.scheduled_tasks.scheduled_workflow_support import (
    FINAL_REPLY,
    MODEL_ID,
    PROFILE_ID,
    SESSION_ID,
    UID,
    USAGE,
    _create_due_run_once_scheduled_task,
    db_session,
    session_factory,
)

__all__ = ("db_session", "session_factory")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("run_once", [False, True], ids=["recurring", "one-time"])
async def test_llm_workflow_creates_lists_and_deletes_scheduled_task(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream: bool,
    run_once: bool,
) -> None:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["security"].update({"audit_channel_id": 1, "audit_model_id": MODEL_ID, "audit_threshold": 5})

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
    monkeypatch.setattr(
        confirmation_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    calls: list[dict[str, Any]] = []
    observed_results: list[dict[str, Any]] = []
    audit_requests: list[dict[str, Any]] = []

    def tool_result(messages: list[InternalMessage], call_id: str) -> dict[str, Any]:
        for message in reversed(messages):
            if message.role == MessageRole.TOOL and message.tool_call_id == call_id:
                assert isinstance(message.content, str)
                result = json.loads(message.content)["tool_result"]
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
        messages: list[InternalMessage] = kwargs["messages"]
        system_messages = [message for message in messages if message.role == MessageRole.SYSTEM]
        if system_messages and system_messages[0].content == AUDIT_BATCH_PROMPT:
            audit_payload = json.loads(next(message.content for message in messages if message.role == MessageRole.USER))
            audit_requests.append(audit_payload)
            results = [
                {
                    "tool_call_id": item["tool_call_id"],
                    "score": 0,
                    "deny": False,
                    "reason": "scheduled task operation is safe",
                    "file_checks": [],
                }
                for item in audit_payload["tool_calls"]
            ]
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content=json.dumps({"results": results}),
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
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
    assert [item["arguments"]["operation"] for request in audit_requests for item in request["tool_calls"]] == ["create", "delete"]
    assert response["choices"][0]["message"]["content"] == FINAL_REPLY
    assert [result["operation"] for result in observed_results] == ["create", "list", "delete"]

    created_task = observed_results[0]["task"]
    assert (created_task["uid"], created_task["session_id"]) == (UID, SESSION_ID)
    assert created_task["run_once"] is run_once
    listed_tasks = observed_results[1]["tasks"]
    assert len(listed_tasks) == 1
    listed_task = listed_tasks[0]
    assert listed_task["id"] == created_task["id"]
    assert (listed_task["uid"], listed_task["session_id"]) == (UID, SESSION_ID)
    assert listed_task["run_once"] is run_once
    assert observed_results[2]["task_id"] == created_task["id"]

    async with session_factory() as verification_db:
        remaining_tasks = list((await verification_db.execute(select(ScheduledTask))).scalars().all())
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id.asc()))).scalars().all())
        audit_records = list((await verification_db.execute(select(AuditRecord).where(AuditRecord.uid == UID, AuditRecord.session_id == SESSION_ID).order_by(AuditRecord.id.asc()))).scalars().all())
        audit_details = list((await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id.in_([record.id for record in audit_records])).order_by(AuditToolDetail.audit_record_id, AuditToolDetail.turn_index))).scalars().all())
        confirmation_cards = list((await verification_db.execute(select(Message).where(Message.uid == UID, Message.session_id == SESSION_ID, Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().all())
        confirmation_claims = list((await verification_db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.uid == UID, AuditConfirmationClaim.session_id == SESSION_ID))).scalars().all())

    assert remaining_tasks == []
    assert len(audit_records) == 2
    assert [record.status for record in audit_records] == [AuditRecordStatus.SUCCEEDED, AuditRecordStatus.SUCCEEDED]
    assert [json.loads(detail.arguments_summary)["operation"] for detail in audit_details] == ["create", "delete"]
    tool_call_messages = [message for message in persisted_messages if message.type == MessageType.TOOL_CALL]
    tool_result_messages = [message for message in persisted_messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_messages = [message for message in persisted_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_call_messages) == 3
    assert all(message.role == MessageRole.ASSISTANT for message in tool_call_messages)
    assert len(tool_result_messages) == 3
    assert all(message.role == MessageRole.TOOL for message in tool_result_messages)
    assert all(message.audit_record_id is None for message in tool_result_messages)
    assert confirmation_cards == []
    assert confirmation_claims == []
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
        is_default=True,
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
        assert queued_work.work_type == SessionReplyWorkType.AGENT_WORK
        assert queued_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert trigger_message.work_id == queued_work.id
        assert queued_work.execution_state.get("frozen_context_message_ids") == []
        queued_spec = AgentWorkSpec.model_validate(queued_work.execution_state.get("agent_work"))
        assert queued_spec.instruction == "Review deployment status"
        assert queued_spec.execution_scope == END_SESSION_SCOPE_TASK
        assert queued_spec.report_target == AgentWorkReportTarget.SESSION
        assert queued_spec.instruction_message_id == trigger_message.id

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
        assert work_items[0].work_type == SessionReplyWorkType.AGENT_WORK
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
    original_enqueue = queue_manager.enqueue_agent_work

    async def enqueue_then_fail(*args: Any, **kwargs: Any) -> None:
        await original_enqueue(*args, **kwargs)
        raise RuntimeError("simulated agent work enqueue failure")

    monkeypatch.setattr(queue_manager, "enqueue_agent_work", enqueue_then_fail)
    with pytest.raises(RuntimeError, match="simulated agent work enqueue failure"):
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

    monkeypatch.setattr(queue_manager, "enqueue_agent_work", original_enqueue)
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
        assert work_items[0].work_type == SessionReplyWorkType.AGENT_WORK
        assert work_items[0].status == SessionReplyWorkStatus.READY_FOR_LLM
