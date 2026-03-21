import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from app.core.tools.shell import ShellExecutor


@pytest.fixture
def executor():
    return ShellExecutor(project_root="/tmp/monobot_test", uid="test_user")


@pytest.mark.asyncio
async def test_shell_execute_success(executor, monkeypatch):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock(return_value=(b"hello world", b""))
    mock_process.returncode = 0

    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.create_subprocess_shell",
        AsyncMock(return_value=mock_process),
    )
    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.wait_for",
        AsyncMock(return_value=(b"hello world", b"")),
    )

    result_json = await executor.execute("echo 'hello world'")
    result = json.loads(result_json)
    assert result["stdout"] == "hello world"
    assert result["exit_code"] == 0
    assert "stderr" in result


@pytest.mark.asyncio
async def test_shell_execute_stderr(executor, monkeypatch):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock(return_value=(b"", b"command not found"))
    mock_process.returncode = 127

    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.create_subprocess_shell",
        AsyncMock(return_value=mock_process),
    )
    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.wait_for",
        AsyncMock(return_value=(b"", b"command not found")),
    )

    result_json = await executor.execute("invalid_cmd")
    result = json.loads(result_json)
    assert "command not found" in result["stderr"]
    assert result["exit_code"] == 127


@pytest.mark.asyncio
async def test_shell_execute_timeout(executor, monkeypatch):
    mock_process = MagicMock()
    mock_process.kill = MagicMock()

    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.create_subprocess_shell",
        AsyncMock(return_value=mock_process),
    )
    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.wait_for",
        AsyncMock(side_effect=asyncio.TimeoutError),
    )

    result_json = await executor.execute("sleep 10", timeout=1)
    result = json.loads(result_json)
    assert result["error"] == "Command timed out"
    mock_process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_shell_executor_init_dir(executor):
    assert executor.user_temp_dir.name == "temp_test_user"
    assert executor.user_temp_dir.exists()


@pytest.mark.asyncio
async def test_shell_execute_exception(executor, monkeypatch):
    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.create_subprocess_shell",
        AsyncMock(side_effect=Exception("Execution failed")),
    )

    result_json = await executor.execute("ls")
    result = json.loads(result_json)
    assert result["error"] == "Execution failed"
