import json
import sys
import sysconfig
from types import SimpleNamespace

import pytest

import app.core.tools.shell as shell_module
from app.core.constants import ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED
from app.core.dispatch_context import DispatchContext
from app.core.i18n import t
from app.core.terminal import (
    ALL_TERMINAL_ACTIONS,
    ShellExecutionMode,
    TerminalOutputBufferState,
    TerminalPermissionScope,
    TerminalSessionSnapshot,
    TerminalSessionStatus,
)
from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor
from app.models.profile import Profile, ProfileConfig


@pytest.mark.asyncio
async def test_execute_python_inline_command_bypasses_shell(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    async def fake_get_profile_timeout():
        return 5.0

    monkeypatch.setattr(executor, "_get_profile_timeout", fake_get_profile_timeout)
    python_executable = sys.executable.replace("\\", "/")
    command = f"\"{python_executable}\" -c \"import json, sys; payload = {{'quote': '\\\"', 'items': [1, 2, 3], 'arg': sys.argv[1]}}; print(json.dumps(payload, ensure_ascii=False))\" \"arg with spaces\""

    result = json.loads(await executor.execute(command=command, execution_mode="non_interactive"))

    assert result["exit_code"] == 0
    payload = json.loads(result["stdout"])
    assert payload["items"] == [1, 2, 3]
    assert payload["quote"] == '"'
    assert payload["arg"] == "arg with spaces"
    assert result["stderr"] == ""


@pytest.mark.asyncio
async def test_execute_python_inline_command_normalizes_compound_statement(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    async def fake_get_profile_timeout():
        return 5.0

    monkeypatch.setattr(executor, "_get_profile_timeout", fake_get_profile_timeout)
    python_executable = sys.executable.replace("\\", "/")
    command = (
        f'"{python_executable}" -c '
        '"import json; class TestClass:\n'
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "        self.data = []\n"
        "\n"
        "    def add_data(self, item):\n"
        "        self.data.append(item)\n"
        "\n"
        "obj = TestClass('测试对象')\n"
        "obj.add_data(10)\n"
        "print(json.dumps({'name': obj.name, 'data': obj.data}, ensure_ascii=False))\""
    )

    result = json.loads(await executor.execute(command=command, execution_mode="non_interactive"))

    assert result["exit_code"] == 0
    payload = json.loads(result["stdout"])
    assert payload == {"name": "测试对象", "data": [10]}


@pytest.mark.asyncio
async def test_execute_command_receives_closed_stdin(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    async def fake_get_profile_timeout():
        return 1.0

    monkeypatch.setattr(executor, "_get_profile_timeout", fake_get_profile_timeout)
    result = json.loads(
        await executor.execute(
            command='python -c "import sys; print(len(sys.stdin.read()))"',
            execution_mode="non_interactive",
        )
    )

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "0"
    assert result["stderr"] == ""


def test_python_inline_command_detection_skips_shell_composition(tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    assert executor._extract_python_inline_command('python -c "print(1)"') == [sys.executable, "-c", "print(1)"]
    assert executor._extract_python_inline_command('py -c "print(1)" "arg with spaces"') == [sys.executable, "-c", "print(1)", "arg with spaces"]
    assert executor._extract_python_inline_command('python -c "import json; class TestClass:\n    pass"')[2].startswith("import json\nclass TestClass:")
    assert executor._extract_python_inline_command('python -c "print(1)" | more') is None
    assert executor._extract_python_inline_command('python -c "print(1)" > out.txt') is None
    assert executor._extract_python_inline_command("python -c \"import sys; sys.stderr.write('x')\" 2> err.txt") is None
    assert executor._extract_python_inline_command("python -c \"import sys; sys.stderr.write('x')\" 2>&1") is None


def test_python_inline_command_keeps_explicit_interpreter_path(tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    assert executor._extract_python_inline_command('C:/Python313/python.exe -c "print(1)"') == ["C:/Python313/python.exe", "-c", "print(1)"]


def test_subprocess_env_prefers_current_python_scripts_dir(tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    env = executor._build_subprocess_env()
    path_separator = ";" if sys.platform == "win32" else ":"

    assert env["PATH"].split(path_separator)[0] == sysconfig.get_path("scripts")
    if sys.prefix != sys.base_prefix:
        assert env["VIRTUAL_ENV"] == sys.prefix


def test_shell_schema_requires_explicit_execution_mode():
    parameters = SHELL_TOOL_SCHEMA["function"]["parameters"]
    execution_mode = parameters["properties"]["execution_mode"]

    assert parameters["required"] == ["command", "execution_mode"]
    assert set(parameters["properties"]) == {"command", "execution_mode"}
    assert parameters["additionalProperties"] is False
    assert execution_mode["enum"] == ["interactive", "non_interactive"]
    assert "default" not in repr(SHELL_TOOL_SCHEMA).lower()
    assert "auto" not in repr(SHELL_TOOL_SCHEMA).lower()
    assert "non_interactive" not in parameters["properties"]["command"]["description"]


def _block_shell_early_dependencies(monkeypatch, executor):
    def fail(*args, **kwargs):
        raise AssertionError("shell execution reached an early dependency")

    monkeypatch.setattr(shell_module, "get_full_system_context", fail)
    monkeypatch.setattr(executor, "_get_profile_timeout", fail)
    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_exec", fail)
    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_shell", fail)
    monkeypatch.setattr(shell_module.subprocess, "Popen", fail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execution_mode",
    ["auto", "AUTO", "Interactive", "NON_INTERACTIVE", None, 1, True, {}],
)
async def test_execute_rejects_invalid_execution_mode_before_side_effects(monkeypatch, tmp_path, execution_mode):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")
    _block_shell_early_dependencies(monkeypatch, executor)

    with pytest.raises(ValueError):
        await executor.execute(command="echo ok", execution_mode=execution_mode)


@pytest.mark.asyncio
async def test_execute_interactive_with_configured_audit_without_active_binding_returns_error(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")
    _block_shell_early_dependencies(monkeypatch, executor)
    executor.set_config(
        ProfileConfig.model_validate(
            {
                "security": {
                    "audit_channel_id": 1,
                    "audit_model_id": "audit-model",
                },
            }
        )
    )
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode="interactive",
            source="test",
            uid="u1",
            session_id="session-1",
            profile=Profile(id=7, uid="u1", name="profile", configs={}),
            db=object(),
            tool_call_id="tool-call-1",
        )
    )

    async def no_active_binding(*args, **kwargs):
        return None

    monkeypatch.setattr(shell_module.audit_crud, "get_running_execution_binding", no_active_binding)

    result = json.loads(
        await executor.execute(
            command="read-from-terminal",
            execution_mode=ShellExecutionMode.INTERACTIVE,
        )
    )

    assert result == {"error": t(ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED)}


@pytest.mark.asyncio
async def test_execute_interactive_without_audit_skips_audit_crud_and_hands_off_to_terminal_session(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")
    _block_shell_early_dependencies(monkeypatch, executor)
    executor.set_config(ProfileConfig.model_validate({}))
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode="interactive",
            source="test",
            uid="u1",
            session_id="session-1",
            profile=Profile(id=7, uid="u1", name="profile", configs={}),
            db=object(),
            tool_call_id="tool-call-1",
        )
    )

    async def fail_audit_call(*args, **kwargs):
        raise AssertionError("unaudited interactive shell must not call audit_crud")

    terminal_session = SimpleNamespace(terminal_session_id="terminal-session-1" * 3)
    snapshot = TerminalSessionSnapshot(
        terminal_session_id=terminal_session.terminal_session_id,
        status=TerminalSessionStatus.STARTING,
        permission_scope=TerminalPermissionScope(
            owner_uid="u1",
            owner_session_id="session-1",
            original_tool_call_id="tool-call-1",
            audit_record_id=None,
            audit_execution_record_id=None,
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

    async def get_or_create_session(*args, **kwargs):
        assert kwargs["original_tool_call_id"] == "tool-call-1"
        assert kwargs["audit_record_id"] is None
        assert kwargs["audit_execution_record_id"] is None
        return terminal_session

    async def get_snapshot(*args, **kwargs):
        return snapshot

    monkeypatch.setattr(shell_module.audit_crud, "get_running_execution_binding", fail_audit_call)
    monkeypatch.setattr(shell_module.audit_crud, "list_tool_details", fail_audit_call)
    monkeypatch.setattr(shell_module.terminal_session_manager, "get_or_create_session_for_execution", get_or_create_session)
    monkeypatch.setattr(shell_module.terminal_session_manager, "get_snapshot", get_snapshot)

    result = json.loads(
        await executor.execute(
            command="python -i",
            execution_mode=ShellExecutionMode.INTERACTIVE,
        )
    )

    assert result["terminal_session_id"] == terminal_session.terminal_session_id
    assert result["status"] == "starting"
    assert result["output_stream"] == "merged_stdout_stderr"


@pytest.mark.asyncio
async def test_execute_interactive_hands_off_to_terminal_session(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")
    _block_shell_early_dependencies(monkeypatch, executor)
    executor.set_config(
        ProfileConfig.model_validate(
            {
                "security": {
                    "audit_channel_id": 1,
                    "audit_model_id": "audit-model",
                },
            }
        )
    )
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode="interactive",
            source="test",
            uid="u1",
            session_id="session-1",
            profile=Profile(id=7, uid="u1", name="profile", configs={}),
            db=object(),
            tool_call_id="tool-call-1",
        )
    )

    audit_record = SimpleNamespace(id=11, uid="u1", session_id="session-1")
    audit_execution = SimpleNamespace(id=22, audit_tool_detail_id=33)
    audit_detail = SimpleNamespace(id=33, original_tool_call_id="original-tool-call")
    terminal_session = SimpleNamespace(terminal_session_id="terminal-session-1" * 3)
    snapshot = TerminalSessionSnapshot(
        terminal_session_id=terminal_session.terminal_session_id,
        status=TerminalSessionStatus.STARTING,
        permission_scope=TerminalPermissionScope(
            owner_uid="u1",
            owner_session_id="session-1",
            original_tool_call_id="original-tool-call",
            audit_record_id=11,
            audit_execution_record_id=22,
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

    async def get_running_execution_binding(*args, **kwargs):
        assert kwargs == {"new_tool_call_id": "tool-call-1"}
        return audit_record, audit_execution

    async def list_tool_details(*args, **kwargs):
        assert args[1:] == (11,)
        return [audit_detail]

    async def get_or_create_session(*args, **kwargs):
        assert kwargs["profile_id"] == 7
        assert kwargs["original_tool_call_id"] == "original-tool-call"
        assert kwargs["audit_record_id"] == 11
        assert kwargs["audit_execution_record_id"] == 22
        assert kwargs["allowed_actions"] == ALL_TERMINAL_ACTIONS
        return terminal_session

    async def get_snapshot(*args, **kwargs):
        assert args[1:] == (terminal_session.terminal_session_id, "u1", "session-1")
        return snapshot

    monkeypatch.setattr(shell_module.audit_crud, "get_running_execution_binding", get_running_execution_binding)
    monkeypatch.setattr(shell_module.audit_crud, "list_tool_details", list_tool_details)
    monkeypatch.setattr(shell_module.terminal_session_manager, "get_or_create_session_for_execution", get_or_create_session)
    monkeypatch.setattr(shell_module.terminal_session_manager, "get_snapshot", get_snapshot)

    result = json.loads(
        await executor.execute(
            command="python -i",
            execution_mode=ShellExecutionMode.INTERACTIVE,
        )
    )

    assert result["terminal_session_id"] == terminal_session.terminal_session_id
    assert result["status"] == "starting"
    assert result["output_stream"] == "merged_stdout_stderr"
    assert result["output_buffer"] == {
        "capacity_bytes": 1_048_576,
        "oldest_offset": 0,
        "next_offset": 0,
        "oldest_sequence": 1,
        "next_sequence": 1,
    }
