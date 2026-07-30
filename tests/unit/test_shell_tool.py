import json
import sys
import sysconfig

import pytest

import app.core.tools.shell as shell_module
from app.core.constants import ERR_TOOL_SHELL_INTERACTIVE_UNAVAILABLE
from app.core.i18n import t
from app.core.terminal import ShellExecutionMode
from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor


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
    monkeypatch.setattr(executor, "check_blacklist", fail)
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
async def test_execute_rejects_interactive_mode_before_side_effects(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")
    _block_shell_early_dependencies(monkeypatch, executor)

    with pytest.raises(RuntimeError) as exc_info:
        await executor.execute(command="read-from-terminal", execution_mode=ShellExecutionMode.INTERACTIVE)

    assert str(exc_info.value) == t(ERR_TOOL_SHELL_INTERACTIVE_UNAVAILABLE)
