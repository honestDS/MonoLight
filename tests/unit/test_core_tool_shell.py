import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.tools.shell import ShellExecutor, CONFIRMATION_TOKEN


@pytest.fixture
def executor():
    return ShellExecutor(project_root="/tmp/monolight_test")


@pytest.mark.asyncio
async def test_shell_execute_success(executor):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock()
    mock_process.communicate.return_value = (b"output", b"")
    mock_process.returncode = 0

    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_profile = MagicMock()
        mock_profile.extra_config = {"shell_timeout": 60}
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_profile
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("asyncio.create_subprocess_shell", return_value=mock_process):
            result_json = await executor.execute("ls")
            result = json.loads(result_json)
            assert result["stdout"] == "output"


@pytest.mark.asyncio
async def test_shell_execute_risky_command_interception(executor):
    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())
        
        result_json = await executor.execute("rm test.txt")
        result = json.loads(result_json)
        assert result.get("error") == "confirmation_required"


@pytest.mark.asyncio
async def test_shell_execute_risky_command_with_confirmation(executor):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock()
    mock_process.communicate.return_value = (b"deleted", b"")
    mock_process.returncode = 0

    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())

        with patch("asyncio.create_subprocess_shell", return_value=mock_process):
            command = f"{CONFIRMATION_TOKEN} rm test.txt"
            result_json = await executor.execute(command)
            result = json.loads(result_json)
            assert result["stdout"] == "deleted"


@pytest.mark.asyncio
async def test_shell_execute_critical_path_denied(executor):
    """
    通过劫持 Path 对象的行为来模拟敏感路径匹配。
    """
    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())

        command = f"{CONFIRMATION_TOKEN} rm -rf /etc"
        
        with patch("app.core.tools.shell.Path") as MockPath:
            mock_path_instance = MockPath.return_value
            mock_path_instance.is_absolute.return_value = True
            
            # resolve 应该返回一个 Mock 对象，其 str() 结果在 SENSITIVE_ROOT_PATHS 中
            mock_resolved = MagicMock()
            mock_resolved.__str__.return_value = "/etc"
            # 兼容 rstrip() 调用
            mock_resolved.rstrip.return_value = "/etc"
            mock_path_instance.resolve.return_value = mock_resolved
            
            result_json = await executor.execute(command)
            result = json.loads(result_json)
            
            assert "error" in result
            assert "Critical Security Alert" in result["error"]


@pytest.mark.asyncio
async def test_shell_execute_forbidden_binary(executor):
    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())

        result_json = await executor.execute("dd if=/dev/zero of=/dev/sda")
        result = json.loads(result_json)
        assert "Forbidden binary" in result["error"]


@pytest.mark.asyncio
async def test_shell_execute_timeout(executor):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock()
    mock_process.kill = MagicMock()

    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())

        with patch("asyncio.create_subprocess_shell", return_value=mock_process):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result_json = await executor.execute("sleep 10", timeout=1)
                result = json.loads(result_json)
                assert result.get("error") == "Command timed out"
