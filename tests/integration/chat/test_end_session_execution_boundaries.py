import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    END_SESSION_TOOL_NAME,
    ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED,
    FINAL_REPLY_TOOL_CORRECTION_MAX_ATTEMPTS,
    GOAL_EXECUTION_PHASE_FINALIZING,
    MANAGE_TODO_TOOL_NAME,
)
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.exceptions import LLMModelCapabilityException, ServerException
from app.core.i18n import t
from app.core.prompts import (
    GOAL_MODE_FINAL_RESPONSE_PROMPT,
    GOAL_MODE_SYSTEM_PROMPT,
    PROMPT_MAX_TURNS_REACHED,
    TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT,
)
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.background_task import BackgroundTask
from app.models.message import (
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    MessageRole,
    MessageType,
)
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan
from tests.integration.chat.test_end_session_workflow import (
    INITIAL_MESSAGE,
    MODEL_ID,
    PROFILE_ID,
    SUMMARY,
    UID,
    _assert_end_session_control_pair,
    _dispatch,
    _end_session_response,
    _final_response,
    _list_control_messages,
    _list_messages,
    _patch_llm,
    _patch_runtime_database,
    _response_for_tool_calls,
    _response_from_dispatch_result,
    _seed_conversation,
    _todo_write_call,
)
from tests.integration.chat.test_end_session_workflow import session_factory as _session_factory

session_factory = _session_factory


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
            feedback = json.loads(tool_result.content or "{}")["tool_result"]
            assert feedback["status"] == "rejected"
            assert feedback["executed"] is False
            assert feedback["instruction"] == TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT
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
            feedback = json.loads(tool_result.content or "{}")["tool_result"]
            assert feedback["status"] == "rejected"
            assert feedback["executed"] is False
            assert feedback["instruction"] == TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT
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
async def test_task_execution_scope_ends_session_without_pulling_foreground_user_messages(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = "end-session-task-scope"
    initial_message = await _seed_conversation(session_factory, session_id)

    new_msg = InternalMessage(role=MessageRole.USER, content="前台并发新消息")
    batch = UserInputBatch(messages=(new_msg,), source_message_ids=(1,))
    additional_user_messages_fetcher = AsyncMock(return_value=batch)

    calls = _patch_llm(
        monkeypatch,
        [
            _end_session_response(
                call_id="task-end-session",
                arguments={"skip_reply": False},
            ),
            _final_response("任务执行总结"),
        ],
        stream=False,
    )

    async with session_factory() as db:
        result = await ChatDispatcher.dispatch(
            db=db,
            message=INITIAL_MESSAGE,
            uid=UID,
            session_id=session_id,
            session_source="worker",
            execution_scope=END_SESSION_SCOPE_TASK,
            additional_user_messages_fetcher=additional_user_messages_fetcher,
            persisted_initial_message=initial_message,
            frozen_user_message_ids=[initial_message.id],
            persisted_profile_id=PROFILE_ID,
            show_tool_calls=True,
        )

    assert additional_user_messages_fetcher.await_count == 0
    assert len(calls) == 2
    assert result["choices"][0]["message"]["content"] == "任务执行总结"
    assert result["choices"][0]["finish_reason"] == "stop"

    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=False,
        call_id="task-end-session",
        call_arguments={"skip_reply": False},
    )
    assert len(control_messages) == 2
    for message in control_messages:
        assert isinstance(message.provider_metadata, dict)
        assert message.provider_metadata["execution_scope"] == END_SESSION_SCOPE_TASK

    messages = await _list_messages(session_factory, session_id)
    assistant_text_messages = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(assistant_text_messages) == 1
    assert assistant_text_messages[0].content == "任务执行总结"


@pytest.mark.asyncio
async def test_task_execution_scope_ends_session_with_skip_reply(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_database(monkeypatch, session_factory)
    session_id = "end-session-task-scope-skip-reply"
    initial_message = await _seed_conversation(session_factory, session_id)

    calls = _patch_llm(
        monkeypatch,
        [
            _end_session_response(
                call_id="task-skip-end-session",
                arguments={"skip_reply": True},
            ),
        ],
        stream=False,
    )

    async with session_factory() as db:
        result = await ChatDispatcher.dispatch(
            db=db,
            message=INITIAL_MESSAGE,
            uid=UID,
            session_id=session_id,
            session_source="worker",
            execution_scope=END_SESSION_SCOPE_TASK,
            persisted_initial_message=initial_message,
            frozen_user_message_ids=[initial_message.id],
            persisted_profile_id=PROFILE_ID,
            show_tool_calls=True,
        )

    assert len(calls) == 1
    assert result.get("skip_reply") is True
    assert result["choices"] == []

    control_messages = await _list_control_messages(session_factory, session_id)
    _assert_end_session_control_pair(
        control_messages,
        skip_reply=True,
        call_id="task-skip-end-session",
        call_arguments={"skip_reply": True},
    )
    assert len(control_messages) == 2
    for message in control_messages:
        assert isinstance(message.provider_metadata, dict)
        assert message.provider_metadata.get("execution_scope") == END_SESSION_SCOPE_TASK

    messages = await _list_messages(session_factory, session_id)
    assistant_text_messages = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert not assistant_text_messages
