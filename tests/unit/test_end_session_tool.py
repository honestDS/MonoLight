import copy
import json

import pytest

from app.core.log import LogManager
from app.core.tools import END_SESSION_TOOL_NAME, get_tools_for_profile
from app.core.tools.end_session import END_SESSION_TOOL_SCHEMA, is_end_session_signal
from app.core.tools.list_background_tasks import LIST_BACKGROUND_TASKS_TOOL_SCHEMA
from app.core.utils.dispatcher.process_single_tool import (
    prevalidate_tool_round,
    prevalidate_tool_round_protocol,
    process_single_tool,
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
        name=LIST_BACKGROUND_TASKS_TOOL_SCHEMA["function"]["name"],
        arguments={},
    )


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
        ({"tool": {"enabled_tools": []}}, False, False),
        ({"tool": {"enabled_tools": []}}, True, True),
        ({"tool": {"goal_mode": False, "enabled_tools": []}}, False, False),
        ({"tool": {"goal_mode": False, "enabled_tools": []}}, True, True),
        (
            {"tool": {"goal_mode": False, "enabled_tools": [END_SESSION_TOOL_NAME]}},
            False,
            False,
        ),
        ({"tool": {"goal_mode": True, "enabled_tools": []}}, False, False),
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
async def test_end_session_exposure_requires_goal_mode(configs, goal_mode, expected_exposed):
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


def test_normal_mode_prevalidation_rejects_end_session_even_when_enabled_and_schema_present():
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

    assert call.id in errors
    payload = json.loads(errors[call.id])
    assert payload["status"] == "failed"
    assert payload["tool_name"] == END_SESSION_TOOL_NAME
    assert payload["error"]


@pytest.mark.asyncio
async def test_normal_mode_process_single_tool_rejects_end_session(monkeypatch):
    monkeypatch.setattr(LogManager, "log_tool_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(LogManager, "log_tool_result", lambda *args, **kwargs: None)
    profile = _profile_without_configured_tools()

    result = await process_single_tool(
        _end_session_call(),
        None,
        profile,
        ProfileConfig.model_validate(profile.configs),
        [],
        "user-1",
        "session-1",
        1,
        "user-1",
    )

    payload = json.loads(result.content)
    assert payload["status"] == "failed"
    assert payload["tool_name"] == END_SESSION_TOOL_NAME
    assert payload["error"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"summary": "completed"},
        {"unexpected": "value"},
    ],
    ids=["legacy-summary", "unexpected-argument"],
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


def test_end_session_signal_requires_one_parameterless_call():
    message = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[_end_session_call()])

    assert is_end_session_signal(message) is True


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
def test_non_matching_tool_round_is_not_end_session_signal(tool_calls):
    message = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls)

    assert is_end_session_signal(message) is False
