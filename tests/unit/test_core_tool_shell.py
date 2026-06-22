import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

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
        "app.core.tools.shell.ShellExecutor._get_profile_timeout",
        AsyncMock(return_value=30.0),
    )
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


@pytest.mark.asyncio
async def test_shell_execute_stderr(executor, monkeypatch):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock(return_value=(b"", b"command not found"))
    mock_process.returncode = 127

    monkeypatch.setattr(
        "app.core.tools.shell.ShellExecutor._get_profile_timeout",
        AsyncMock(return_value=30.0),
    )
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
        "app.core.tools.shell.ShellExecutor._get_profile_timeout",
        AsyncMock(return_value=1.0),
    )
    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.create_subprocess_shell",
        AsyncMock(return_value=mock_process),
    )
    monkeypatch.setattr(
        "app.core.tools.shell.asyncio.wait_for",
        AsyncMock(side_effect=asyncio.TimeoutError),
    )

    result_json = await executor.execute("sleep 10")
    result = json.loads(result_json)
    assert "Command timed out" in result["error"]
    mock_process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_shell_execute_blacklisted_command(executor):
    # 测试当前黑名单 powershell (忽略大小写)
    result_json = await executor.execute("PowerShell -Command 'ls'")
    result = json.loads(result_json)
    assert "不允许使用shell工具执行该命令: powershell" in result["stdout"]
    assert result["exit_code"] == 1
    assert result["system_info"]

    # 包含 confirmation prefix 的情况
    result_json = await executor.execute("__confirm__ powershell -Command 'ls'")
    result = json.loads(result_json)
    assert "不允许使用shell工具执行该命令: powershell" in result["stdout"]
    assert result["exit_code"] == 1
    assert result["system_info"]
