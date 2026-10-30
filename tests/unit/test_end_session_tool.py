import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import (
    END_SESSION_SCOPE_FOREGROUND,
    END_SESSION_SCOPE_TASK,
    END_SESSION_SCOPES,
)
from app.core.tools import END_SESSION_TOOL_NAME, get_tools_for_profile
from app.core.tools.background_tasks import MANAGE_BACKGROUND_TASKS_TOOL_SCHEMA
from app.core.tools.end_session import (
    END_SESSION_TOOL_SCHEMA,
    is_end_session_signal,
    is_skip_reply_signal,
    persist_end_session_control,
    resolve_end_session_scope,
)
from app.core.utils.dispatcher.process_single_tool import (
    prevalidate_tool_round,
    prevalidate_tool_round_protocol,
)
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import Profile, ProfileConfig


def _profile_without_configured_tools() -> Profile:
    return Profile(
        id=None,
        uid="user-1",
        name="end-session-test-profile",
        configs={"tool": {"enabled_tools": []}},
    )


def _config_with_one_parallel_slot() -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "tool": {
                "enabled_tools": [],
                "max_parallel_tools": 1,
            }
        }
    )


def _end_session_call(call_id: str = "end-session-call", arguments: dict[str, object] | None = None) -> InternalToolCall:
    return InternalToolCall(
        id=call_id,
        name=END_SESSION_TOOL_NAME,
        arguments={} if arguments is None else arguments,
    )


def _ordinary_call(call_id: str = "ordinary-call") -> InternalToolCall:
    return InternalToolCall(
        id=call_id,
        name=MANAGE_BACKGROUND_TASKS_TOOL_SCHEMA["function"]["name"],
        arguments={"operation": "list"},
    )


def _message_with_tool_calls(
    tool_calls: list[InternalToolCall] | None,
    message_type: str,
) -> dict[str, object] | InternalMessage:
    if message_type == "dict":
        return {
            "tool_calls": None if tool_calls is None else [tool_call.model_dump() for tool_call in tool_calls],
        }
    return InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls)


@pytest.mark.asyncio
async def test_goal_mode_exposes_end_session_and_parameterless_call_passes_prevalidation():
    profile = _profile_without_configured_tools()
    original_configs = copy.deepcopy(profile.configs)

    tools, whitelist = await get_tools_for_profile(None, profile, goal_mode=True)

    assert END_SESSION_TOOL_NAME in {tool["function"]["name"] for tool in tools}
    assert whitelist == []
    errors = prevalidate_tool_round(
        [_end_session_call()],
        ProfileConfig.model_validate(profile.configs),
        goal_mode=True,
        tool_schemas=tools,
    )
    assert errors == {}
    assert profile.configs == original_configs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configs", "goal_mode", "expected_exposed"),
    [
        ({"tool": {"enabled_tools": []}}, False, True),
        ({"tool": {"enabled_tools": []}}, True, True),
        ({"tool": {"goal_mode": False, "enabled_tools": []}}, False, True),
        ({"tool": {"goal_mode": False, "enabled_tools": []}}, True, True),
        (
            {"tool": {"goal_mode": False, "enabled_tools": [END_SESSION_TOOL_NAME]}},
            False,
            True,
        ),
        ({"tool": {"goal_mode": True, "enabled_tools": []}}, False, True),
        ({"tool": {"goal_mode": True, "enabled_tools": []}}, True, True),
        ({"tool": {"goal_mode": True, "enabled_tools": ["write_file"]}}, True, True),
        ({"goal_mode": True, "enabled_tools": []}, True, True),
    ],
    ids=[
        "missing-old-field-normal-mode",
        "missing-old-field-goal-mode",
        "old-normal-mode",
        "old-normal-field-goal-mode",
        "normal-mode-explicit-end",
        "old-goal-mode-normal-field",
        "goal-mode",
        "goal-mode-with-configured-tool",
        "flat-goal-mode",
    ],
)
async def test_end_session_exposure_is_available_in_all_conversation_modes(configs, goal_mode, expected_exposed):
    original_configs = copy.deepcopy(configs)
    profile = Profile(
        id=None,
        uid="user-1",
        name="end-session-exposure-profile",
        configs=copy.deepcopy(configs),
    )

    tools, _ = await get_tools_for_profile(None, profile, goal_mode=goal_mode)

    assert (END_SESSION_TOOL_NAME in {tool["function"]["name"] for tool in tools}) is expected_exposed
    assert profile.configs == original_configs
    assert configs == original_configs


def test_normal_mode_prevalidation_accepts_end_session_when_enabled_and_schema_present():
    call = _end_session_call()
    config = ProfileConfig.model_validate(
        {
            "tool": {
                "enabled_tools": [END_SESSION_TOOL_NAME],
                "goal_mode": False,
                "max_parallel_tools": 1,
            }
        }
    )

    errors = prevalidate_tool_round(
        [call],
        config,
        tool_schemas=[END_SESSION_TOOL_SCHEMA],
    )

    assert errors == {}


@pytest.mark.parametrize(
    "arguments",
    [
        {"skip_reply": " true"},
        {"skip_reply": 1},
        {"skip_reply": None},
        {"unknown": "value"},
    ],
    ids=["skip-reply-leading-space", "skip-reply-integer", "skip-reply-null", "unknown-argument"],
)
def test_invalid_end_session_arguments_return_failed_protocol_result(arguments):
    call = InternalToolCall(
        id="invalid-end-session-call",
        name=END_SESSION_TOOL_NAME,
        arguments=arguments,
    )

    errors = prevalidate_tool_round(
        [call],
        _config_with_one_parallel_slot(),
        goal_mode=True,
        tool_schemas=[END_SESSION_TOOL_SCHEMA],
    )

    assert call.id in errors
    payload = json.loads(errors[call.id])
    assert payload["status"] == "failed"
    assert payload["tool_name"] == END_SESSION_TOOL_NAME
    assert payload["error"]


@pytest.mark.parametrize(
    ("tool_calls", "end_call_id"),
    [
        (
            [_end_session_call("end-first"), _ordinary_call("ordinary-second")],
            "end-first",
        ),
        (
            [_ordinary_call("ordinary-first"), _end_session_call("end-second")],
            "end-second",
        ),
    ],
    ids=["end-first", "end-second"],
)
def test_protocol_precheck_rejects_end_session_mixed_round_regardless_of_order(tool_calls, end_call_id):
    errors = prevalidate_tool_round_protocol(tool_calls, _config_with_one_parallel_slot())

    assert end_call_id in errors
    payload = json.loads(errors[end_call_id])
    assert payload["status"] == "failed"
    assert payload["tool_name"] == END_SESSION_TOOL_NAME
    assert payload["round_execution_policy"] == "exclusive"


def test_protocol_precheck_rejects_two_end_session_calls_in_one_round():
    calls = [_end_session_call("end-1"), _end_session_call("end-2")]

    errors = prevalidate_tool_round_protocol(calls, _config_with_one_parallel_slot())

    assert set(errors) == {"end-1", "end-2"}
    for call in calls:
        payload = json.loads(errors[call.id])
        assert payload["status"] == "failed"
        assert payload["tool_name"] == END_SESSION_TOOL_NAME
        assert payload["round_execution_policy"] == "exclusive"


def test_goal_mode_protocol_precheck_accepts_a_single_end_session_call():
    errors = prevalidate_tool_round_protocol(
        [_end_session_call()],
        _config_with_one_parallel_slot(),
    )
    assert errors == {}


@pytest.mark.parametrize(
    "message_type",
    ["dict", "internal"],
    ids=["dictionary", "internal-message"],
)
@pytest.mark.parametrize(
    ("arguments", "expected_skip_reply"),
    [
        ({}, False),
        ({"skip_reply": False}, False),
        ({"skip_reply": True}, True),
    ],
    ids=["default", "skip-reply-false", "skip-reply-true"],
)
def test_end_session_signals_accept_supported_arguments(arguments, expected_skip_reply, message_type):
    message = _message_with_tool_calls([_end_session_call(arguments=arguments)], message_type)

    assert is_end_session_signal(message) is True
    assert is_skip_reply_signal(message) is expected_skip_reply


@pytest.mark.parametrize(
    "message_type",
    ["dict", "internal"],
    ids=["dictionary", "internal-message"],
)
@pytest.mark.parametrize(
    "arguments",
    [
        {"skip_reply": " true"},
        {"skip_reply": 1},
        {"skip_reply": None},
        {"unknown": "value"},
    ],
    ids=["skip-reply-leading-space", "skip-reply-integer", "skip-reply-null", "unknown-argument"],
)
def test_invalid_end_session_arguments_are_rejected_by_signals(arguments, message_type):
    message = _message_with_tool_calls([_end_session_call(arguments=arguments)], message_type)

    assert is_end_session_signal(message) is False
    assert is_skip_reply_signal(message) is False


@pytest.mark.parametrize(
    "message_type",
    ["dict", "internal"],
    ids=["dictionary", "internal-message"],
)
@pytest.mark.parametrize(
    "tool_calls",
    [
        None,
        [],
        [_end_session_call(arguments={"summary": "completed"})],
        [_end_session_call(), _end_session_call("second")],
        [_ordinary_call("ordinary-only")],
    ],
    ids=["no-tools", "empty-tools", "legacy-summary", "multiple-tools", "other-tool"],
)
def test_non_matching_tool_round_is_not_end_session_signal(tool_calls, message_type):
    message = _message_with_tool_calls(tool_calls, message_type)

    assert is_end_session_signal(message) is False
    assert is_skip_reply_signal(message) is False


def test_resolve_end_session_scope():
    for scope in END_SESSION_SCOPES:
        assert resolve_end_session_scope(execution_scope=scope) == scope
    assert resolve_end_session_scope(session_source="queue", execution_scope=END_SESSION_SCOPE_FOREGROUND) == END_SESSION_SCOPE_FOREGROUND
    assert resolve_end_session_scope(session_source="http", execution_scope=END_SESSION_SCOPE_TASK) == END_SESSION_SCOPE_TASK

    for source in ("http", "ws"):
        assert resolve_end_session_scope(session_source=source) == END_SESSION_SCOPE_FOREGROUND
        assert resolve_end_session_scope(session_source=source, call_context="chat") == END_SESSION_SCOPE_FOREGROUND

    task_keywords = ("task", "subagent", "worker", "background", "scheduled", "agent")
    for keyword in task_keywords:
        assert resolve_end_session_scope(session_source=keyword) == END_SESSION_SCOPE_TASK
        assert resolve_end_session_scope(call_context=keyword) == END_SESSION_SCOPE_TASK
        assert resolve_end_session_scope(session_source="ws", call_context=f"{keyword}_run") == END_SESSION_SCOPE_TASK
        assert resolve_end_session_scope(session_source=f"{keyword}_trigger", call_context="chat") == END_SESSION_SCOPE_TASK

    assert resolve_end_session_scope() == END_SESSION_SCOPE_FOREGROUND
    assert resolve_end_session_scope(session_source=None, call_context=None, execution_scope=None) == END_SESSION_SCOPE_FOREGROUND


@pytest.mark.asyncio
async def test_persist_end_session_control_records_execution_scope(monkeypatch):
    mock_db = MagicMock()
    mock_saved_call = SimpleNamespace(id="saved-call-1", created_at=datetime.now(UTC))
    mock_saved_result = SimpleNamespace(id="saved-result-2", created_at=datetime.now(UTC))
    mock_save = AsyncMock(side_effect=[mock_saved_call, mock_saved_result])
    monkeypatch.setattr("app.core.utils.dispatcher.save_message.save_message", mock_save)

    incoming_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[_end_session_call(arguments={"skip_reply": True})],
        provider_metadata={"trace_id": "test-trace"},
    )

    call_message, result_message = await persist_end_session_control(
        mock_db,
        uid="user-test-1",
        session_id="session-test-1",
        profile_id=10,
        message=incoming_message,
        skip_reply=True,
        received_task_ids=[101, 102],
        dedupe_scope="scope-key",
        execution_scope=END_SESSION_SCOPE_TASK,
    )

    assert mock_save.await_count == 2
    saved_call_message = mock_save.await_args_list[0].args[5]
    saved_result_message = mock_save.await_args_list[1].args[5]

    assert saved_call_message.provider_metadata["execution_scope"] == END_SESSION_SCOPE_TASK
    assert saved_result_message.provider_metadata["execution_scope"] == END_SESSION_SCOPE_TASK
    assert call_message.provider_metadata["execution_scope"] == END_SESSION_SCOPE_TASK
    assert result_message.provider_metadata["execution_scope"] == END_SESSION_SCOPE_TASK

    payload = json.loads(result_message.content)
    assert payload["status"] == "completed"
    assert payload["skip_reply"] is True
    assert payload["received_result_task_ids"] == [101, 102]
    assert {"status", "skip_reply", "received_result_task_ids"}.issubset(payload.keys())
