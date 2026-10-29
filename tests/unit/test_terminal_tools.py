import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.terminal.watcher as terminal_watcher_module
import app.core.tools.terminal as terminal_module
from app.core.constants import ERR_TERMINAL_COMMAND_TIMEOUT
from app.core.dispatchers.context import DispatchContext
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
    TerminalWriteResult,
)
from app.core.terminal.pty_base import PtyResourceSnapshot
from app.core.terminal.session_runtime_commands import TerminalSessionRuntimeCommands
from app.core.terminal.watch_schema import TerminalWatchRequest
from app.core.tools import (
    SHELL_COMPANION_TOOL_SCHEMAS,
    SHELL_TOOL_SCHEMA,
    SYSTEM_BUILTIN_TOOL_NAMES,
    get_tools_for_profile,
)
from app.core.tools.terminal import (
    TERMINAL_WRITE_TOOL_SCHEMA,
    TerminalCloseExecutor,
    TerminalReadExecutor,
    TerminalResizeExecutor,
    TerminalWriteExecutor,
)
from app.core.utils.dispatcher.process_single_tool import get_handed_off_terminal_session_id
from app.models.profile import Profile, ProfileConfig
from app.models.terminal_session import TerminalControlCommand

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


def _snapshot(
    *,
    next_offset: int = 0,
    status: TerminalSessionStatus = TerminalSessionStatus.RUNNING,
    exit_code: int | None = None,
) -> TerminalSessionSnapshot:
    return TerminalSessionSnapshot(
        terminal_session_id=TERMINAL_SESSION_ID,
        status=status,
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
            next_offset=next_offset,
            oldest_sequence=1,
            next_sequence=1 if next_offset == 0 else 2,
        ),
        exit_code=0 if status is TerminalSessionStatus.EXITED and exit_code is None else exit_code,
    )


def _write_command_result(*, bytes_written: int, output_offset_before_write: int) -> dict[str, int]:
    return {
        "bytes_written": bytes_written,
        "output_offset_before_write": output_offset_before_write,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "bytes_written", "output_offset_before_write", "expected"),
    [
        ("abcde", 5, 8, {"bytes_written": 5, "output_offset_before_write": 8}),
        ("wxyz", 4, 7, {"bytes_written": 4, "output_offset_before_write": 7}),
    ],
)
async def test_terminal_write_command_result_matches_runtime(data, bytes_written, output_offset_before_write, expected):
    class FakeDriver:
        def resource_snapshot(self):
            return PtyResourceSnapshot(
                pid=123,
                running=True,
                eof=False,
                exit_code=None,
                output_buffer=_snapshot(next_offset=output_offset_before_write).output_buffer,
                retained_bytes=output_offset_before_write,
                dropped_bytes=0,
            )

        async def write(self, received_data):
            self.received_data = received_data
            return len(received_data)

    driver = FakeDriver()
    runtime = TerminalSessionRuntimeCommands()
    runtime._driver = driver
    command = TerminalControlCommand(
        terminal_session_id=TERMINAL_SESSION_ID,
        request_id="write-result-test",
        action=TerminalAction.WRITE,
        payload={"data": data},
        payload_hash="0" * 64,
    )

    actual = await runtime._execute_command(command)
    helper_result = _write_command_result(
        bytes_written=bytes_written,
        output_offset_before_write=output_offset_before_write,
    )

    assert driver.received_data == data
    assert actual == expected
    assert helper_result == expected


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


def _executor(executor_class, *, tool_timeout: float = 30.0, db=None):
    executor = executor_class(project_root=".", uid="u1")
    executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": tool_timeout}}))
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode="interactive",
            source="test",
            uid="u1",
            session_id="session-1",
            profile=_profile(["execute_shell"]),
            db=db if db is not None else object(),
            tool_call_id="tool-call-1",
        )
    )
    return executor


@pytest.mark.asyncio
async def test_terminal_read_records_completed_eof_after_awaiting_watch_reply(monkeypatch):
    snapshot = _snapshot(status=TerminalSessionStatus.EXITED)
    result = TerminalReadResult(
        terminal_session_id=TERMINAL_SESSION_ID,
        read_status=TerminalOutputReadStatus.OK,
        requested_offset=0,
        start_offset=0,
        next_offset=4,
        oldest_available_offset=0,
        latest_offset=4,
        sequence=1,
        output="done",
        eof=True,
    )
    recorded = []

    async def read_once(*_args, **_kwargs):
        return result

    async def get_snapshot(*_args, **_kwargs):
        return snapshot

    async def record_terminal_read(received_db, **kwargs):
        await asyncio.sleep(0)
        recorded.append((received_db, kwargs))

    async with AsyncSession() as db:
        executor = _executor(TerminalReadExecutor, db=db)
        monkeypatch.setattr(executor, "_read_once", read_once)
        monkeypatch.setattr(executor, "_get_snapshot", get_snapshot)
        monkeypatch.setattr(terminal_module.terminal_watch_reply_crud, "record_terminal_read", record_terminal_read)

        payload = await executor.execute(TERMINAL_SESSION_ID, offset=0, max_bytes=16_384)

    assert json.loads(payload) == {**snapshot.model_dump(mode="json"), **result.model_dump(mode="json")}
    assert recorded == [
        (
            db,
            {
                "uid": "u1",
                "session_id": "session-1",
                "profile_id": 7,
                "terminal_session_id": TERMINAL_SESSION_ID,
                "start_offset": 0,
                "next_offset": 4,
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.EXPIRED,
            requested_offset=8,
            start_offset=8,
            next_offset=8,
            oldest_available_offset=8,
            latest_offset=8,
            sequence=0,
            output="",
            eof=True,
        ),
        TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.OK,
            requested_offset=0,
            start_offset=0,
            next_offset=4,
            oldest_available_offset=0,
            latest_offset=8,
            sequence=1,
            output="done",
            eof=True,
        ),
    ],
    ids=["expired", "not-at-latest"],
)
async def test_terminal_read_does_not_record_expired_or_unread_latest_result(monkeypatch, result):
    snapshot = _snapshot(status=TerminalSessionStatus.EXITED)
    recorded = []

    async def read_once(*_args, **_kwargs):
        return result

    async def get_snapshot(*_args, **_kwargs):
        return snapshot

    async def record_terminal_read(received_db, **kwargs):
        recorded.append((received_db, kwargs))

    async with AsyncSession() as db:
        executor = _executor(TerminalReadExecutor, db=db)
        monkeypatch.setattr(executor, "_read_once", read_once)
        monkeypatch.setattr(executor, "_get_snapshot", get_snapshot)
        monkeypatch.setattr(terminal_module.terminal_watch_reply_crud, "record_terminal_read", record_terminal_read)

        payload = await executor.execute(TERMINAL_SESSION_ID, offset=0, max_bytes=16_384)

    assert json.loads(payload) == {**snapshot.model_dump(mode="json"), **result.model_dump(mode="json")}
    assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_on_timeout", "expected_output", "expected_next_offset"),
    [
        (None, "", 0),
        (True, "new-output-14!", 14),
    ],
    ids=["default", "read-on-timeout"],
)
async def test_watch_terminal_read_on_timeout_isolated_from_default(
    monkeypatch,
    read_on_timeout,
    expected_output,
    expected_next_offset,
):
    fixed_now = datetime(2026, 10, 24, 12, 0, 1, tzinfo=UTC)
    snapshot = _snapshot(next_offset=14)
    read_requests = []

    class FakeAsyncSessionLocal:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def commit(self):
            return None

    async def get_background_task(*args, **kwargs):
        return None

    async def get_snapshot(*args, **kwargs):
        return snapshot

    async def read(**kwargs):
        read_requests.append(kwargs)
        output = "new-output-14!"
        offset = kwargs["offset"]
        next_offset = offset + len(output.encode("ascii"))
        return TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.OK,
            requested_offset=offset,
            start_offset=offset,
            next_offset=next_offset,
            oldest_available_offset=0,
            latest_offset=next_offset,
            sequence=1,
            output=output,
            eof=False,
        )

    monkeypatch.setattr(terminal_watcher_module, "AsyncSessionLocal", FakeAsyncSessionLocal)
    monkeypatch.setattr(terminal_watcher_module.background_task_crud, "get", get_background_task)
    monkeypatch.setattr(terminal_watcher_module.terminal_session_manager, "get_snapshot", get_snapshot)
    monkeypatch.setattr(terminal_watcher_module, "_read", read)
    monkeypatch.setattr(terminal_watcher_module, "get_local_time", lambda: fixed_now)

    arguments = {
        "task_id": 901,
        "uid": "u1",
        "session_id": "session-1",
        "request": TerminalWatchRequest(terminal_session_id=TERMINAL_SESSION_ID, after_offset=0),
        "deadline": fixed_now - timedelta(seconds=1),
    }
    if read_on_timeout is not None:
        arguments["read_on_timeout"] = read_on_timeout

    result = await terminal_watcher_module.watch_terminal(**arguments)

    assert result == {
        "terminal_session_id": TERMINAL_SESSION_ID,
        "event": "timeout",
        "after_offset": 0,
        "session_status": "running",
        "exit_code": None,
        "failure_reason": None,
        "output": expected_output,
        "start_offset": 0,
        "next_offset": expected_next_offset,
        "latest_offset": 14,
        "output_truncated": False,
        "eof": False,
    }
    if read_on_timeout is None:
        assert read_requests == []
    else:
        assert len(read_requests) == 1
        assert read_requests[0]["offset"] == 0
        assert read_requests[0]["max_bytes"] == 16_384
        assert 0 < read_requests[0]["timeout_seconds"] <= 5.0


@pytest.mark.asyncio
async def test_get_tools_for_profile_exposes_shell_companions_only_with_shell():
    enabled_tools, _ = await get_tools_for_profile(None, _profile(["execute_shell"]))
    enabled_names = [tool["function"]["name"] for tool in enabled_tools if tool["function"]["name"] not in SYSTEM_BUILTIN_TOOL_NAMES]

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
        "terminal_read": {
            "required": ["terminal_session_id"],
            "properties": {
                "terminal_session_id": {},
                "request_id": {
                    "type": "string",
                    "minLength": 16,
                    "maxLength": 128,
                    "pattern": r"^[A-Za-z0-9_-]+$",
                },
                "offset": {"minimum": 0, "default": 0},
                "max_bytes": {"minimum": 1, "maximum": 16_384, "default": 16_384},
                "read_output": {"type": "boolean", "default": True},
            },
        },
        "terminal_write": {
            "required": ["terminal_session_id", "data"],
            "properties": {
                "terminal_session_id": {},
                "data": {"minLength": 1, "maxLength": 65_536},
                "request_id": {
                    "type": "string",
                    "minLength": 16,
                    "maxLength": 128,
                    "pattern": r"^[A-Za-z0-9_-]+$",
                },
            },
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
        "terminal_watch": {
            "required": ["terminal_session_id", "after_offset"],
            "properties": {
                "terminal_session_id": {},
                "after_offset": {"minimum": 0},
                "condition": {
                    "default": "output",
                    "enum": ["output", "process_exit", "output_contains", "quiet_for"],
                },
                "text": {"default": None},
                "settle_seconds": {"minimum": 1, "maximum": 60, "default": 2},
                "max_bytes": {"minimum": 1, "maximum": 16_384, "default": 16_384},
            },
        },
    }

    assert "terminal_signal" not in {schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS}
    assert "terminal_status" not in {schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS}
    for schema in SHELL_COMPANION_TOOL_SCHEMAS:
        name = schema["function"]["name"]
        parameters = schema["function"]["parameters"]
        assert parameters["additionalProperties"] is False
        assert parameters["required"] == expected[name]["required"]
        assert set(parameters["properties"]) == set(expected[name]["properties"])
        if name == "terminal_watch":
            assert parameters["properties"]["terminal_session_id"] == {"$ref": "#/$defs/TerminalSessionId"}
        else:
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

    description = TERMINAL_WRITE_TOOL_SCHEMA["function"]["description"]
    assert "terminal_read" in description
    assert "read_offset" in description


@pytest.mark.asyncio
async def test_terminal_executors_use_protocol_results_and_stable_request_ids(monkeypatch):
    control_requests = []
    read_requests = []
    command_results = {}
    command_kinds = {}
    wait_timeouts = []
    write_result_payload = _write_command_result(bytes_written=5, output_offset_before_write=8)
    snapshot = _snapshot(next_offset=14)

    async def get_snapshot(*args, **kwargs):
        return snapshot

    async def enqueue_read(*args, **kwargs):
        request = args[3]
        request_id = args[4]
        read_requests.append((request, request_id))
        command_id = 100 + len(read_requests)
        read_result = TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.OK,
            requested_offset=request.offset,
            start_offset=request.offset,
            next_offset=request.offset + 6,
            oldest_available_offset=0,
            latest_offset=request.offset + 6,
            sequence=1,
            output="terminal output",
            eof=False,
        )
        command_results[command_id] = read_result.model_dump(mode="json")
        command_results[command_id]["_terminal_snapshot"] = snapshot.model_dump(mode="json")
        command_kinds[command_id] = "read"
        return SimpleNamespace(id=command_id, kind="read", terminal_session_id=TERMINAL_SESSION_ID), len(read_requests) == 1

    async def enqueue_control(*args, **kwargs):
        request = args[3]
        control_requests.append(request)
        command_id = 200 + len(control_requests)
        if request.action is TerminalAction.WRITE:
            command_results[command_id] = {
                **write_result_payload,
                "_terminal_snapshot": snapshot.model_dump(mode="json"),
            }
            command_kinds[command_id] = "write"
            created = sum(item.action is TerminalAction.WRITE for item in control_requests) == 1
        elif request.action is TerminalAction.RESIZE:
            command_results[command_id] = {
                "columns": request.columns,
                "rows": request.rows,
                "_terminal_snapshot": snapshot.model_dump(mode="json"),
            }
            command_kinds[command_id] = "resize"
            created = False
        else:
            close_output = "final output"
            closed_snapshot = _snapshot(
                next_offset=len(close_output.encode("ascii")),
                status=TerminalSessionStatus.EXITED,
                exit_code=0,
            )
            command_results[command_id] = {
                "_terminal_snapshot": closed_snapshot.model_dump(mode="json"),
                "exit_code": 0,
                "failure_reason": None,
                "output": close_output,
                "start_offset": 0,
                "next_offset": len(close_output.encode("ascii")),
                "latest_offset": len(close_output.encode("ascii")),
                "output_truncated": False,
            }
            command_kinds[command_id] = "close"
            created = False
        return SimpleNamespace(id=command_id, kind=request.action, terminal_session_id=TERMINAL_SESSION_ID), created

    async def wait_for_command_result(*args, terminal_session_id=None, **kwargs):
        command_id = args[1]
        timeout_seconds = args[2]
        wait_timeouts.append((command_kinds[command_id], timeout_seconds))
        return dict(command_results[command_id])

    async def get_owned_session(*args, **kwargs):
        raise terminal_module.ResourceNotFoundException()

    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_snapshot", get_snapshot)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_read", enqueue_read)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_control", enqueue_control)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "wait_for_command_result", wait_for_command_result)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_owned_session", get_owned_session)

    read_executor = _executor(TerminalReadExecutor)
    status_result = json.loads(await read_executor.execute(TERMINAL_SESSION_ID, read_output=False))
    status_snapshot = TerminalSessionSnapshot.model_validate({field: status_result[field] for field in TerminalSessionSnapshot.model_fields})
    assert status_snapshot.status is TerminalSessionStatus.RUNNING

    standalone_read_payload = json.loads(await read_executor.execute(TERMINAL_SESSION_ID, offset=0, max_bytes=16_384))
    standalone_read_result = TerminalReadResult.model_validate({field: standalone_read_payload[field] for field in TerminalReadResult.model_fields})
    assert standalone_read_result.requested_offset == 0
    expected_read_result = {field: value for field, value in command_results[101].items() if field != "_terminal_snapshot"}
    standalone_read_snapshot = TerminalSessionSnapshot.model_validate({field: standalone_read_payload[field] for field in TerminalSessionSnapshot.model_fields})
    assert standalone_read_snapshot == snapshot
    assert standalone_read_result.model_dump(mode="json") == expected_read_result
    assert set(standalone_read_payload) == set(snapshot.model_dump(mode="json")) | set(expected_read_result)
    assert "_terminal_snapshot" not in standalone_read_payload

    write_executor = _executor(TerminalWriteExecutor, tool_timeout=5.0)
    write_payload = json.loads(await write_executor.execute(terminal_session_id=TERMINAL_SESSION_ID, data="input"))
    write_result = TerminalWriteResult.model_validate(write_payload)
    repeated_write_payload = json.loads(await write_executor.execute(terminal_session_id=TERMINAL_SESSION_ID, data="input"))
    repeated_write_result = TerminalWriteResult.model_validate(repeated_write_payload)
    assert write_result.action is TerminalAction.WRITE
    assert write_result.bytes_written == 5
    assert write_result.read_offset == 8
    expected_write_fields = {
        "terminal_session_id",
        "request_id",
        "action",
        "duplicate",
        "session_status",
        "bytes_written",
        "read_offset",
    }
    assert set(write_payload) == expected_write_fields
    assert write_payload["bytes_written"] == 5
    assert write_payload["read_offset"] == 8
    assert set(repeated_write_payload) == expected_write_fields
    assert write_result.duplicate is False
    assert repeated_write_result.duplicate is True
    assert repeated_write_result.model_dump(mode="json", exclude={"duplicate"}) == write_result.model_dump(mode="json", exclude={"duplicate"})

    action_executors = [
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
        payload = json.loads(await executor.execute(**arguments))
        receipt = TerminalActionReceipt.model_validate({field: payload[field] for field in TerminalActionReceipt.model_fields})
        assert receipt.action is action
        assert receipt.duplicate is True
        assert receipt.request_id == request_ids[action]

        if action is TerminalAction.CLOSE:
            assert payload["session_status"] == TerminalSessionStatus.EXITED.value
            assert payload["exit_code"] == 0
            assert payload["failure_reason"] is None
            assert payload["output"] == "final output"
            assert payload["start_offset"] == 0
            assert payload["next_offset"] == len("final output")
            assert payload["latest_offset"] == len("final output")
            assert payload["output_truncated"] is False
            assert payload["resources_released"] is True
            assert "_terminal_snapshot" not in payload
        else:
            repeated_payload = json.loads(await executor.execute(**arguments))
            repeated_receipt = TerminalActionReceipt.model_validate({field: repeated_payload[field] for field in TerminalActionReceipt.model_fields})
            assert repeated_receipt.model_dump(mode="json") == receipt.model_dump(mode="json")

    assert len(set(request_ids.values())) == len(request_ids)
    all_action_request_ids = {action: _executor(TerminalReadExecutor)._derive_request_id(action) for action in TerminalAction}
    assert all(len(request_id) == 64 for request_id in all_action_request_ids.values())
    assert len(set(all_action_request_ids.values())) == len(all_action_request_ids)
    write_requests = [request for request in control_requests if request.action is TerminalAction.WRITE]
    assert len(write_requests) == 2
    assert write_requests[0].request_id == write_requests[1].request_id
    assert write_requests[0].request_id == _executor(TerminalWriteExecutor)._derive_request_id(TerminalAction.WRITE)
    assert len(read_requests) == 1
    assert read_requests[0][0].offset == 0
    write_wait_timeouts = [timeout for kind, timeout in wait_timeouts if kind == "write"]
    assert len(write_wait_timeouts) == 2
    assert all(0 < timeout <= 5.0 for timeout in write_wait_timeouts)
    assert [request.action for request in control_requests] == [
        TerminalAction.WRITE,
        TerminalAction.WRITE,
        TerminalAction.RESIZE,
        TerminalAction.RESIZE,
        TerminalAction.CLOSE,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write_request",
    [
        {"request_id": "request-status-pending-1", "command_status": "pending", "result": None, "error": None},
        {
            "request_id": "request-status-succeeded-1",
            "command_status": "succeeded",
            "result": {"bytes_written": 5, "output_offset_before_write": 14},
            "error": None,
        },
    ],
    ids=["pending", "succeeded"],
)
@pytest.mark.parametrize("read_output", [False, True], ids=["query", "read"])
async def test_terminal_read_returns_write_request_status_without_enqueuing(monkeypatch, write_request, read_output):
    snapshot = _snapshot(next_offset=14)
    read_result = TerminalReadResult(
        terminal_session_id=TERMINAL_SESSION_ID,
        read_status=TerminalOutputReadStatus.OK,
        requested_offset=0,
        start_offset=0,
        next_offset=4,
        oldest_available_offset=0,
        latest_offset=8,
        sequence=1,
        output="done",
        eof=False,
    )
    read_calls = []

    async def get_snapshot(*args, **kwargs):
        return snapshot

    async def get_write_status(*args, **kwargs):
        return write_request

    async def unexpected_enqueue(*args, **kwargs):
        raise AssertionError("terminal_read must not enqueue terminal commands in this test")

    async def read_once(*args, **kwargs):
        read_calls.append((args, kwargs))
        if not read_output:
            raise AssertionError("terminal_read query must not read output")
        return read_result

    async def unexpected_auto_watch(*args, **kwargs):
        raise AssertionError("terminal_read must not register an automatic watch")

    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_snapshot", get_snapshot)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_write_status", get_write_status)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_control", unexpected_enqueue)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_read", unexpected_enqueue)
    monkeypatch.setattr(terminal_module, "ensure_terminal_auto_watch", unexpected_auto_watch)

    executor = _executor(TerminalReadExecutor)
    monkeypatch.setattr(executor, "_read_once", read_once)

    result = json.loads(
        await executor.execute(
            TERMINAL_SESSION_ID,
            request_id=write_request["request_id"],
            read_output=read_output,
        )
    )

    expected = {**snapshot.model_dump(mode="json"), "write_request": write_request}
    if read_output:
        expected.update(read_result.model_dump(mode="json"))
    assert result == expected
    assert bool(read_calls) is read_output


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["", "short", "x" * 129, "invalid/request"])
@pytest.mark.parametrize("executor_class", [TerminalReadExecutor, TerminalWriteExecutor], ids=["read", "write"])
async def test_terminal_read_and_write_reject_invalid_request_ids_without_enqueue(
    monkeypatch,
    executor_class,
    request_id,
):
    control_requests = []

    async def get_snapshot(*args, **kwargs):
        return _snapshot()

    async def enqueue_control(*args, **kwargs):
        control_requests.append(args[3])
        return SimpleNamespace(id=201, terminal_session_id=TERMINAL_SESSION_ID), True

    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_snapshot", get_snapshot)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_control", enqueue_control)

    with pytest.raises(ValidationError):
        if executor_class is TerminalReadExecutor:
            await _executor(executor_class).execute(TERMINAL_SESSION_ID, request_id=request_id)
        else:
            await _executor(executor_class).execute(
                terminal_session_id=TERMINAL_SESSION_ID,
                data="input",
                request_id=request_id,
            )

    assert control_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_changes",
    [False, True],
    ids=["no-new-output", "continuous-output"],
)
@pytest.mark.parametrize(
    "request_id",
    [None, "explicit-write-request-123"],
    ids=["derived-request-id", "explicit-request-id"],
)
async def test_terminal_write_waits_for_confirmation_without_waiting_for_output(monkeypatch, output_changes, request_id):
    next_offset = 7
    control_requests = []
    read_requests = []
    confirmation_started = asyncio.Event()
    release_confirmation = asyncio.Event()

    async def get_snapshot(*args, **kwargs):
        nonlocal next_offset
        snapshot = _snapshot(next_offset=next_offset)
        if output_changes:
            next_offset += 7
        return snapshot

    async def enqueue_control(*args, **kwargs):
        request = args[3]
        control_requests.append(request)
        return SimpleNamespace(id=201, terminal_session_id=TERMINAL_SESSION_ID), True

    async def enqueue_read(*args, **kwargs):
        read_requests.append(args[3])
        raise AssertionError("terminal_write must not enqueue terminal_read")

    async def wait_for_command_result(*args, **kwargs):
        confirmation_started.set()
        await release_confirmation.wait()
        return _write_command_result(bytes_written=5, output_offset_before_write=7)

    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_snapshot", get_snapshot)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_read", enqueue_read)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_control", enqueue_control)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "wait_for_command_result", wait_for_command_result)

    task = asyncio.create_task(
        _executor(TerminalWriteExecutor, tool_timeout=5.0).execute(
            terminal_session_id=TERMINAL_SESSION_ID,
            data="input",
            request_id=request_id,
        )
    )
    try:
        await asyncio.wait_for(confirmation_started.wait(), timeout=1.0)
        assert not task.done()
        release_confirmation.set()
        result_payload = json.loads(await asyncio.wait_for(task, timeout=1.0))
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    result = TerminalWriteResult.model_validate(result_payload)
    assert set(result_payload) == {
        "terminal_session_id",
        "request_id",
        "action",
        "duplicate",
        "session_status",
        "bytes_written",
        "read_offset",
    }
    assert result.bytes_written == 5
    assert result.read_offset == 7
    assert result.session_status is TerminalSessionStatus.RUNNING
    expected_request_id = request_id or _executor(TerminalWriteExecutor)._derive_request_id(TerminalAction.WRITE)
    assert result.request_id == expected_request_id
    assert [request.request_id for request in control_requests] == [expected_request_id]
    assert read_requests == []
    assert [request.action for request in control_requests] == [TerminalAction.WRITE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "message"),
    [(RuntimeError, "worker failure"), (TimeoutError, "receipt timeout")],
)
async def test_terminal_write_confirmation_failure_or_timeout_is_not_retried(
    monkeypatch,
    exception_type,
    message,
):
    control_requests = []
    read_requests = []
    failure = exception_type(message)

    async def get_snapshot(*args, **kwargs):
        return _snapshot(next_offset=14)

    async def enqueue_control(*args, **kwargs):
        request = args[3]
        control_requests.append(request)
        return SimpleNamespace(id=201, kind=request.action, terminal_session_id=TERMINAL_SESSION_ID), True

    async def enqueue_read(*args, **kwargs):
        read_requests.append(args[3])
        raise AssertionError("terminal_write must not enqueue terminal_read")

    async def wait_for_command_result(*args, **kwargs):
        raise failure

    monkeypatch.setattr(terminal_module.terminal_session_manager, "get_snapshot", get_snapshot)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_read", enqueue_read)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "enqueue_control", enqueue_control)
    monkeypatch.setattr(terminal_module.terminal_session_manager, "wait_for_command_result", wait_for_command_result)

    executor = _executor(TerminalWriteExecutor, tool_timeout=5.0)
    if exception_type is TimeoutError:
        result_payload = json.loads(
            await executor.execute(
                terminal_session_id=TERMINAL_SESSION_ID,
                data="input",
            )
        )
        assert result_payload == {
            "status": "execution_unknown",
            "tool_name": "terminal_write",
            "terminal_session_id": TERMINAL_SESSION_ID,
            "request_id": control_requests[0].request_id,
            "error_code": ERR_TERMINAL_COMMAND_TIMEOUT,
            "error": str(failure),
        }
    else:
        with pytest.raises(exception_type) as raised:
            await executor.execute(
                terminal_session_id=TERMINAL_SESSION_ID,
                data="input",
            )

        assert raised.value is failure
    assert read_requests == []
    assert [request.action for request in control_requests] == [TerminalAction.WRITE]
