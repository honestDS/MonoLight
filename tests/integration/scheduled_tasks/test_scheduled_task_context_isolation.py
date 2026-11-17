import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.session_reply_queue.executor_agent_work as executor_agent_work_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
from app.core.constants import (
    END_SESSION_TOOL_NAME,
    MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    MANAGE_TODO_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.time import get_local_time
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.user import User
from app.providers.llm.client import LLMClient
from tests.integration.scheduled_tasks.scheduled_workflow_support import (
    MODEL_ID,
    PROFILE_ID,
    UID,
    USAGE,
    _enqueue_scheduled_agent_work,
    db_session,
    session_factory,
)

__all__ = ("db_session", "session_factory")


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

    work_item, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        trigger_message=trigger_message,
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

    response = await executor_agent_work_module.execute_agent_work(db_session, work_item, worker_id)

    assert captured_tools
    captured_tool_names = {tool["function"]["name"] for tool in captured_tools}
    assert MANAGE_SCHEDULED_TASKS_TOOL_NAME not in captured_tool_names
    assert MANAGE_TODO_TOOL_NAME not in captured_tool_names
    assert END_SESSION_TOOL_NAME in captured_tool_names
    assert response["content"] == "定时任务检查已完成"


@pytest.mark.asyncio
async def test_scheduled_agent_work_ignores_all_conversation_history(
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

    work_item_a, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        trigger_message=trigger_a,
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

    response = await executor_agent_work_module.execute_agent_work(db_session, work_item_a, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert any("A任务触发指令" in content for content in message_contents)
    assert not any("历史前置消息" in content for content in message_contents)
    assert not any("B任务指令" in content for content in message_contents)
    assert not any("共享总结已覆盖" in content for content in message_contents)
    assert {m.id for m in llm_messages if m.id is not None} == {trigger_a.id}
    assert response["content"] == "A任务处理完毕"


@pytest.mark.asyncio
async def test_scheduled_agent_work_reload_history_resume_stays_context_free(
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

    work, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        trigger_message=trigger_a,
    )
    await db_session.flush()
    await db_session.refresh(work)

    worker_id = "worker-boundary-reload"
    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = worker_id
    work.lock_until = int(get_local_time().timestamp()) + 300
    checkpoint = {
        "messages": [],
        "turn_messages": [],
        "reload_history": True,
        "context_summary_trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE.value,
        "context_summary_fixed_upper_message_id": trigger_a.id,
    }
    work.execution_state = dict(work.execution_state or {})
    await db_session.flush()
    assert work.id is not None
    await session_reply_work_item_crud.save_checkpoint(db_session, work.id, checkpoint)
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

    response = await executor_agent_work_module.execute_agent_work(db_session, work, worker_id)

    assert len(captured_calls) == 1
    llm_messages: list[InternalMessage] = captured_calls[0]["messages"]
    message_contents = [str(m.content) for m in llm_messages if m.content]

    assert any("A任务触发指令" in content for content in message_contents)
    assert not any("历史前置消息" in content for content in message_contents)
    assert not any("B任务指令特征词" in content for content in message_contents)
    assert {m.id for m in llm_messages if m.id is not None} == {trigger_a.id}
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

    scheduled_work, _ = await _enqueue_scheduled_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=profile_id,
        trigger_message=scheduled_trigger,
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

    response = await executor_agent_work_module.execute_agent_work(db_session, scheduled_work, worker_id)

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
