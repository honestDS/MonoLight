import asyncio
import json
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.providers.database as database_module
from app.core.constants import (
    END_SESSION_TOOL_NAME,
    ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED,
    FINAL_REPLY_TOOL_CORRECTION_MAX_ATTEMPTS,
    GOAL_EXECUTION_PHASE_FINALIZING,
    MANAGE_TODO_TOOL_NAME,
)
from app.core.crud.session.message import message_crud
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.exceptions import LLMModelCapabilityException, ServerException
from app.core.i18n import t
from app.core.i18n.context import reset_current_log_locale, set_current_log_locale
from app.core.prompts import GOAL_MODE_FINAL_RESPONSE_PROMPT, GOAL_MODE_SYSTEM_PROMPT, PROMPT_MAX_TURNS_REACHED, TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask
from app.models.channel import ModelChannel
from app.models.message import (
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    Message,
    MessageRole,
    MessageType,
)
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from app.models.user import User
from tests.database_support import clone_sqlite_schema

UID = "end-session-user"
PROFILE_ID = 1
CHANNEL_ID = 1
MODEL_ID = "end-session-model"
INITIAL_MESSAGE = "请结束本轮会话"
SUMMARY = "结束会话第一行\n" + ("这是一段较长的最终摘要，用于确认结束工具结果不会经过工具预算截断。 " * 80) + "\n结束会话最后一行"


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "end-session-workflow.sqlite3"
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


@pytest.fixture
def captured_logs() -> Generator[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        records.append(message.record)

    sink_id = logger.add(sink, level="INFO")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _profile_configs(
    *,
    max_parallel_tools: int = 1,
) -> dict[str, Any]:
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
        "security": {
            "audit_threshold": 0,
        },
        "tool": {
            "enabled_tools": [],
            "max_parallel_tools": max_parallel_tools,
        },
        "other": {
            "context_summary_threshold_percent": 90,
        },
        "memory": {
            "enabled": False,
            "precheck_enabled": False,
        },
    }


def _model_channel() -> ModelChannel:
    return ModelChannel(
        id=CHANNEL_ID,
        name="end-session-test-channel",
        api_key="enc:v1:stored-test-key",
        base_url="https://llm.invalid",
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
    )


def _patch_runtime_database(monkeypatch: pytest.MonkeyPatch, factory: async_sessionmaker[AsyncSession]) -> None:
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)


async def _seed_conversation(
    factory: async_sessionmaker[AsyncSession],
    session_id: str,
    *,
    max_parallel_tools: int = 1,
    goal_mode: bool = True,
    max_turns: int = 5,
    source: str = "http",
) -> InternalMessage:
    async with factory() as db:
        db.add(_model_channel())
        db.add(
            Profile(
                id=PROFILE_ID,
                uid=UID,
                name=f"end-session-profile-{session_id}",
                configs=_profile_configs(
                    max_parallel_tools=max_parallel_tools,
                ),
            )
        )
        db.add(User(uid=UID, username="end_session_user"))
        await db.flush()
        db.add(
            ChatSession(
                session_id=session_id,
                uid=UID,
                profile_id=PROFILE_ID,
                source=source,
                goal_mode=goal_mode,
                max_turns=max_turns,
            )
        )
        await db.flush()
        initial_message = Message(
            session_id=session_id,
            uid=UID,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=INITIAL_MESSAGE,
            profile_id=PROFILE_ID,
            is_processed=False,
        )
        db.add(initial_message)
        await db.commit()
        await db.refresh(initial_message)
        return InternalMessage(
            id=initial_message.id,
            role=MessageRole.USER,
            content=initial_message.content,
            created_at=initial_message.created_at.timestamp(),
        )


async def _seed_running_work(
    factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> tuple[InternalMessage, SessionReplyWorkItem]:
    initial_message = await _seed_conversation(factory, session_id, source="ws")
    async with factory() as db:
        work = SessionReplyWorkItem(
            uid=UID,
            session_id=session_id,
            profile_id=PROFILE_ID,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=str(initial_message.id),
            dedupe_key=f"end-session-queue-work:{session_id}",
            status=SessionReplyWorkStatus.RUNNING,
            locked_by="worker-1",
            input_message_ids=[initial_message.id],
            execution_state={
                "stream_requested": True,
                "context_summary_events_requested": False,
                "show_tool_calls": True,
                "expose_tool_call_content": True,
                "message_source": "ws",
                "request_ids": [f"request-{session_id}"],
            },
        )
        db.add(work)
        await db.commit()
        await db.refresh(work)
        return initial_message, work


def _end_session_response(
    *,
    call_id: str,
    reasoning: str | None = "结束前检查已完成",
    content: str | None = None,
    arguments: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            reasoning_content=reasoning,
            tool_calls=[
                InternalToolCall(
                    id=call_id,
                    name=END_SESSION_TOOL_NAME,
                    arguments=arguments if arguments is not None else {},
                )
            ],
        ),
        model=MODEL_ID,
        usage=usage or {"prompt_tokens": 31, "completion_tokens": 17, "total_tokens": 48},
        finish_reason="tool_calls",
    )


def _final_response(content: str = SUMMARY) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
        model=MODEL_ID,
        usage={"prompt_tokens": 19, "completion_tokens": 11, "total_tokens": 30},
        finish_reason="stop",
    )


def _todo_write_call(call_id: str = "todo-call") -> InternalToolCall:
    return InternalToolCall(
        id=call_id,
        name=MANAGE_TODO_TOOL_NAME,
        arguments={
            "operation": "write",
            "todos": [{"content": "不得执行此项", "status": "pending"}],
            "expected_revision": 0,
        },
    )


def _response_for_tool_calls(
    tool_calls: list[InternalToolCall],
    *,
    reasoning: str | None = "首轮工具协议检查",
    content: str | None = None,
) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            reasoning_content=reasoning,
            tool_calls=tool_calls,
        ),
        model=MODEL_ID,
        usage={"prompt_tokens": 29, "completion_tokens": 13, "total_tokens": 42},
        finish_reason="tool_calls",
    )


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[InternalResponse],
    *,
    stream: bool,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    response_iterator = iter(responses)

    async def generate(**kwargs: Any) -> InternalResponse:
        if stream:
            raise AssertionError("stream dispatch must use generate_with_stream_callback")
        calls.append(kwargs)
        return next(response_iterator)

    async def generate_with_stream_callback(**kwargs: Any) -> InternalResponse:
        if not stream:
            raise AssertionError("non-stream dispatch must use generate")
        calls.append(kwargs)
        response = next(response_iterator)
        if response.message.reasoning_content:
            await kwargs["on_reasoning"](response.message.reasoning_content)
        if isinstance(response.message.content, str) and response.message.content:
            await kwargs["on_content"](response.message.content)
        return response

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    return calls


async def _dispatch(
    factory: async_sessionmaker[AsyncSession],
    initial_message: InternalMessage,
    *,
    session_id: str,
    stream: bool,
    show_tool_calls: bool,
    execution_resume_state: dict[str, Any] | None = None,
    execution_checkpoint_callback=None,
) -> dict[str, Any] | list[dict[str, Any]]:
    async with factory() as db:
        if stream:
            return [
                event
                async for event in ChatDispatcher.dispatch_stream(
                    db=db,
                    message=INITIAL_MESSAGE,
                    uid=UID,
                    session_id=session_id,
                    request_id=f"request-{session_id}",
                    session_source="ws",
                    persisted_initial_message=initial_message,
                    frozen_user_message_ids=[initial_message.id],
                    persisted_profile_id=PROFILE_ID,
                    show_tool_calls=show_tool_calls,
                    execution_resume_state=execution_resume_state,
                    execution_checkpoint_callback=execution_checkpoint_callback,
                )
            ]
        return await ChatDispatcher.dispatch(
            db=db,
            message=INITIAL_MESSAGE,
            uid=UID,
            session_id=session_id,
            session_source="http",
            persisted_initial_message=initial_message,
            frozen_user_message_ids=[initial_message.id],
            persisted_profile_id=PROFILE_ID,
            show_tool_calls=show_tool_calls,
            execution_resume_state=execution_resume_state,
            execution_checkpoint_callback=execution_checkpoint_callback,
        )


async def _list_messages(factory: async_sessionmaker[AsyncSession], session_id: str) -> list[Message]:
    """Return every persisted message, including internal control records."""
    async with factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        return list(result.scalars().all())


async def _list_control_messages(factory: async_sessionmaker[AsyncSession], session_id: str) -> list[Message]:
    messages = await _list_messages(factory, session_id)
    return [message for message in messages if _is_end_session_control_message(message)]


def _is_end_session_control_message(message: Message | InternalMessage) -> bool:
    return isinstance(message.provider_metadata, dict) and message.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME


def _internal_control_messages(messages: list[InternalMessage]) -> list[InternalMessage]:
    return [message for message in messages if _is_end_session_control_message(message)]


def _assert_end_session_control_pair(
    messages: list[Message] | list[InternalMessage],
    *,
    skip_reply: bool,
    call_id: str,
    call_arguments: dict[str, Any] | None = None,
) -> tuple[InternalMessage, InternalMessage]:
    parsed_messages = parse_db_messages_to_internal(messages) if messages and isinstance(messages[0], Message) else messages
    control_messages = _internal_control_messages(parsed_messages)
    assert len(control_messages) == 2
    assert len({message.id for message in control_messages}) == 2
    control_call = next(message for message in control_messages if message.role == MessageRole.ASSISTANT)
    control_result = next(message for message in control_messages if message.role == MessageRole.TOOL)
    assert control_call.content is None
    assert control_call.reasoning_content is None
    assert control_call.tool_calls and len(control_call.tool_calls) == 1
    tool_call = control_call.tool_calls[0]
    assert tool_call.id == call_id
    assert tool_call.name == END_SESSION_TOOL_NAME
    assert tool_call.arguments == (call_arguments if call_arguments is not None else {"skip_reply": skip_reply})
    assert control_result.content is not None
    assert control_result.reasoning_content is None
    assert control_result.tool_call_id == call_id
    assert json.loads(control_result.content) == {
        "status": "completed",
        "skip_reply": skip_reply,
        "received_result_task_ids": [],
    }
    return control_call, control_result


def _assert_end_session_model_pair(
    messages: list[InternalMessage],
    *,
    skip_reply: bool,
    call_id: str,
    call_arguments: dict[str, Any],
) -> tuple[InternalMessage, InternalMessage]:
    control_calls = [message for message in messages if message.role == MessageRole.ASSISTANT and any(tool.name == END_SESSION_TOOL_NAME for tool in (message.tool_calls or []))]
    assert len(control_calls) == 1
    control_call = control_calls[0]
    assert control_call.content is None
    assert control_call.reasoning_content is None
    assert control_call.tool_calls and len(control_call.tool_calls) == 1
    tool_call = control_call.tool_calls[0]
    assert tool_call.id == call_id
    assert tool_call.name == END_SESSION_TOOL_NAME
    assert tool_call.arguments == call_arguments
    control_results = [message for message in messages if message.role == MessageRole.TOOL and message.tool_call_id == call_id]
    assert len(control_results) == 1
    control_result = control_results[0]
    assert control_result.content is not None
    assert control_result.reasoning_content is None
    assert json.loads(control_result.content) == {
        "status": "completed",
        "skip_reply": skip_reply,
        "received_result_task_ids": [],
    }
    return control_call, control_result


def _assert_no_control_history(
    history: list[dict[str, Any]],
    *,
    forbidden_texts: tuple[str, ...] = (),
) -> None:
    serialized_history = json.dumps(history, ensure_ascii=False)
    assert "internal_control" not in serialized_history
    assert all(text not in serialized_history for text in forbidden_texts)
    assert all(item.get("role") != MessageRole.TOOL for item in history)
    assert all(not item.get("tool_calls") for item in history)


def _response_from_dispatch_result(result: dict[str, Any] | list[dict[str, Any]], *, stream: bool) -> dict[str, Any]:
    if not stream:
        assert isinstance(result, dict)
        return result
    assert isinstance(result, list)
    done_events = [event for event in result if event.get("type") == "done"]
    assert len(done_events) == 1
    response = done_events[0].get("response")
    assert isinstance(response, dict)
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("show_tool_calls", [False, True], ids=["hide-tools", "show-tools"])
@pytest.mark.parametrize(
    ("log_locale", "expected_log_message"),
    [
        ("zh", "[end_session_user] 第 1 轮 | LLM主动终止循环，进入最终回复阶段。"),
        ("en", "[end_session_user] turn 1 | LLM proactively terminated the loop; entering the final reply phase."),
    ],
    ids=["chinese", "english"],
)
async def test_end_session_is_stored_as_final_text_and_streamed_as_final_reply(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[dict[str, Any]],
    stream: bool,
    show_tool_calls: bool,
    log_locale: str,
    expected_log_message: str,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-{int(stream)}-{int(show_tool_calls)}"
    initial_message = await _seed_conversation(session_factory, session_id)
    checkpoints: list[dict[str, Any]] = []

    async def capture_checkpoint(checkpoint: dict[str, Any]) -> None:
        checkpoints.append(checkpoint)

    calls = _patch_llm(
        monkeypatch,
        [
            _end_session_response(
                call_id="final-end-session",
                content="这段正文不应展示或持久化",
            ),
            _final_response(),
        ],
        stream=stream,
    )

    locale_token = set_current_log_locale(log_locale)
    try:
        result = await _dispatch(
            session_factory,
            initial_message,
            session_id=session_id,
            stream=stream,
            show_tool_calls=show_tool_calls,
            execution_checkpoint_callback=capture_checkpoint,
        )
    finally:
        reset_current_log_locale(locale_token)
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == 2
    assert calls[0]["tool_choice"] == "auto"
    assert any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in calls[0]["tools"])
    assert calls[1]["tool_choice"] == "auto"
    assert calls[1]["tools"] == calls[0]["tools"]
    assert any(message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_FINAL_RESPONSE_PROMPT for message in calls[1]["messages"])
    assert all(message.content != GOAL_MODE_SYSTEM_PROMPT for message in calls[1]["messages"] if message.role == MessageRole.SYSTEM)
    _assert_end_session_model_pair(
        calls[1]["messages"],
        skip_reply=False,
        call_id="final-end-session",
        call_arguments={},
    )
    assert any(checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_FINALIZING for checkpoint in checkpoints)
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["history"][-1]["role"] == MessageRole.ASSISTANT
    assert response["history"][-1]["content"] == SUMMARY
    assert all(item.get("role") != MessageRole.TOOL for item in response["history"])
    assert all(not item.get("tool_calls") for item in response["history"])
    _assert_no_control_history(
        response["history"],
        forbidden_texts=("final-end-session", "这段正文不应展示或持久化", "结束前检查已完成"),
    )

    messages = await _list_messages(session_factory, session_id)
    assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 2
    assistant_text_messages = [message for message in assistant_messages if message.type == MessageType.TEXT]
    assert len(assistant_text_messages) == 1
    assert assistant_text_messages[0].content == SUMMARY
    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=False,
        call_id="final-end-session",
        call_arguments={},
    )
    control_ids = {message.id for message in control_messages}
    ordinary_tool_messages = [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} and message.id not in control_ids]
    assert ordinary_tool_messages == []

    if stream:
        events = result
        content_events = [event for event in events if event.get("type") == "content"]
        reasoning_events = [event for event in events if event.get("type") == "reasoning"]
        turn_end_events = [event for event in events if event.get("type") == "turn_end"]
        done_events = [event for event in events if event.get("type") == "done"]
        assert [event["content"] for event in content_events] == [SUMMARY]
        assert reasoning_events == []
        assert len(turn_end_events) == 1
        assert turn_end_events[0]["content"] == SUMMARY
        assert turn_end_events[0]["message_id"] == assistant_text_messages[0].id
        assert len(done_events) == 1
        assert done_events[0]["response"]["choices"][0]["message"]["content"] == SUMMARY
        _assert_no_control_history(
            [event for event in events if event.get("type") != "done"],
            forbidden_texts=("final-end-session", "这段正文不应展示或持久化", "结束前检查已完成"),
        )
        assert not [event for event in events if event.get("name") == END_SESSION_TOOL_NAME]

        agent_loop_start_events = [event for event in events if event.get("type") == "agent_loop_start" and event.get("turn") == 1]
        assert len(agent_loop_start_events) == 1

    termination_logs = [record for record in captured_logs if record["extra"].get("session_id") == session_id and record["extra"].get("tool_name") == END_SESSION_TOOL_NAME]
    assert len(termination_logs) == 1
    termination_log = termination_logs[0]
    termination_extra = termination_log["extra"]
    assert termination_log["level"].name == "INFO"
    assert termination_log["message"] == expected_log_message
    assert termination_extra["uid"] == UID
    assert termination_extra["session_id"] == session_id
    assert termination_extra["turn"] == 1
    assert termination_extra["response_id"] and isinstance(termination_extra["response_id"], str)
    assert termination_extra["tool_call_id"] == "final-end-session"
    assert termination_extra["tool_name"] == END_SESSION_TOOL_NAME
    assert termination_extra["execution_phase"] == GOAL_EXECUTION_PHASE_FINALIZING

    expected_final_log_message = t(
        "LOG_DISPATCHER_LLM_RESPONSE",
        locale=log_locale,
        username="end_session_user",
        turn=2,
        content=SUMMARY,
    )
    final_log_indices = [index for index, record in enumerate(captured_logs) if record["extra"].get("session_id") == session_id and record["message"] == expected_final_log_message]
    assert len(final_log_indices) == 1
    termination_log_index = next(index for index, record in enumerate(captured_logs) if record is termination_log)
    assert termination_log_index < final_log_indices[0]

    for forbidden_text in ("这段正文不应展示或持久化", "结束前检查已完成"):
        assert forbidden_text not in termination_log["message"]
        assert forbidden_text not in str(termination_extra)

    if stream:
        assert termination_extra["response_id"] == agent_loop_start_events[0]["response_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("goal_mode", [False, True], ids=["legacy-mode", "goal-mode"])
@pytest.mark.parametrize("skip_reply", [False, True], ids=["normal-reply", "skip-reply"])
async def test_end_session_skip_reply_signal_controls_final_generation_and_public_events(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    goal_mode: bool,
    skip_reply: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-skip-reply-{int(stream)}-{int(goal_mode)}-{int(skip_reply)}"
    initial_message = await _seed_conversation(session_factory, session_id, goal_mode=goal_mode)
    same_turn_content = "同轮正文不得泄漏"
    same_turn_reasoning = "同轮推理不得泄漏"
    checkpoints: list[dict[str, Any]] = []

    async def capture_checkpoint(checkpoint: dict[str, Any]) -> None:
        checkpoints.append(checkpoint)

    responses = [
        _end_session_response(
            call_id="skip-reply-end-session",
            arguments={"skip_reply": skip_reply},
            content=same_turn_content,
            reasoning=same_turn_reasoning,
        )
    ]
    if not skip_reply:
        responses.append(_final_response())
    calls = _patch_llm(monkeypatch, responses, stream=stream)

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
        execution_checkpoint_callback=capture_checkpoint,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == (1 if skip_reply else 2)
    assert all(call["tool_choice"] == "auto" for call in calls)
    assert all(any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in call["tools"]) for call in calls)
    if not skip_reply:
        _assert_end_session_model_pair(
            calls[1]["messages"],
            skip_reply=False,
            call_id="skip-reply-end-session",
            call_arguments={"skip_reply": False},
        )

    messages = await _list_messages(session_factory, session_id)
    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=skip_reply,
        call_id="skip-reply-end-session",
        call_arguments={"skip_reply": skip_reply},
    )
    control_ids = {message.id for message in control_messages}
    assert len(control_ids) == 2
    ordinary_tool_messages = [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} and message.id not in control_ids]
    assert ordinary_tool_messages == []

    async with session_factory() as db:
        internal_history = await message_crud.get_history(
            db,
            session_id=session_id,
            uid=UID,
            limit=100,
        )
        visible_history = await message_crud.get_history_paged(
            db,
            session_id=session_id,
            uid=UID,
            limit=100,
            include_tool_messages=True,
        )
    _assert_end_session_control_pair(
        internal_history,
        skip_reply=skip_reply,
        call_id="skip-reply-end-session",
        call_arguments={"skip_reply": skip_reply},
    )
    visible_ids = {message.id for message in visible_history}
    assert not control_ids & visible_ids
    assert all(not _is_end_session_control_message(message) for message in visible_history)

    assert checkpoints
    checkpoint_messages = [InternalMessage.model_validate(message) for message in checkpoints[-1].get("messages", [])]
    checkpoint_turn_messages = [InternalMessage.model_validate(message) for message in checkpoints[-1].get("turn_messages", [])]
    _assert_end_session_control_pair(
        _internal_control_messages(checkpoint_messages),
        skip_reply=skip_reply,
        call_id="skip-reply-end-session",
        call_arguments={"skip_reply": skip_reply},
    )
    assert not _internal_control_messages(checkpoint_turn_messages)
    assert {message.id for message in _internal_control_messages(checkpoint_messages)} == control_ids

    _assert_no_control_history(
        response["history"],
        forbidden_texts=("skip-reply-end-session", same_turn_content, same_turn_reasoning),
    )
    public_result = json.dumps(result, ensure_ascii=False)
    assert "internal_control" not in public_result
    assert "skip-reply-end-session" not in public_result
    assert same_turn_content not in public_result
    assert same_turn_reasoning not in public_result
    if skip_reply:
        assert response["skip_reply"] is True
        assert response["choices"] == []
        assert not [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
        if stream:
            assert not [event for event in result if event.get("type") in {"content", "reasoning", "agent_loop_output", "turn_end"}]
    else:
        assert response.get("skip_reply", False) is False
        assert response["choices"][0]["message"]["content"] == SUMMARY
        assert response["choices"][0]["finish_reason"] == "stop"
        assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
        assert len(assistant_text_rows) == 1
        assert assistant_text_rows[0].content == SUMMARY
        if stream:
            assert [event["content"] for event in result if event.get("type") == "content"] == [SUMMARY]
    if stream:
        _assert_no_control_history(
            result,
            forbidden_texts=("skip-reply-end-session", same_turn_content, same_turn_reasoning),
        )
        assert not [event for event in result if event.get("name") == END_SESSION_TOOL_NAME]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("show_tool_calls", [False, True], ids=["hide-tools", "show-tools"])
@pytest.mark.parametrize("goal_mode", [False, True], ids=["legacy-mode", "goal-mode"])
@pytest.mark.parametrize("empty_tool_calls", [None, []], ids=["none-tool-calls", "empty-tool-calls"])
@pytest.mark.parametrize("has_prior_tool_round", [False, True], ids=["without-tool-round", "with-tool-round"])
async def test_no_tool_call_round_returns_response_without_extra_final_generation(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    show_tool_calls: bool,
    goal_mode: bool,
    empty_tool_calls: list[InternalToolCall] | None,
    has_prior_tool_round: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-no-tool-final-{int(stream)}-{int(show_tool_calls)}-{int(goal_mode)}-{int(empty_tool_calls is None)}-{int(has_prior_tool_round)}"
    initial_message = await _seed_conversation(session_factory, session_id, goal_mode=goal_mode, max_turns=5)
    final_reasoning = "本轮无工具调用，直接返回最终回复"
    final_response = _final_response()
    final_response.message.tool_calls = empty_tool_calls
    final_response.message.reasoning_content = final_reasoning
    responses: list[InternalResponse] = []
    if has_prior_tool_round:
        responses.append(
            _response_for_tool_calls(
                [
                    InternalToolCall(
                        id="prior-todo-read",
                        name=MANAGE_TODO_TOOL_NAME,
                        arguments={"operation": "read"},
                    )
                ],
                reasoning=None,
            )
        )
    responses.append(final_response)
    calls = _patch_llm(monkeypatch, responses, stream=stream)

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=show_tool_calls,
    )
    response = _response_from_dispatch_result(result, stream=stream)
    expected_turn = 1 + int(has_prior_tool_round)

    assert len(calls) == expected_turn
    assert all(call["tool_choice"] == "auto" for call in calls)
    for call in calls:
        assert any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in call["tools"])

    choice = response["choices"][0]
    assert choice["message"]["content"] == SUMMARY
    assert choice["message"]["reasoning_content"] == final_reasoning
    assert choice["finish_reason"] == "stop"
    final_history_message = response["history"][-1]
    assert final_history_message["role"] == MessageRole.ASSISTANT
    assert final_history_message["content"] == SUMMARY
    assert final_history_message["reasoning_content"] == final_reasoning
    assert not final_history_message.get("tool_calls")

    messages = await _list_messages(session_factory, session_id)
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_call_rows) == len(tool_result_rows) == int(has_prior_tool_round)
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY
    assert assistant_text_rows[0].reasoning_content == final_reasoning
    ordinary_tool_calls = parse_db_messages_to_internal(tool_call_rows)
    assert all(tool.name != END_SESSION_TOOL_NAME for message in ordinary_tool_calls for tool in (message.tool_calls or []))
    for row in tool_result_rows:
        outer_payload = json.loads(row.content or "")
        inner_payload = json.loads(outer_payload["content"])
        assert inner_payload["status"] == "success"

    if stream:
        content_events = [event for event in result if event.get("type") == "content"]
        reasoning_events = [event for event in result if event.get("type") == "reasoning"]
        turn_end_events = [event for event in result if event.get("type") == "turn_end"]
        final_turn_end_events = [event for event in turn_end_events if event.get("content") == SUMMARY]
        done_events = [event for event in result if event.get("type") == "done"]
        assert [event["content"] for event in content_events] == [SUMMARY]
        assert [event["content"] for event in reasoning_events] == [final_reasoning]
        assert len(final_turn_end_events) == 1
        assert turn_end_events[-1]["turn"] == expected_turn
        assert final_turn_end_events[0]["message_id"] == assistant_text_rows[0].id
        assert final_turn_end_events[0]["reasoning_content"] == final_reasoning
        assert len(done_events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
async def test_end_session_checkpoint_failure_does_not_log_termination_or_generate_final_reply(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[dict[str, Any]],
    stream: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-checkpoint-failed-{int(stream)}"
    initial_message = await _seed_conversation(session_factory, session_id)
    calls = _patch_llm(
        monkeypatch,
        [_end_session_response(call_id="checkpoint-failed-end")],
        stream=stream,
    )

    async def fail_checkpoint(checkpoint: dict[str, Any]) -> None:
        if checkpoint.get("execution_phase") == GOAL_EXECUTION_PHASE_FINALIZING:
            raise RuntimeError("checkpoint save failed")

    if stream:
        result = await _dispatch(
            session_factory,
            initial_message,
            session_id=session_id,
            stream=True,
            show_tool_calls=True,
            execution_checkpoint_callback=fail_checkpoint,
        )
        error_events = [event for event in result if event.get("type") == "error"]
        assert len(error_events) == 1
        assert not [event for event in result if event.get("type") in {"done", "content", "turn_end", "tool_start", "tool_end"}]
    else:
        with pytest.raises(ServerException):
            await _dispatch(
                session_factory,
                initial_message,
                session_id=session_id,
                stream=False,
                show_tool_calls=True,
                execution_checkpoint_callback=fail_checkpoint,
            )

    assert len(calls) == 1
    termination_logs = [record for record in captured_logs if record["extra"].get("session_id") == session_id and record["extra"].get("tool_name") == END_SESSION_TOOL_NAME]
    assert not termination_logs

    messages = await _list_messages(session_factory, session_id)
    assert not [message for message in messages if message.role == MessageRole.ASSISTANT]
    assert not [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT}]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("show_tool_calls", [False, True], ids=["hide-tools", "show-tools"])
async def test_goal_mode_continues_beyond_configured_and_legacy_turn_limits_then_ends(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    show_tool_calls: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-goal-mode-limit-{int(stream)}-{int(show_tool_calls)}"
    initial_message = await _seed_conversation(
        session_factory,
        session_id,
        goal_mode=True,
        max_turns=1,
    )
    todo_responses = [
        _response_for_tool_calls(
            [
                InternalToolCall(
                    id=f"todo-read-{turn}",
                    name=MANAGE_TODO_TOOL_NAME,
                    arguments={"operation": "read"},
                )
            ]
        )
        for turn in range(1, 26)
    ]
    calls = _patch_llm(
        monkeypatch,
        [
            *todo_responses,
            _end_session_response(call_id="final-end-session"),
            _final_response(),
        ],
        stream=stream,
    )

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=show_tool_calls,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == 27
    assert all(call["tool_choice"] == "auto" for call in calls[:-1])
    assert calls[-1]["tool_choice"] == "auto"
    assert calls[-1]["tools"] == calls[0]["tools"]
    max_turns_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=1)
    for call in calls[:-1]:
        assert any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in call["tools"])
        goal_mode_messages = [message for message in call["messages"] if message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_SYSTEM_PROMPT]
        assert len(goal_mode_messages) == 1
        for message in call["messages"]:
            if message.role == MessageRole.USER:
                user_payload = json.loads(message.content)
                assert user_payload.get("user_message") != max_turns_notice
    assert any(message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_FINAL_RESPONSE_PROMPT for message in calls[-1]["messages"])

    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"

    messages = await _list_messages(session_factory, session_id)
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=False,
        call_id="final-end-session",
        call_arguments={},
    )
    control_ids = {message.id for message in control_messages}
    ordinary_tool_call_rows = [message for message in tool_call_rows if message.id not in control_ids]
    ordinary_tool_result_rows = [message for message in tool_result_rows if message.id not in control_ids]
    assert len(ordinary_tool_call_rows) == 25
    assert len(ordinary_tool_result_rows) == 25
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY
    for row in ordinary_tool_result_rows:
        outer_payload = json.loads(row.content or "")
        inner_payload = json.loads(outer_payload["content"])
        assert inner_payload["status"] == "success"

    if not show_tool_calls:
        assert all(item.get("role") != MessageRole.TOOL for item in response["history"])
        assert all(not item.get("tool_calls") for item in response["history"])
        assistant_history = [item for item in response["history"] if item.get("role") == MessageRole.ASSISTANT]
        assert [item.get("content") for item in assistant_history] == [SUMMARY]

    if stream:
        content_events = [event for event in result if event.get("type") == "content"]
        turn_end_events = [event for event in result if event.get("type") == "turn_end"]
        assert [event["content"] for event in content_events] == [SUMMARY]
        assert turn_end_events[-1]["turn"] == 27
        assert turn_end_events[-1]["content"] == SUMMARY
        assert turn_end_events[-1]["message_id"] == assistant_text_rows[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("max_turns", [3, 21], ids=["three-turns", "twenty-one-turns"])
async def test_non_goal_mode_stops_at_configured_turn_limit_then_ends(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    max_turns: int,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-non-goal-mode-limit-{max_turns}-{int(stream)}"
    initial_message = await _seed_conversation(
        session_factory,
        session_id,
        goal_mode=False,
        max_turns=max_turns,
    )
    calls = _patch_llm(
        monkeypatch,
        [
            *[
                _response_for_tool_calls(
                    [
                        InternalToolCall(
                            id=f"todo-read-{turn}",
                            name=MANAGE_TODO_TOOL_NAME,
                            arguments={"operation": "read"},
                        )
                    ]
                )
                for turn in range(1, max_turns)
            ],
            InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=SUMMARY),
                model=MODEL_ID,
                finish_reason="stop",
            ),
        ],
        stream=stream,
    )

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == max_turns
    assert all(call["tool_choice"] == "auto" for call in calls)
    assert all(call["tools"] == calls[0]["tools"] for call in calls)
    assert any(tool["function"]["name"] == MANAGE_TODO_TOOL_NAME for tool in calls[0]["tools"])
    assert all(any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in call["tools"]) for call in calls)
    assert all(message.content != GOAL_MODE_SYSTEM_PROMPT for call in calls for message in call["messages"] if message.role == MessageRole.SYSTEM)
    max_turns_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
    user_payloads = [[json.loads(message.content)["user_message"] for message in call["messages"] if message.role == MessageRole.USER] for call in calls]
    assert all(max_turns_notice not in payloads for payloads in user_payloads[:-1])
    assert user_payloads[-1].count(max_turns_notice) == 1

    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"

    messages = await _list_messages(session_factory, session_id)
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_call_rows) == max_turns - 1
    assert len(tool_result_rows) == max_turns - 1
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY

    if stream:
        content_events = [event for event in result if event.get("type") == "content"]
        turn_end_events = [event for event in result if event.get("type") == "turn_end"]
        assert [event["content"] for event in content_events] == [SUMMARY]
        assert turn_end_events[-1]["turn"] == max_turns
        assert turn_end_events[-1]["content"] == SUMMARY
        assert turn_end_events[-1]["message_id"] == assistant_text_rows[0].id


@pytest.mark.asyncio
async def test_external_source_non_goal_mode_honors_configured_turn_limit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = "end-session-external-source-limit"
    initial_message = await _seed_conversation(
        session_factory,
        session_id,
        source="external",
        goal_mode=False,
        max_turns=3,
    )
    calls = _patch_llm(
        monkeypatch,
        [
            *[
                _response_for_tool_calls(
                    [
                        InternalToolCall(
                            id=f"external-todo-read-{turn}",
                            name=MANAGE_TODO_TOOL_NAME,
                            arguments={"operation": "read"},
                        )
                    ]
                )
                for turn in range(1, 3)
            ],
            InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=SUMMARY),
                model=MODEL_ID,
                finish_reason="stop",
            ),
        ],
        stream=False,
    )

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=False,
        show_tool_calls=True,
    )
    response = _response_from_dispatch_result(result, stream=False)

    async with session_factory() as db:
        session = await db.get(ChatSession, session_id)
    assert session is not None
    assert session.source == "external"
    assert (session.goal_mode, session.max_turns) == (False, 3)

    assert len(calls) == 3
    assert all(call["tool_choice"] == "auto" for call in calls)
    assert all(call["tools"] == calls[0]["tools"] for call in calls)
    assert any(tool["function"]["name"] == MANAGE_TODO_TOOL_NAME for tool in calls[0]["tools"])
    assert all(any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in call["tools"]) for call in calls)
    assert all(message.content != GOAL_MODE_SYSTEM_PROMPT for call in calls for message in call["messages"] if message.role == MessageRole.SYSTEM)
    max_turns_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=3)
    user_payloads = [[json.loads(message.content)["user_message"] for message in call["messages"] if message.role == MessageRole.USER] for call in calls]
    assert [payloads.count(max_turns_notice) for payloads in user_payloads] == [0, 0, 1]
    assert response["choices"][0]["message"]["content"] == SUMMARY


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("goal_mode", [False, True], ids=["legacy-mode", "goal-mode"])
async def test_execution_resume_state_restarts_over_limit_and_preserves_mode_contract(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    goal_mode: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-resume-state-{int(stream)}-{int(goal_mode)}"
    initial_message = await _seed_conversation(
        session_factory,
        session_id,
        goal_mode=goal_mode,
        max_turns=1,
    )
    resume_messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="保存的系统规则"),
        InternalMessage(role=MessageRole.SYSTEM, content=GOAL_MODE_SYSTEM_PROMPT),
        InternalMessage(role=MessageRole.SYSTEM, content=GOAL_MODE_SYSTEM_PROMPT),
        initial_message,
    ]
    execution_resume_state = {
        "current_turn": 25,
        "messages": [message.model_dump(mode="json") for message in resume_messages],
        "turn_messages": [],
        "files_to_user": [],
    }
    final_content = SUMMARY if goal_mode else "恢复普通模式后的总结"
    responses = [_end_session_response(call_id="resumed-end"), _final_response(final_content)] if goal_mode else [_final_response(final_content)]
    calls = _patch_llm(monkeypatch, responses, stream=stream)

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
        execution_resume_state=execution_resume_state,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == (2 if goal_mode else 1)
    assert calls[0]["tool_choice"] == "auto"
    assert any(tool["function"]["name"] == MANAGE_TODO_TOOL_NAME for tool in calls[0]["tools"])
    has_end_session = any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in calls[0]["tools"])
    assert has_end_session
    goal_mode_messages = [message for message in calls[0]["messages"] if message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_SYSTEM_PROMPT]
    assert len(goal_mode_messages) == (1 if goal_mode else 0)
    if goal_mode:
        assert calls[1]["tool_choice"] == "auto"
        assert calls[1]["tools"] == calls[0]["tools"]
        assert any(message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_FINAL_RESPONSE_PROMPT for message in calls[1]["messages"])
    max_turns_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=1)
    user_payloads = [json.loads(message.content)["user_message"] for message in calls[0]["messages"] if message.role == MessageRole.USER]
    assert user_payloads.count(max_turns_notice) == (0 if goal_mode else 1)

    assert response["choices"][0]["message"]["content"] == final_content
    assert response["choices"][0]["finish_reason"] == "stop"

    messages = await _list_messages(session_factory, session_id)
    user_rows = [message for message in messages if message.role == MessageRole.USER]
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(user_rows) == 1
    control_messages = await _list_control_messages(session_factory, session_id)
    if goal_mode:
        _assert_end_session_control_pair(
            control_messages,
            skip_reply=False,
            call_id="resumed-end",
            call_arguments={},
        )
        control_ids = {message.id for message in control_messages}
        assert len([message for message in tool_call_rows if message.id not in control_ids]) == 0
        assert len([message for message in tool_result_rows if message.id not in control_ids]) == 0
    else:
        assert control_messages == []
        assert not tool_call_rows
        assert not tool_result_rows
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == final_content

    if stream:
        content_events = [event for event in result if event.get("type") == "content"]
        turn_end_events = [event for event in result if event.get("type") == "turn_end"]
        assert [event["content"] for event in content_events] == [final_content]
        assert turn_end_events[-1]["turn"] == (27 if goal_mode else 1)
        assert turn_end_events[-1]["content"] == final_content
        assert turn_end_events[-1]["message_id"] == assistant_text_rows[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
async def test_finalizing_resume_skips_tools_and_generates_only_final_reply(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-finalizing-resume-{int(stream)}"
    initial_message = await _seed_conversation(session_factory, session_id, goal_mode=True)
    execution_resume_state = {
        "current_turn": 6,
        "execution_phase": GOAL_EXECUTION_PHASE_FINALIZING,
        "messages": [initial_message.model_dump(mode="json")],
        "turn_messages": [],
        "files_to_user": [],
    }
    calls = _patch_llm(monkeypatch, [_final_response()], stream=stream)

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
        execution_resume_state=execution_resume_state,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == 1
    assert calls[0]["tool_choice"] == "auto"
    assert any(tool["function"]["name"] == MANAGE_TODO_TOOL_NAME for tool in calls[0]["tools"])
    assert any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in calls[0]["tools"])
    assert any(message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_FINAL_RESPONSE_PROMPT for message in calls[0]["messages"])
    assert all(message.content != GOAL_MODE_SYSTEM_PROMPT for message in calls[0]["messages"] if message.role == MessageRole.SYSTEM)
    assert response["choices"][0]["message"]["content"] == SUMMARY

    messages = await _list_messages(session_factory, session_id)
    assert not [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT}]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("goal_mode", [False, True], ids=["max-turn-final", "goal-finalizing"])
@pytest.mark.parametrize("illegal_rounds", [1, 3], ids=["one-illegal-round", "three-illegal-rounds"])
async def test_text_only_final_reply_never_executes_provider_tool_calls(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    goal_mode: bool,
    illegal_rounds: int,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"final-tool-call-forbidden-{int(stream)}-{int(goal_mode)}"
    initial_message = await _seed_conversation(
        session_factory,
        session_id,
        goal_mode=goal_mode,
        max_turns=1,
    )
    execution_resume_state = None
    if goal_mode:
        execution_resume_state = {
            "current_turn": 6,
            "execution_phase": GOAL_EXECUTION_PHASE_FINALIZING,
            "messages": [initial_message.model_dump(mode="json")],
            "turn_messages": [],
            "files_to_user": [],
        }
    forbidden_texts: list[str] = []
    forbidden_call_ids: dict[int, list[str]] = {}
    responses: list[InternalResponse] = []
    for attempt in range(1, illegal_rounds + 1):
        todo_call_id = f"forbidden-final-todo-{attempt}"
        shell_call_id = f"forbidden-final-shell-{attempt}"
        forbidden_call_ids[attempt] = [todo_call_id, shell_call_id]
        forbidden_texts.extend([f"违规最终正文-{attempt}", f"违规最终推理-{attempt}"])
        responses.append(
            _response_for_tool_calls(
                [
                    _todo_write_call(todo_call_id),
                    InternalToolCall(
                        id=shell_call_id,
                        name="execute_shell",
                        arguments={"command": f"echo forbidden-{attempt}", "run_in_background": True},
                    ),
                ],
                content=f"违规最终正文-{attempt}",
                reasoning=f"违规最终推理-{attempt}",
            )
        )
    responses.append(_final_response("纠正后的最终回复"))
    calls = _patch_llm(monkeypatch, responses, stream=stream)

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
        execution_resume_state=execution_resume_state,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == illegal_rounds + 1
    assert all(call["tool_choice"] == "auto" for call in calls)
    assert all(call["tools"] == calls[0]["tools"] for call in calls)
    assert any(tool["function"]["name"] == MANAGE_TODO_TOOL_NAME for tool in calls[0]["tools"])
    assert any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in calls[0]["tools"])
    assert any(message.role == MessageRole.USER and INITIAL_MESSAGE in (message.content or "") for message in calls[0]["messages"])
    earlier_call_ids: list[str] = []
    for correction_attempt in range(1, illegal_rounds + 1):
        earlier_call_ids.extend(forbidden_call_ids[correction_attempt])
        correction_tool_results = [message for message in calls[correction_attempt]["messages"] if message.role == MessageRole.TOOL and message.tool_call_id in earlier_call_ids]
        assert {message.tool_call_id for message in correction_tool_results} == set(earlier_call_ids)
        assert len(correction_tool_results) == len(earlier_call_ids)
        for tool_result in correction_tool_results:
            feedback = json.loads(tool_result.content or "{}")
            assert feedback["status"] == "rejected"
            assert feedback["executed"] is False
            assert TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT in (tool_result.content or "")
    assert response["choices"][0]["message"]["content"] == "纠正后的最终回复"
    public_result = json.dumps(result, ensure_ascii=False)
    assert all(text not in public_result for text in forbidden_texts)
    messages = await _list_messages(session_factory, session_id)
    assert not [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT}]
    assistant_rows = [message for message in messages if message.role == MessageRole.ASSISTANT]
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == "纠正后的最终回复"
    assert all(text not in (assistant_rows[0].content or "") for text in forbidden_texts)
    async with session_factory() as db:
        assert await db.get(SessionTodoPlan, session_id) is None
        assert not (await db.execute(select(BackgroundTask))).scalars().all()
    if stream:
        assert [event["content"] for event in result if event.get("type") == "content"] == ["纠正后的最终回复"]
        assert not [event for event in result if event.get("type") in {"tool_start", "tool_end"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("goal_mode", [False, True], ids=["max-turn-final", "goal-finalizing"])
async def test_text_only_final_reply_stops_after_three_failed_corrections(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    goal_mode: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"final-tool-call-correction-exhausted-{int(stream)}-{int(goal_mode)}"
    initial_message = await _seed_conversation(
        session_factory,
        session_id,
        goal_mode=goal_mode,
        max_turns=1,
    )
    execution_resume_state = None
    if goal_mode:
        execution_resume_state = {
            "current_turn": 6,
            "execution_phase": GOAL_EXECUTION_PHASE_FINALIZING,
            "messages": [initial_message.model_dump(mode="json")],
            "turn_messages": [],
            "files_to_user": [],
        }
    forbidden_texts: list[str] = []
    responses: list[InternalResponse] = []
    forbidden_call_ids: dict[int, list[str]] = {}
    for attempt in range(1, FINAL_REPLY_TOOL_CORRECTION_MAX_ATTEMPTS + 2):
        todo_call_id = f"forbidden-final-todo-{attempt}"
        shell_call_id = f"forbidden-final-shell-{attempt}"
        forbidden_call_ids[attempt] = [todo_call_id, shell_call_id]
        forbidden_texts.extend([f"错误最终正文-{attempt}", f"错误最终推理-{attempt}"])
        responses.append(
            _response_for_tool_calls(
                [
                    _todo_write_call(todo_call_id),
                    InternalToolCall(
                        id=shell_call_id,
                        name="execute_shell",
                        arguments={"command": f"echo forbidden-{attempt}", "run_in_background": True},
                    ),
                ],
                content=f"错误最终正文-{attempt}",
                reasoning=f"错误最终推理-{attempt}",
            )
        )
    calls = _patch_llm(monkeypatch, responses, stream=stream)

    if stream:
        result = await _dispatch(
            session_factory,
            initial_message,
            session_id=session_id,
            stream=True,
            show_tool_calls=True,
            execution_resume_state=execution_resume_state,
        )
        error_events = [event for event in result if event.get("type") == "error"]
        assert len(error_events) == 1
        assert error_events[0]["message"] == t(ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED)
        assert not [event for event in result if event.get("type") in {"content", "reasoning", "turn_end", "tool_start", "tool_end"}]
    else:
        with pytest.raises(LLMModelCapabilityException) as exc_info:
            await _dispatch(
                session_factory,
                initial_message,
                session_id=session_id,
                stream=False,
                show_tool_calls=True,
                execution_resume_state=execution_resume_state,
            )
        assert exc_info.value.render_message() == t(ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED)

    assert len(calls) == FINAL_REPLY_TOOL_CORRECTION_MAX_ATTEMPTS + 1 == 4
    assert all(call["tool_choice"] == "auto" for call in calls)
    assert all(call["tools"] == calls[0]["tools"] for call in calls)
    assert any(tool["function"]["name"] == MANAGE_TODO_TOOL_NAME for tool in calls[0]["tools"])
    assert any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in calls[0]["tools"])
    assert any(message.role == MessageRole.USER and INITIAL_MESSAGE in (message.content or "") for message in calls[0]["messages"])
    earlier_call_ids: list[str] = []
    for correction_attempt in range(1, FINAL_REPLY_TOOL_CORRECTION_MAX_ATTEMPTS + 1):
        earlier_call_ids.extend(forbidden_call_ids[correction_attempt])
        correction_tool_results = [message for message in calls[correction_attempt]["messages"] if message.role == MessageRole.TOOL and message.tool_call_id in earlier_call_ids]
        assert {message.tool_call_id for message in correction_tool_results} == set(earlier_call_ids)
        assert len(correction_tool_results) == len(earlier_call_ids)
        for tool_result in correction_tool_results:
            feedback = json.loads(tool_result.content or "{}")
            assert feedback["status"] == "rejected"
            assert feedback["executed"] is False
            assert TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT in (tool_result.content or "")
    if stream:
        assert all(text not in json.dumps(result, ensure_ascii=False) for text in forbidden_texts)
    else:
        assert all(text not in exc_info.value.render_message() for text in forbidden_texts)
    messages = await _list_messages(session_factory, session_id)
    assert not [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT}]
    assert not [message for message in messages if message.role == MessageRole.ASSISTANT]
    async with session_factory() as db:
        assert await db.get(SessionTodoPlan, session_id) is None
        assert not (await db.execute(select(BackgroundTask))).scalars().all()


@pytest.mark.asyncio
async def test_invalid_execution_phase_fails_before_any_llm_request(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = "end-session-invalid-execution-phase"
    initial_message = await _seed_conversation(session_factory, session_id, goal_mode=True)
    execution_resume_state = {
        "current_turn": 6,
        "execution_phase": "unknown-phase",
        "messages": [initial_message.model_dump(mode="json")],
        "turn_messages": [],
        "files_to_user": [],
    }
    calls = _patch_llm(monkeypatch, [], stream=False)

    with pytest.raises(ServerException):
        await _dispatch(
            session_factory,
            initial_message,
            session_id=session_id,
            stream=False,
            show_tool_calls=True,
            execution_resume_state=execution_resume_state,
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize(
    ("arguments", "case_id"),
    [
        ({"summary": SUMMARY}, "legacy-summary"),
        ({"unexpected": "argument"}, "extra-argument"),
    ],
)
async def test_invalid_end_session_call_is_corrected_and_failed_result_is_persisted(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    arguments: dict[str, Any],
    case_id: str,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-invalid-{case_id}-{int(stream)}"
    initial_message = await _seed_conversation(session_factory, session_id)
    invalid_call = InternalToolCall(
        id=f"invalid-end-{case_id}",
        name=END_SESSION_TOOL_NAME,
        arguments=arguments,
    )
    calls = _patch_llm(
        monkeypatch,
        [
            _response_for_tool_calls([invalid_call]),
            _end_session_response(
                call_id="corrected-end-session",
            ),
            _final_response(),
        ],
        stream=stream,
    )

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == 3
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["history"][-1]["content"] == SUMMARY

    messages = await _list_messages(session_factory, session_id)
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=False,
        call_id="corrected-end-session",
        call_arguments={},
    )
    control_ids = {message.id for message in control_messages}
    ordinary_tool_call_rows = [message for message in tool_call_rows if message.id not in control_ids]
    ordinary_tool_result_rows = [message for message in tool_result_rows if message.id not in control_ids]
    assert len(ordinary_tool_call_rows) == len(ordinary_tool_result_rows) == 1
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY

    ordinary_tool_calls = parse_db_messages_to_internal(ordinary_tool_call_rows)
    assert [tool.name for message in ordinary_tool_calls for tool in (message.tool_calls or [])] == [END_SESSION_TOOL_NAME]
    stored_tool_result = json.loads(ordinary_tool_result_rows[0].content or "")
    assert stored_tool_result["tool_call_id"] == invalid_call.id
    failed_payload = json.loads(stored_tool_result["content"])
    assert failed_payload["status"] == "failed"
    assert failed_payload["tool_name"] == END_SESSION_TOOL_NAME
    assert isinstance(failed_payload.get("error"), str) and failed_payload["error"]
    assert stored_tool_result["content"].startswith("{")
    assert stored_tool_result["content"].endswith("}")

    if stream:
        assert [event["content"] for event in result if event.get("type") == "content"] == [SUMMARY]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize(
    ("tool_calls", "case_id"),
    [
        (
            [
                InternalToolCall(id="end-first", name=END_SESSION_TOOL_NAME, arguments={}),
                InternalToolCall(id="end-second", name=END_SESSION_TOOL_NAME, arguments={}),
            ],
            "two-end-session-calls",
        ),
        (
            [
                InternalToolCall(id="end-before-todo", name=END_SESSION_TOOL_NAME, arguments={}),
                _todo_write_call("todo-after-end"),
            ],
            "end-before-todo",
        ),
        (
            [
                _todo_write_call("todo-before-end"),
                InternalToolCall(id="end-after-todo", name=END_SESSION_TOOL_NAME, arguments={}),
            ],
            "todo-before-end",
        ),
    ],
)
async def test_end_session_protocol_conflict_persists_all_failures_without_executing_todo(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    tool_calls: list[InternalToolCall],
    case_id: str,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-protocol-{case_id}-{int(stream)}"
    initial_message = await _seed_conversation(session_factory, session_id, max_parallel_tools=1)
    calls = _patch_llm(
        monkeypatch,
        [
            _response_for_tool_calls(tool_calls),
            _end_session_response(
                call_id="corrected-end-session",
            ),
            _final_response(),
        ],
        stream=stream,
    )

    result = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
    )
    response = _response_from_dispatch_result(result, stream=stream)

    assert len(calls) == 3
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"

    messages = await _list_messages(session_factory, session_id)
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=False,
        call_id="corrected-end-session",
        call_arguments={},
    )
    control_ids = {message.id for message in control_messages}
    ordinary_tool_call_rows = [message for message in tool_call_rows if message.id not in control_ids]
    ordinary_tool_result_rows = [message for message in tool_result_rows if message.id not in control_ids]
    assert len(ordinary_tool_call_rows) == 1
    assert len(ordinary_tool_result_rows) == len(tool_calls)
    ordinary_tool_calls = parse_db_messages_to_internal(ordinary_tool_call_rows)
    assert {tool.id for message in ordinary_tool_calls for tool in (message.tool_calls or [])} == {tool_call.id for tool_call in tool_calls}
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY
    persisted_by_call_id = {}
    for row in ordinary_tool_result_rows:
        outer_payload = json.loads(row.content or "")
        inner_payload = json.loads(outer_payload["content"])
        persisted_by_call_id[outer_payload["tool_call_id"]] = inner_payload
        assert inner_payload["status"] == "failed"
        assert isinstance(inner_payload.get("error"), str) and inner_payload["error"]
        assert outer_payload["content"].startswith("{")
        assert outer_payload["content"].endswith("}")
    assert set(persisted_by_call_id) == {tool_call.id for tool_call in tool_calls}
    assert all(persisted_by_call_id[tool_call.id]["tool_name"] == tool_call.name for tool_call in tool_calls)
    assert any(payload.get("round_execution_policy") == "exclusive" for payload in persisted_by_call_id.values() if payload.get("tool_name") == END_SESSION_TOOL_NAME)

    async with session_factory() as db:
        todo_plan = await db.get(SessionTodoPlan, session_id)
    assert todo_plan is None

    if stream:
        assert [event["content"] for event in result if event.get("type") == "content"] == [SUMMARY]


@pytest.mark.asyncio
async def test_queue_stream_persists_end_session_final_events_and_deduplicated_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    session_id = "end-session-queue-stream"
    initial_message, seeded_work = await _seed_running_work(session_factory, session_id)
    calls = _patch_llm(
        monkeypatch,
        [
            _end_session_response(
                call_id="queue-end-session",
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            ),
            _final_response(),
        ],
        stream=True,
    )

    async with session_factory() as db:
        work = await db.get(SessionReplyWorkItem, seeded_work.id)
        assert work is not None
        response = await executor_interactive_module._dispatch_interactive_work(
            db,
            work=work,
            worker_id="worker-1",
            message=INITIAL_MESSAGE,
            initial_message=initial_message,
            history_before_id=initial_message.id,
            frozen_user_message_ids=[initial_message.id],
            attachments=None,
            allow_additional_user_messages=False,
            execution_resume_state=None,
        )

    assert len(calls) == 2
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"
    _assert_end_session_model_pair(
        calls[1]["messages"],
        skip_reply=False,
        call_id="queue-end-session",
        call_arguments={},
    )

    async with session_factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        messages = list(result.scalars().all())
        event_result = await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == seeded_work.id).order_by(SessionReplyStreamEvent.sequence_no.asc()))
        stream_events = list(event_result.scalars().all())
        persisted_work = await db.get(SessionReplyWorkItem, seeded_work.id)

    assert persisted_work is not None
    assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 2
    assistant_text_messages = [message for message in assistant_messages if message.type == MessageType.TEXT]
    assert len(assistant_text_messages) == 1
    assistant_message = assistant_text_messages[0]
    assert assistant_message.type == MessageType.TEXT
    assert assistant_message.content == SUMMARY
    assert assistant_message.dedupe_key == _result_message_dedupe_key(persisted_work)
    control_messages = [message for message in messages if _is_end_session_control_message(message)]
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=False,
        call_id="queue-end-session",
        call_arguments={},
    )
    control_ids = {message.id for message in control_messages}
    assert [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} and message.id not in control_ids] == []

    content_events = [row.event for row in stream_events if row.event.get("type") == "content"]
    turn_end_events = [row.event for row in stream_events if row.event.get("type") == "turn_end"]
    assert [event["content"] for event in content_events] == [SUMMARY]
    assert len(turn_end_events) == 1
    assert turn_end_events[0]["content"] == SUMMARY
    assert turn_end_events[0]["message_id"] == assistant_message.id
    assert turn_end_events[0]["response_id"] == response["response_id"]
    _assert_no_control_history(
        [row.event for row in stream_events],
        forbidden_texts=("queue-end-session",),
    )
    assert not [row for row in stream_events if row.event.get("name") == END_SESSION_TOOL_NAME]


@pytest.mark.asyncio
async def test_queue_skip_reply_completes_silently_and_is_idempotent_on_reclaim(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    notifier_events: list[tuple[str, str, dict[str, Any]]] = []

    async def capture_notifier(uid: str, session_id: str, event: dict[str, Any]) -> None:
        notifier_events.append((uid, session_id, event))

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_notifier)
    session_id = "end-session-queue-skip-reply"
    _initial_message, seeded_work = await _seed_running_work(session_factory, session_id)
    calls = _patch_llm(
        monkeypatch,
        [
            _end_session_response(
                call_id="queue-skip-end-session",
                arguments={"skip_reply": True},
                content="队列同轮正文不得泄漏",
                reasoning="队列同轮推理不得泄漏",
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
        ],
        stream=True,
    )

    await executor_lifecycle_module.execute_session_reply_work(seeded_work.id, "worker-1")

    async with session_factory() as db:
        messages_result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        messages = list(messages_result.scalars().all())
        events_result = await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id.asc()))
        session_events = list(events_result.scalars().all())
        persisted_work = await db.get(SessionReplyWorkItem, seeded_work.id)

    assert persisted_work is not None
    work_identity = (persisted_work.id, persisted_work.sequence_no)
    assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert persisted_work.result_message_id is None
    saved_response = (persisted_work.execution_state or {}).get("response")
    assert isinstance(saved_response, dict)
    assert saved_response["skip_reply"] is True
    assert saved_response["choices"] == []
    assert len(session_events) == 1
    assert session_events[0].event["type"] == "done"
    assert session_events[0].event["skip_reply"] is True
    assert session_events[0].event["work_id"] == work_identity[0]
    assert session_events[0].event["work_sequence_no"] == work_identity[1]
    assert notifier_events == []
    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=True,
        call_id="queue-skip-end-session",
        call_arguments={"skip_reply": True},
    )
    control_ids = {message.id for message in control_messages}
    assert not [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert not [message for message in messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} and message.id not in control_ids]

    result_response = await session_reply_queue_manager.wait_for_result(seeded_work.id)
    assert result_response["skip_reply"] is True
    assert result_response["choices"] == []
    assert (result_response["work_id"], result_response["work_sequence_no"]) == work_identity

    async def collect_stream() -> list[dict[str, Any]]:
        return [event async for event in session_reply_queue_manager.wait_for_stream(seeded_work.id)]

    stream_events = await asyncio.wait_for(collect_stream(), timeout=2)
    assert stream_events
    assert stream_events[-1]["type"] == "done"
    assert stream_events[-1]["response"]["skip_reply"] is True
    _assert_no_control_history(
        stream_events,
        forbidden_texts=("queue-skip-end-session", "队列同轮正文不得泄漏", "队列同轮推理不得泄漏"),
    )
    assert not [event for event in stream_events if event.get("name") == END_SESSION_TOOL_NAME]

    async with session_factory() as db:
        reclaimed_work = await db.get(SessionReplyWorkItem, seeded_work.id)
        assert reclaimed_work is not None
        reclaimed_work.status = SessionReplyWorkStatus.RUNNING
        reclaimed_work.locked_by = "worker-1"
        reclaimed_work.lock_until = 2_000_000_000
        await db.commit()

    await executor_lifecycle_module.execute_session_reply_work(seeded_work.id, "worker-1")
    assert len(calls) == 1

    async with session_factory() as db:
        messages_result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        retried_messages = list(messages_result.scalars().all())
        events_result = await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id.asc()))
        retried_events = list(events_result.scalars().all())
        retried_work = await db.get(SessionReplyWorkItem, seeded_work.id)

    assert retried_work is not None
    assert (retried_work.id, retried_work.sequence_no) == work_identity
    assert retried_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert retried_work.result_message_id is None
    retried_response = (retried_work.execution_state or {}).get("response")
    assert isinstance(retried_response, dict)
    assert retried_response["skip_reply"] is True
    retried_control_messages = [message for message in retried_messages if _is_end_session_control_message(message)]
    _assert_end_session_control_pair(
        retried_control_messages,
        skip_reply=True,
        call_id="queue-skip-end-session",
        call_arguments={"skip_reply": True},
    )
    assert len(retried_events) == 1
    assert retried_events[0].event["type"] == "done"
    retried_control_ids = {message.id for message in retried_control_messages}
    assert len(retried_control_ids) == 2
    assert retried_control_ids == control_ids
    assert not [message for message in retried_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert not [message for message in retried_messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} and message.id not in retried_control_ids]
    assert notifier_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
async def test_end_session_context_is_loaded_for_model_in_subsequent_turn_while_hidden_from_frontend(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-context-{int(stream)}"
    initial_message = await _seed_conversation(session_factory, session_id)

    calls = _patch_llm(
        monkeypatch,
        [
            _end_session_response(
                call_id="turn1-end-session",
                arguments={"skip_reply": True},
            ),
            _final_response("第二轮模型回复"),
        ],
        stream=stream,
    )

    result_turn1 = await _dispatch(
        session_factory,
        initial_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
    )
    response_turn1 = _response_from_dispatch_result(result_turn1, stream=stream)

    assert response_turn1["skip_reply"] is True
    _assert_no_control_history(response_turn1["history"], forbidden_texts=("turn1-end-session",))
    if stream:
        _assert_no_control_history(result_turn1, forbidden_texts=("turn1-end-session",))
        assert not [event for event in result_turn1 if event.get("name") == END_SESSION_TOOL_NAME]

    async with session_factory() as db:
        visible_history_turn1 = await message_crud.get_history_paged(
            db,
            session_id=session_id,
            uid=UID,
            limit=100,
            include_tool_messages=True,
        )
    assert all(not _is_end_session_control_message(message) for message in visible_history_turn1)

    async with session_factory() as db:
        second_message = Message(
            session_id=session_id,
            uid=UID,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="第二轮用户提问",
            profile_id=PROFILE_ID,
            is_processed=False,
        )
        db.add(second_message)
        await db.commit()
        await db.refresh(second_message)
        second_internal_message = InternalMessage(
            id=second_message.id,
            role=MessageRole.USER,
            content=second_message.content,
            created_at=second_message.created_at.timestamp(),
        )

    result_turn2 = await _dispatch(
        session_factory,
        second_internal_message,
        session_id=session_id,
        stream=stream,
        show_tool_calls=True,
    )
    response_turn2 = _response_from_dispatch_result(result_turn2, stream=stream)

    assert len(calls) == 2
    model_turn2_messages = calls[1]["messages"]
    _assert_end_session_model_pair(
        model_turn2_messages,
        skip_reply=True,
        call_id="turn1-end-session",
        call_arguments={"skip_reply": True},
    )

    assert response_turn2["choices"][0]["message"]["content"] == "第二轮模型回复"
    _assert_no_control_history(response_turn2["history"], forbidden_texts=("turn1-end-session",))

    async with session_factory() as db:
        visible_history_all = await message_crud.get_history_paged(
            db,
            session_id=session_id,
            uid=UID,
            limit=100,
            include_tool_messages=True,
        )
    assert all(not _is_end_session_control_message(message) for message in visible_history_all)
