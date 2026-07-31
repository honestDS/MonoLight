import json
from types import SimpleNamespace

import pytest

import app.core.tools.terminal as terminal_module
from app.core.dispatch_context import DispatchContext
from app.core.terminal import (
    ALL_TERMINAL_ACTIONS,
    TerminalAction,
    TerminalActionReceipt,
    TerminalOutputBufferState,
    TerminalOutputReadStatus,
    TerminalPermissionScope,
    TerminalReadResult,
    TerminalSessionSnapshot,
    TerminalSessionStatus,
)
from app.core.tools import (
    SHELL_COMPANION_TOOL_SCHEMAS,
    SHELL_TOOL_SCHEMA,
    get_tools_for_profile,
)
from app.core.tools.terminal import (
    TerminalCloseExecutor,
    TerminalReadExecutor,
    TerminalResizeExecutor,
    TerminalStatusExecutor,
    TerminalWriteExecutor,
)
from app.core.utils.dispatcher.process_single_tool import get_handed_off_terminal_session_id
from app.models.profile import Profile

TERMINAL_SESSION_ID = "t" * 32
_VALID_HANDOFF_PAYLOAD = {
    "terminal_session_id": TERMINAL_SESSION_ID,
    "status": "starting",
    "output_buffer": {
        "capacity_bytes": 1_048_576,
        "oldest_offset": 0,
        "next_offset": 0,
        "oldest_sequence": 1,
        "next_sequence": 1,
    },
    "output_stream": "merged_stdout_stderr",
}


def _profile(enabled_tools: list[str]) -> Profile:
    return Profile(
        id=7,
        uid="u1",
        name="terminal-test",
        configs={"tool": {"enabled_tools": enabled_tools}},
    )


def _snapshot() -> TerminalSessionSnapshot:
    return TerminalSessionSnapshot(
        terminal_session_id=TERMINAL_SESSION_ID,
        status=TerminalSessionStatus.RUNNING,
        permission_scope=TerminalPermissionScope(
            owner_uid="u1",
            owner_session_id="session-1",
            original_tool_call_id="tool-call-1",
            audit_record_id=1,
            audit_execution_record_id=2,
            allowed_actions=ALL_TERMINAL_ACTIONS,
        ),
        output_buffer=TerminalOutputBufferState(
            capacity_bytes=1_048_576,
            oldest_offset=0,
            next_offset=0,
            oldest_sequence=1,
            next_sequence=1,
        ),
    )


def test_get_handed_off_terminal_session_id_accepts_complete_handoff_result():
    assert get_handed_off_terminal_session_id(json.dumps(_VALID_HANDOFF_PAYLOAD)) == TERMINAL_SESSION_ID


@pytest.mark.parametrize(
    "tool_result",
    [
        "{",
        "[]",
        "null",
        json.dumps({"terminal_session_id": TERMINAL_SESSION_ID}),
        json.dumps({"status": "starting", "output_stream": "merged_stdout_stderr"}),
        json.dumps(
            {
                "terminal_session_id": TERMINAL_SESSION_ID,
                "status": "starting",
                "output_stream": "merged_stdout_stderr",
            }
        ),
        json.dumps({"terminal_session_id": TERMINAL_SESSION_ID, "status": "success", "output": "done"}),
        json.dumps({**_VALID_HANDOFF_PAYLOAD, "status": "exited"}),
        json.dumps({**_VALID_HANDOFF_PAYLOAD, "output_stream": "stdout"}),
    ],
)
def test_get_handed_off_terminal_session_id_rejects_non_handoff_results(tool_result):
    assert get_handed_off_terminal_session_id(tool_result) is None


def _executor(executor_class):
    executor = executor_class(project_root=".", uid="u1")
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode="interactive",
            source="test",
            uid="u1",
            session_id="session-1",
            profile=_profile(["execute_shell"]),
            db=object(),
            tool_call_id="tool-call-1",
        )
    )
    return executor


@pytest.mark.asyncio
async def test_get_tools_for_profile_exposes_shell_companions_only_with_shell():
    enabled_tools, _ = await get_tools_for_profile(None, _profile(["execute_shell"]))
    enabled_names = [tool["function"]["name"] for tool in enabled_tools]

    assert enabled_names == [
        SHELL_TOOL_SCHEMA["function"]["name"],
        *(schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS),
    ]
    assert set(schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS).isdisjoint(_profile(["execute_shell"]).configs["tool"]["enabled_tools"])

    disabled_tools, _ = await get_tools_for_profile(
        None,
        _profile([schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS]),
    )

    assert not any(name.startswith("terminal_") for name in (tool["function"]["name"] for tool in disabled_tools))


def test_terminal_tool_schemas_match_protocol_defaults_and_bounds():
    expected = {
        "terminal_status": {
            "required": ["terminal_session_id"],
            "properties": {"terminal_session_id": {}},
        },
        "terminal_read": {
            "required": ["terminal_session_id"],
            "properties": {
                "terminal_session_id": {},
                "offset": {"minimum": 0, "default": 0},
                "max_bytes": {"minimum": 1, "maximum": 1_048_576, "default": 65_536},
            },
        },
        "terminal_write": {
            "required": ["terminal_session_id", "data"],
            "properties": {"terminal_session_id": {}, "data": {"minLength": 1, "maxLength": 65_536}},
        },
        "terminal_resize": {
            "required": ["terminal_session_id", "columns", "rows"],
            "properties": {
                "terminal_session_id": {},
                "columns": {"minimum": 1, "maximum": 1_000},
                "rows": {"minimum": 1, "maximum": 1_000},
            },
        },
        "terminal_close": {
            "required": ["terminal_session_id"],
            "properties": {"terminal_session_id": {}, "force": {"default": False}},
        },
    }

    assert "terminal_signal" not in {schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS}
    for schema in SHELL_COMPANION_TOOL_SCHEMAS:
        name = schema["function"]["name"]
        parameters = schema["function"]["parameters"]
        assert parameters["additionalProperties"] is False
        assert parameters["required"] == expected[name]["required"]
        assert set(parameters["properties"]) == set(expected[name]["properties"])
        assert parameters["properties"]["terminal_session_id"] == {
            "type": "string",
            "minLength": 32,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9_-]+$",
        }
        for property_name, property_expectations in expected[name]["properties"].items():
            properties = parameters["properties"][property_name]
            for field, value in property_expectations.items():
                assert properties[field] == value


@pytest.mark.asyncio
async def test_terminal_executors_use_protocol_results_and_stable_request_ids(monkeypatch):
    snapshot = _snapshot()
    read_result = TerminalReadResult(
        terminal_session_id=TERMINAL_SESSION_ID,
        read_status=TerminalOutputReadStatus.EMPTY,
        requested_offset=0,
        start_offset=0,
        next_offset=0,
        oldest_available_offset=0,
        latest_offset=0,
        sequence=0,
        output="",
        eof=False,
    )
    control_requests = []

    async def get_snapshot(*args, **kwargs):
        return snapshot

    async def enqueue_read(*args, **kwargs):
        return SimpleNamespace(id=101), True

    async def enqueue_control(*args, **kwargs):
        control_requests.append(args[3])
        return SimpleNamespace(id=102), False

    async def wait_for_command_result(*args, **kwargs):
        return read_result.model_dump(mode="json")

    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_snapshot", get_snapshot)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_read", enqueue_read)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_control", enqueue_control)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "wait_for_command_result", wait_for_command_result)

    status_result = json.loads(await _executor(TerminalStatusExecutor).execute(TERMINAL_SESSION_ID))
    assert TerminalSessionSnapshot.model_validate(status_result).status is TerminalSessionStatus.RUNNING

    read_executor = _executor(TerminalReadExecutor)
    read_payload = json.loads(await read_executor.execute(TERMINAL_SESSION_ID, offset=0, max_bytes=65_536))
    assert TerminalReadResult.model_validate(read_payload) == read_result

    action_executors = [
        (TerminalWriteExecutor, {"terminal_session_id": TERMINAL_SESSION_ID, "data": "input"}, TerminalAction.WRITE),
        (
            TerminalResizeExecutor,
            {"terminal_session_id": TERMINAL_SESSION_ID, "columns": 100, "rows": 40},
            TerminalAction.RESIZE,
        ),
        (TerminalCloseExecutor, {"terminal_session_id": TERMINAL_SESSION_ID, "force": True}, TerminalAction.CLOSE),
    ]
    request_ids = {}

    for executor_class, arguments, action in action_executors:
        executor = _executor(executor_class)
        request_ids[action] = executor._derive_request_id(action)
        assert len(request_ids[action]) == 64
        receipt = TerminalActionReceipt.model_validate(json.loads(await executor.execute(**arguments)))
        assert receipt.action is action
        assert receipt.duplicate is True
        assert receipt.request_id == request_ids[action]

        repeated_receipt = TerminalActionReceipt.model_validate(json.loads(await executor.execute(**arguments)))
        assert repeated_receipt.model_dump(mode="json") == receipt.model_dump(mode="json")

    assert request_ids[TerminalAction.WRITE] == _executor(TerminalWriteExecutor)._derive_request_id(TerminalAction.WRITE)
    assert len(set(request_ids.values())) == len(request_ids)
    all_action_request_ids = {action: _executor(TerminalStatusExecutor)._derive_request_id(action) for action in TerminalAction}
    assert all(len(request_id) == 64 for request_id in all_action_request_ids.values())
    assert len(set(all_action_request_ids.values())) == len(all_action_request_ids)
    assert [request.action for request in control_requests] == [
        TerminalAction.WRITE,
        TerminalAction.WRITE,
        TerminalAction.RESIZE,
        TerminalAction.RESIZE,
        TerminalAction.CLOSE,
        TerminalAction.CLOSE,
    ]
