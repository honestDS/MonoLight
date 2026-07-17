import json
import sys
import sysconfig

import pytest

from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor


@pytest.mark.asyncio
async def test_execute_python_inline_command_bypasses_shell(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    async def fake_get_profile_timeout():
        return 5.0

    monkeypatch.setattr(executor, "_get_profile_timeout", fake_get_profile_timeout)
    python_executable = sys.executable.replace("\\", "/")
    command = f"\"{python_executable}\" -c \"import json, sys; payload = {{'quote': '\\\"', 'items': [1, 2, 3], 'arg': sys.argv[1]}}; print(json.dumps(payload, ensure_ascii=False))\" \"arg with spaces\""

    result = json.loads(await executor.execute(command=command))

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

    result = json.loads(await executor.execute(command=command))

    assert result["exit_code"] == 0
    payload = json.loads(result["stdout"])
    assert payload == {"name": "测试对象", "data": [10]}


@pytest.mark.asyncio
async def test_execute_command_receives_closed_stdin(monkeypatch, tmp_path):
    executor = ShellExecutor(project_root=str(tmp_path), uid="u1")

    async def fake_get_profile_timeout():
        return 1.0

    monkeypatch.setattr(executor, "_get_profile_timeout", fake_get_profile_timeout)
    result = json.loads(await executor.execute(command='python -c "import sys; print(len(sys.stdin.read()))"'))

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


def test_shell_schema_only_exposes_command():
    parameters = SHELL_TOOL_SCHEMA["function"]["parameters"]

    assert parameters["required"] == ["command"]
    assert set(parameters["properties"]) == {"command"}
    assert "automatically bypasses shell escaping" in parameters["properties"]["command"]["description"]
