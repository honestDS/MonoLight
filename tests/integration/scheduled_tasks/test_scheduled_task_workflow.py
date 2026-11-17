from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.message_platforms.notifier as notifier_module
import app.core.session.notifier as session_notifier_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    END_SESSION_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.exceptions import ServerException
from app.core.profile.deletion import build_profile_deletion_impact, execute_profile_deletion
from app.core.session_reply_queue.executor_resume import restore_interactive_tool_replies
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.llm.client import LLMClient
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
async def test_agent_work_resume_restores_its_hidden_tool_result_only(
    db_session: AsyncSession,
) -> None:
    profile_id = 204
    session_id = "agent-work-resume-hidden-result"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="agent-work-resume-profile", configs=ProfileConfig.model_validate({}).model_dump()),
            ChatSession(session_id=session_id, uid=UID, profile_id=profile_id),
        ]
    )
    await db_session.commit()

    work, _created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="resume-trigger",
        dedupe_key="scheduled-agent-work:resume-hidden-result",
        spec=AgentWorkSpec(
            instruction="恢复隐藏工具结果",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
        commit=False,
    )
    await db_session.commit()
    await db_session.refresh(work)
    assert work.id is not None

    worker_id = "agent-work-resume-worker"
    claimed = await session_reply_work_item_crud.claim_next(db_session, worker_id=worker_id, lease_seconds=300)
    assert claimed is not None
    assert claimed.id == work.id
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)

    call_id = "resume-hidden-call"
    assistant_call = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id=call_id, name="resume_tool", arguments={})],
    )
    saved_call = await save_assistant_message(db_session, session_id, UID, profile_id, assistant_call)
    assert saved_call.id is not None

    public_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="公开主会话消息",
        is_processed=True,
    )
    await message_crud.persist(db_session, message=public_message, commit=False)

    other_work, _created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="other-agent-work",
        dedupe_key="scheduled-agent-work:other-hidden-result",
        spec=AgentWorkSpec(
            instruction="其他工作",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(other_work)
    assert other_work.id is not None
    other_worker_id = "other-agent-work-worker"
    other_work.status = SessionReplyWorkStatus.RUNNING
    other_work.locked_by = other_worker_id
    other_work.lock_until = int(get_local_time().timestamp()) + 300
    db_session.add(other_work)
    await db_session.commit()

    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (other_work.id, other_worker_id)
    other_result = await save_tool_response(
        db_session,
        session_id,
        UID,
        profile_id,
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=call_id,
            content="其他工作结果",
            tool_execution_status="succeeded",
        ),
        [],
        [],
    )
    assert other_result.id is not None

    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    own_result = await save_tool_response(
        db_session,
        session_id,
        UID,
        profile_id,
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=call_id,
            content="真实成功结果",
            tool_execution_status="succeeded",
        ),
        [],
        [],
    )
    assert own_result.id is not None

    checkpoint_messages = [assistant_call.model_dump(mode="json", exclude_none=True)]
    checkpoint = {
        "messages": checkpoint_messages,
        "turn_messages": list(checkpoint_messages),
        "reload_history": False,
    }
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    restored = await restore_interactive_tool_replies(
        db_session,
        work=work,
        worker_id=worker_id,
        checkpoint=checkpoint,
    )

    restored_messages = [InternalMessage.model_validate(message) for message in restored["messages"]]
    restored_turn_messages = [InternalMessage.model_validate(message) for message in restored["turn_messages"]]
    restored_results = [message for message in restored_messages if message.role == MessageRole.TOOL]
    restored_turn_results = [message for message in restored_turn_messages if message.role == MessageRole.TOOL]
    assert len(restored_results) == 1
    assert restored_results[0].id == own_result.id
    assert restored_results[0].content == "真实成功结果"
    assert restored_results[0].tool_execution_status == "succeeded"
    assert len(restored_turn_results) == 1
    assert restored_turn_results[0].id == own_result.id
    assert not any(message.tool_execution_status == "execution_unknown" for message in restored_results)

    tool_rows = list(
        (
            await db_session.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.type == MessageType.TOOL_RESULT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.id for row in tool_rows} == {other_result.id, own_result.id}
    assert [row.id for row in tool_rows if row.work_id == work.id] == [own_result.id]
    saved_call_row = await db_session.get(Message, saved_call.id)
    assert saved_call_row is not None
    assert saved_call_row.work_id == work.id
    public_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert all(message.work_id is None for message in public_history)
    assert all(message.type not in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} for message in public_history)

    restored_again = await restore_interactive_tool_replies(
        db_session,
        work=work,
        worker_id=worker_id,
        checkpoint=restored,
    )
    assert restored_again == restored
    tool_rows_after_repeat = list(
        (
            await db_session.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.type == MessageType.TOOL_RESULT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.id for row in tool_rows_after_repeat} == {row.id for row in tool_rows}


@pytest.mark.asyncio
async def test_work_owned_messages_stay_out_of_session_history_until_promoted(db_session: AsyncSession) -> None:
    profile_id = 190
    session_id = "work-owned-message-isolation"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="work-owned-message-profile", configs=ProfileConfig.model_validate({}).model_dump()),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id))
    await db_session.flush()

    main_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="主会话消息",
        is_processed=True,
    )
    await message_crud.persist(db_session, message=main_message, commit=False)
    internal_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="AgentWork内部消息",
        is_processed=True,
        work_id=777,
    )
    await message_crud.persist(db_session, message=internal_message, commit=False)
    await db_session.commit()

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    assert session.latest_message_id == main_message.id

    model_history = await message_crud.get_history_backward_by_id(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(model_history)] == ["主会话消息"]

    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息"]

    assert internal_message.id is not None
    promoted = await message_crud.promote_work_message_to_session(
        db_session,
        message_id=internal_message.id,
        work_id=777,
        commit=True,
    )
    assert promoted is True

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    assert session.latest_message_id == internal_message.id
    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息", "AgentWork内部消息"]


@pytest.mark.asyncio
async def test_agent_work_saved_messages_are_hidden_until_promoted(db_session: AsyncSession) -> None:
    profile_id = 191
    session_id = "agent-work-save-isolation"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="agent-work-save-profile", configs=ProfileConfig.model_validate({}).model_dump()),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id))
    await db_session.flush()

    main_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="主会话消息",
        is_processed=True,
    )
    await message_crud.persist(db_session, message=main_message, commit=False)

    work, _created = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="trigger-1",
        dedupe_key="scheduled-agent-work:save-isolation",
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(work)
    assert work.id is not None

    worker_id = "worker-save-isolation"
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = int(get_local_time().timestamp()) + 300
    db_session.add(work)
    await db_session.commit()
    await db_session.refresh(work)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)

    saved = await save_assistant_message(
        db_session,
        session_id,
        UID,
        profile_id,
        InternalMessage(role=MessageRole.ASSISTANT, content="计划任务结果"),
    )
    assert saved.id is not None

    saved_row = await db_session.get(Message, saved.id)
    assert saved_row is not None
    assert saved_row.work_id == work.id

    work_user_message = Message(
        session_id=session_id,
        uid=UID,
        profile_id=profile_id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="工作消息不应进入主会话输入队列",
        is_processed=False,
        work_id=work.id,
    )
    await message_crud.persist(db_session, message=work_user_message, commit=False)
    unprocessed = await message_crud.get_unprocessed_messages(db_session, session_id=session_id, uid=UID)
    assert work_user_message not in unprocessed

    session = await db_session.get(ChatSession, session_id)
    assert session is not None
    assert session.latest_message_id == main_message.id
    model_history = await message_crud.get_history_backward_by_id(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(model_history)] == ["主会话消息"]
    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息"]

    promoted = await message_crud.promote_work_message_to_session(
        db_session,
        message_id=saved.id,
        work_id=work.id,
        commit=True,
    )
    assert promoted is True
    ui_history = await message_crud.get_history_paged(db_session, session_id=session_id, uid=UID)
    assert [message.content for message in reversed(ui_history)] == ["主会话消息", "计划任务结果"]


@pytest.mark.asyncio
async def test_scheduled_agent_work_failure_message_is_visible_in_session_history(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id = 192
    session_id = "agent-work-failure-isolation"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=profile_id, uid=UID, name="agent-work-failure-profile", configs=ProfileConfig.model_validate({}).model_dump()),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=profile_id))
    await db_session.flush()

    work, _created = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="1",
        dedupe_key="scheduled-agent-work:failure-isolation",
        commit=False,
    )
    await db_session.flush()
    await db_session.refresh(work)
    assert work.id is not None
    work.execution_state = {
        "frozen_context_message_ids": [],
        "agent_work": AgentWorkSpec(
            instruction="计划任务失败",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ).model_dump(mode="json"),
    }
    db_session.add(work)

    worker_id = "worker-failure-isolation"
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = int(get_local_time().timestamp()) + 300
    db_session.add(work)
    await db_session.commit()

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(session_notifier_module, "AsyncSessionLocal", session_factory)

    await executor_lifecycle_module.fail_session_reply_work(work.id, worker_id, "internal failure", user_error="计划任务失败")

    async with session_factory() as verification_db:
        error_message = (await verification_db.execute(select(Message).where(Message.session_id == session_id, Message.role == MessageRole.ERR))).scalars().one()
        session = await verification_db.get(ChatSession, session_id)
        assert session is not None
        finished_work = await verification_db.get(SessionReplyWorkItem, work.id)
        assert finished_work is not None
        assert finished_work.status == SessionReplyWorkStatus.FAILED
        model_history = await message_crud.get_history_backward_by_id(verification_db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(verification_db, session_id=session_id, uid=UID)
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())

    assert error_message.work_id is None
    assert session.latest_message_id == error_message.id
    assert [message.id for message in reversed(model_history)] == [error_message.id]
    assert [message.id for message in reversed(ui_history)] == [error_message.id]
    assert len(session_events) == 1
    error_event = session_events[0].event
    assert error_event["type"] == "proactive_reply_error"
    assert error_event["work_id"] == work.id
    assert error_event["message_id"] == error_message.id
    assert error_event["content"] == error_message.content
    assert finished_work.event_sent is True


@pytest.mark.asyncio
async def test_scheduled_task_workflow_preserves_profile_binding_across_session_switches(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-profile-switch-session"
    profile_a_id, profile_b_id, profile_c_id = 301, 302, 303
    model_a, model_b, model_c = "scheduled-profile-a", "scheduled-profile-b", "scheduled-profile-c"

    def profile_configs(model_id: str) -> dict[str, Any]:
        configs = ProfileConfig.model_validate({}).model_dump()
        configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]
        configs["memory"]["precheck_enabled"] = False
        return configs

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-profile-switch-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[{"model_id": model_id, "usage": "CHAT", "protocol": "OPENAI", "context_window_k": 32, "max_tokens": 512, "temperature": 0, "top_p": 1} for model_id in (model_a, model_b, model_c)],
            ),
            Profile(id=profile_a_id, uid=UID, name="scheduled-profile-a", configs=profile_configs(model_a), is_default=True),
            Profile(id=profile_b_id, uid=UID, name="scheduled-profile-b", configs=profile_configs(model_b)),
            Profile(id=profile_c_id, uid=UID, name="scheduled-profile-c", configs=profile_configs(model_c)),
            ChatSession(
                session_id=session_id,
                uid=UID,
                profile_id=profile_a_id,
                profile_override_id=None,
                goal_mode=True,
                max_turns=5,
            ),
        ]
    )
    await db_session.flush()
    scheduled_task = ScheduledTask(
        name="Profile switch reminder",
        uid=UID,
        session_id=session_id,
        message="Run the profile-bound reminder",
        interval_seconds=60,
        run_once=False,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db_session.add(scheduled_task)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    assert scheduled_task.id is not None
    task_id = scheduled_task.id

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    for module in (
        channel_cursor_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)

    async def set_session_profile(profile_id: int | None = None, profile_override_id: int | None = None) -> None:
        async with session_factory() as profile_db:
            session = await profile_db.get(ChatSession, session_id)
            assert session is not None
            if profile_id is not None:
                session.profile_id = profile_id
            session.profile_override_id = profile_override_id
            await profile_db.commit()

    async def capture_send_session_event(_uid: str, _session_id: str, _event: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_send_session_event)

    await set_session_profile(profile_override_id=profile_b_id)
    async with session_factory() as setup_db:
        session = await setup_db.get(ChatSession, session_id)
        assert session is not None
        assert (session.profile_id, session.profile_override_id) == (profile_a_id, profile_b_id)

    async def scheduled_runs(db: AsyncSession) -> tuple[list[Message], list[SessionReplyWorkItem]]:
        triggers = list((await db.execute(select(Message).where(Message.session_id == session_id, Message.type == MessageType.SCHEDULED_TASK_TRIGGER))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id, SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN).order_by(SessionReplyWorkItem.id))).scalars().all())
        return triggers, works

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(task_id)

    async with session_factory() as verify_db:
        trigger_messages, work_items = await scheduled_runs(verify_db)
    assert len(trigger_messages) == len(work_items) == 1
    first_trigger, first_work = trigger_messages[0], work_items[0]
    assert first_trigger.profile_id == profile_b_id
    assert first_work.profile_id == profile_b_id
    assert first_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert first_trigger.work_id == first_work.id
    assert first_work.id is not None
    first_work_id = first_work.id

    worker_id = "scheduled-profile-switch-worker"
    await set_session_profile(profile_c_id, profile_c_id)
    async with session_factory() as claim_db:
        claimed = await session_reply_work_item_crud.claim_next(claim_db, worker_id=worker_id, lease_seconds=300)
    assert claimed is not None
    assert claimed.id == first_work_id
    assert claimed.profile_id == profile_b_id

    calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        assert kwargs["model_id"] == model_b
        if len(calls) == 1:
            await set_session_profile(profile_a_id, profile_a_id)
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="profile-switch-end-session",
                            name=END_SESSION_TOOL_NAME,
                            arguments={"skip_reply": False},
                        )
                    ],
                ),
                model=model_b,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if len(calls) == 2:
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=FINAL_REPLY),
                model=model_b,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected model call {len(calls)}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)

    await executor_lifecycle_module.execute_session_reply_work(first_work_id, worker_id)

    async with session_factory() as verify_db:
        finished_work = await verify_db.get(SessionReplyWorkItem, first_work_id)
        assert finished_work is not None
        assert finished_work.result_message_id is not None
        final_message = await verify_db.get(Message, finished_work.result_message_id)
        session = await verify_db.get(ChatSession, session_id)
        task = await verify_db.get(ScheduledTask, task_id)
    assert finished_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert final_message is not None
    assert final_message.profile_id == profile_b_id
    assert final_message.content == FINAL_REPLY
    assert session is not None
    assert (session.profile_id, session.profile_override_id) == (profile_a_id, profile_a_id)
    assert task is not None
    assert task.status == ScheduledTaskStatus.ENABLED
    assert [call["model_id"] for call in calls] == [model_b, model_b]
    async with session_factory() as reschedule_db:
        task = await reschedule_db.get(ScheduledTask, task_id)
        assert task is not None
        task.next_run_at = get_local_time() - timedelta(seconds=1)
        await reschedule_db.commit()

    await scheduler._dispatch_one(task_id)

    async with session_factory() as verify_db:
        task = await verify_db.get(ScheduledTask, task_id)
        _trigger_messages, work_items = await scheduled_runs(verify_db)
    assert task is not None
    assert task.status == ScheduledTaskStatus.ENABLED
    assert task.run_count == 2
    assert len(work_items) == 2
    assert work_items[0].id == first_work_id
    assert work_items[0].profile_id == profile_b_id
    assert work_items[0].status == SessionReplyWorkStatus.SUCCEEDED
    assert work_items[1].profile_id == profile_a_id
    assert work_items[1].status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_scheduled_task_work_rejects_profile_deleted_during_dispatch(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    assert scheduled_task.id is not None
    task_id = scheduled_task.id
    profile_b_id = PROFILE_ID + 1

    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    profile_a = await db_session.get(Profile, PROFILE_ID)
    assert profile_a is not None
    profile_a.configs = configs
    profile_a.is_default = False
    db_session.add_all(
        [
            ModelChannel(
                id=1,
                name="scheduled-profile-deletion-channel",
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
            Profile(
                id=profile_b_id,
                uid=UID,
                name="scheduled-profile-deletion-default",
                configs=deepcopy(configs),
                is_default=True,
            ),
        ]
    )
    await db_session.flush()
    session = await db_session.get(ChatSession, SESSION_ID)
    assert session is not None
    session.profile_id = PROFILE_ID
    session.profile_override_id = PROFILE_ID
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    for module in (
        channel_cursor_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)

    async def capture_send_session_event(_uid: str, _session_id: str, _event: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_send_session_event)

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(task_id)

    async with session_factory() as claim_db:
        queued_work = (
            (
                await claim_db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                        SessionReplyWorkItem.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .one()
        )
        assert queued_work.profile_id == PROFILE_ID
        assert queued_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert queued_work.id is not None
        work_id = queued_work.id
        claimed_work = await session_reply_work_item_crud.claim_next(claim_db, worker_id="profile-deletion-worker", lease_seconds=300)
        assert claimed_work is not None
        assert claimed_work.id == work_id

    calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        if len(calls) != 1:
            raise AssertionError(f"Unexpected model call {len(calls)}")
        async with session_factory() as deletion_db:
            switched_session = await deletion_db.get(ChatSession, SESSION_ID)
            assert switched_session is not None
            switched_session.profile_id = profile_b_id
            switched_session.profile_override_id = profile_b_id
            await deletion_db.commit()

            profile_to_delete = await deletion_db.get(Profile, PROFILE_ID)
            assert profile_to_delete is not None
            impact = await build_profile_deletion_impact(deletion_db, profile=profile_to_delete)
            assert impact["sessions"]["count"] == 0
            assert impact["scheduled_tasks"]["count"] == 0
            await execute_profile_deletion(deletion_db, profile=profile_to_delete, impact=impact)
            await deletion_db.commit()

        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="profile-deletion-end-session",
                        name=END_SESSION_TOOL_NAME,
                        arguments={"skip_reply": False},
                    )
                ],
            ),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)

    with pytest.raises(ServerException):
        await executor_lifecycle_module.execute_session_reply_work(work_id, "profile-deletion-worker")

    assert len(calls) == 1
    async with session_factory() as verification_db:
        finished_work = await verification_db.get(SessionReplyWorkItem, work_id)
        remaining_profile_a = await verification_db.get(Profile, PROFILE_ID)
        remaining_session = await verification_db.get(ChatSession, SESSION_ID)
        remaining_task = await verification_db.get(ScheduledTask, task_id)
        work_messages = list((await verification_db.execute(select(Message).where(Message.work_id == work_id))).scalars().all())

    assert finished_work is not None
    assert finished_work.status == SessionReplyWorkStatus.CANCELLED
    assert finished_work.locked_by is None
    assert finished_work.lock_until is None
    assert finished_work.result_message_id is None
    assert remaining_profile_a is None
    assert remaining_session is not None
    assert (remaining_session.profile_id, remaining_session.profile_override_id) == (profile_b_id, profile_b_id)
    assert remaining_task is not None
    assert remaining_task.status == ScheduledTaskStatus.DISABLED
    assert [message for message in work_messages if message.role in {MessageRole.ASSISTANT, MessageRole.TOOL}] == []
