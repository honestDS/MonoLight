import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.tools.shell import ShellExecutor


@pytest.fixture
def executor():
    return ShellExecutor(project_root="/tmp/monolight_test")


@pytest.mark.asyncio
async def test_shell_execute_success(executor):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock()
    mock_process.wait = AsyncMock()
    mock_process.communicate.return_value = (b"output", b"")
    mock_process.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_process):
        result_json = await executor.execute("ls")
        result = json.loads(result_json)
        assert result["exit_code"] == 0
        assert result["stdout"] == "output"
        assert result["stderr"] == ""


@pytest.mark.asyncio
async def test_shell_execute_blacklist(executor):
    result_json = await executor.execute("rm -rf /")
    result = json.loads(result_json)
    assert result["exit_code"] == -1
    assert "Security Alert" in result["error"]


@pytest.mark.asyncio
async def test_shell_execute_timeout(executor):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock()
    mock_process.wait = AsyncMock()
    mock_process.returncode = None
    mock_process.kill = MagicMock()

    with patch("asyncio.create_subprocess_shell", return_value=mock_process):
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            result_json = await executor.execute("sleep 10", timeout=1)
            result = json.loads(result_json)
            assert result["exit_code"] == -1
            assert "timed out" in result["error"]
            mock_process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_shell_execute_exception(executor):
    with patch(
        "asyncio.create_subprocess_shell", side_effect=Exception("Unexpected error")
    ):
        result_json = await executor.execute("ls")
        result = json.loads(result_json)
        assert "Unexpected error" in result["error"]
