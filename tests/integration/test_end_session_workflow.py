import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatcher as dispatcher_module
from app.core.constants import END_SESSION_TOOL_NAME, MANAGE_TODO_TOOL_NAME
from app.core.dispatcher import ChatDispatcher
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.prompts import GOAL_MODE_SYSTEM_PROMPT, PROMPT_MAX_TURNS_REACHED
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
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
    summary: str,
    *,
    call_id: str,
    reasoning: str | None = "结束前检查已完成",
    usage: dict[str, Any] | None = None,
) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            reasoning_content=reasoning,
            tool_calls=[
                InternalToolCall(
                    id=call_id,
                    name=END_SESSION_TOOL_NAME,
                    arguments={"summary": summary},
                )
            ],
        ),
        model=MODEL_ID,
        usage=usage or {"prompt_tokens": 31, "completion_tokens": 17, "total_tokens": 48},
        finish_reason="tool_calls",
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
) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
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
        )


async def _list_messages(factory: async_sessionmaker[AsyncSession], session_id: str) -> list[Message]:
    async with factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        return list(result.scalars().all())


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
async def test_end_session_is_stored_as_final_text_and_streamed_as_final_reply(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    show_tool_calls: bool,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = f"end-session-{int(stream)}-{int(show_tool_calls)}"
    initial_message = await _seed_conversation(session_factory, session_id)
    calls = _patch_llm(
        monkeypatch,
        [
            _end_session_response(
                SUMMARY,
                call_id="final-end-session",
            )
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

    assert len(calls) == 1
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["history"][-1]["role"] == MessageRole.ASSISTANT
    assert response["history"][-1]["content"] == SUMMARY
    assert all(item.get("role") != MessageRole.TOOL for item in response["history"])
    assert all(not item.get("tool_calls") for item in response["history"])

    messages = await _list_messages(session_factory, session_id)
    assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].type == MessageType.TEXT
    assert assistant_messages[0].content == SUMMARY
    assert all(message.type not in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} for message in messages)

    if stream:
        events = result
        content_events = [event for event in events if event.get("type") == "content"]
        turn_end_events = [event for event in events if event.get("type") == "turn_end"]
        done_events = [event for event in events if event.get("type") == "done"]
        assert [event["content"] for event in content_events] == [SUMMARY]
        assert len(turn_end_events) == 1
        assert turn_end_events[0]["content"] == SUMMARY
        assert turn_end_events[0]["message_id"] == assistant_messages[0].id
        assert len(done_events) == 1
        assert done_events[0]["response"]["choices"][0]["message"]["content"] == SUMMARY


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
            _end_session_response(SUMMARY, call_id="final-end-session"),
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

    assert len(calls) == 26
    assert all(call["tool_choice"] == "required" for call in calls)
    max_turns_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=1)
    for call in calls:
        assert any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in call["tools"])
        goal_mode_messages = [message for message in call["messages"] if message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_SYSTEM_PROMPT]
        assert len(goal_mode_messages) == 1
        for message in call["messages"]:
            if message.role == MessageRole.USER:
                user_payload = json.loads(message.content)
                assert user_payload.get("user_message") != max_turns_notice

    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"

    messages = await _list_messages(session_factory, session_id)
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_call_rows) == 25
    assert len(tool_result_rows) == 25
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY
    for row in tool_result_rows:
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
        assert turn_end_events[-1]["turn"] == 26
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
    assert [call["tool_choice"] for call in calls] == ["auto"] * (max_turns - 1) + ["none"]
    assert all(all(tool["function"]["name"] != END_SESSION_TOOL_NAME for tool in call["tools"]) for call in calls)
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
    assert [call["tool_choice"] for call in calls] == ["auto", "auto", "none"]
    assert all(all(tool["function"]["name"] != END_SESSION_TOOL_NAME for tool in call["tools"]) for call in calls)
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
    responses = [
        _end_session_response(SUMMARY, call_id="resumed-end")
        if goal_mode
        else InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=final_content),
            model=MODEL_ID,
            finish_reason="stop",
        )
    ]
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

    assert len(calls) == 1
    assert calls[0]["tool_choice"] == ("required" if goal_mode else "none")
    has_end_session = any(tool["function"]["name"] == END_SESSION_TOOL_NAME for tool in calls[0]["tools"])
    assert has_end_session is goal_mode
    goal_mode_messages = [message for message in calls[0]["messages"] if message.role == MessageRole.SYSTEM and message.content == GOAL_MODE_SYSTEM_PROMPT]
    assert len(goal_mode_messages) == (1 if goal_mode else 0)
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
    assert not tool_call_rows
    assert not tool_result_rows
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == final_content

    if stream:
        content_events = [event for event in result if event.get("type") == "content"]
        turn_end_events = [event for event in result if event.get("type") == "turn_end"]
        assert [event["content"] for event in content_events] == [final_content]
        assert turn_end_events[-1]["turn"] == (26 if goal_mode else 1)
        assert turn_end_events[-1]["content"] == final_content
        assert turn_end_events[-1]["message_id"] == assistant_text_rows[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize(
    ("arguments", "case_id"),
    [
        ({}, "missing"),
        ({"summary": None}, "null"),
        ({"summary": ""}, "empty"),
        ({"summary": " \n\t "}, "whitespace"),
        ({"summary": SUMMARY, "unexpected": "argument"}, "extra-argument"),
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
                SUMMARY,
                call_id="corrected-end-session",
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

    assert len(calls) == 2
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["history"][-1]["content"] == SUMMARY

    messages = await _list_messages(session_factory, session_id)
    tool_call_rows = [message for message in messages if message.type == MessageType.TOOL_CALL]
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_call_rows) == len(tool_result_rows) == 1
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY

    stored_tool_result = json.loads(tool_result_rows[0].content or "")
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
                InternalToolCall(id="end-first", name=END_SESSION_TOOL_NAME, arguments={"summary": SUMMARY}),
                InternalToolCall(id="end-second", name=END_SESSION_TOOL_NAME, arguments={"summary": SUMMARY}),
            ],
            "two-end-session-calls",
        ),
        (
            [
                InternalToolCall(id="end-before-todo", name=END_SESSION_TOOL_NAME, arguments={"summary": SUMMARY}),
                _todo_write_call("todo-after-end"),
            ],
            "end-before-todo",
        ),
        (
            [
                _todo_write_call("todo-before-end"),
                InternalToolCall(id="end-after-todo", name=END_SESSION_TOOL_NAME, arguments={"summary": SUMMARY}),
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
                SUMMARY,
                call_id="corrected-end-session",
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

    assert len(calls) == 2
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"

    messages = await _list_messages(session_factory, session_id)
    tool_result_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    assistant_text_rows = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(tool_result_rows) == len(tool_calls)
    assert len(assistant_text_rows) == 1
    assert assistant_text_rows[0].content == SUMMARY
    persisted_by_call_id = {}
    for row in tool_result_rows:
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
                SUMMARY,
                call_id="queue-end-session",
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
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

    assert len(calls) == 1
    assert response["choices"][0]["message"]["content"] == SUMMARY
    assert response["choices"][0]["finish_reason"] == "stop"

    async with session_factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        messages = list(result.scalars().all())
        event_result = await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == seeded_work.id).order_by(SessionReplyStreamEvent.sequence_no.asc()))
        stream_events = list(event_result.scalars().all())
        persisted_work = await db.get(SessionReplyWorkItem, seeded_work.id)

    assert persisted_work is not None
    assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assistant_message = assistant_messages[0]
    assert assistant_message.type == MessageType.TEXT
    assert assistant_message.content == SUMMARY
    assert assistant_message.dedupe_key == _result_message_dedupe_key(persisted_work)

    content_events = [row.event for row in stream_events if row.event.get("type") == "content"]
    turn_end_events = [row.event for row in stream_events if row.event.get("type") == "turn_end"]
    assert [event["content"] for event in content_events] == [SUMMARY]
    assert len(turn_end_events) == 1
    assert turn_end_events[0]["content"] == SUMMARY
    assert turn_end_events[0]["message_id"] == assistant_message.id
    assert turn_end_events[0]["response_id"] == response["response_id"]
