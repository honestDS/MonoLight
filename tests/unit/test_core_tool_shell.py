import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

# 全局安全拦截：禁止测试脚本执行任何真实系统指令
@pytest.fixture(autouse=True)
def block_real_shell(monkeypatch):
    # 建立一个会抛出异常的 Mock
    forbidden = MagicMock(side_effect=RuntimeError("REAL_SHELL_FORBIDDEN"))
    # 直接拦截 shell.py 中引用的 asyncio 关键入口
    monkeypatch.setattr("app.core.tools.shell.asyncio.create_subprocess_shell", forbidden)
    monkeypatch.setattr("app.core.tools.shell.asyncio.wait_for", forbidden)

from app.core.tools.shell import ShellExecutor, CONFIRMATION_TOKEN

@pytest.fixture
def executor():
    return ShellExecutor(project_root="/tmp/monolight_test")

@pytest.mark.asyncio
async def test_shell_execute_success(executor, monkeypatch):
    mock_process = MagicMock()
    # 必须确保 communicate 返回的是 coroutine
    mock_process.communicate = AsyncMock(return_value=(b"output", b""))
    mock_process.returncode = 0

    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_profile = MagicMock()
        mock_profile.extra_config = {"shell_timeout": 60}
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_profile
        mock_session.execute = AsyncMock(return_value=mock_result)

        # 使用 monkeypatch 覆盖全局拦截器，并确保返回 AsyncMock
        monkeypatch.setattr("app.core.tools.shell.asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process))
        monkeypatch.setattr("app.core.tools.shell.asyncio.wait_for", AsyncMock(return_value=(b"output", b"")))

        result_json = await executor.execute("ls")
        result = json.loads(result_json)
        assert result.get("stdout") == "output"

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
async def test_shell_execute_risky_command_with_confirmation(executor, monkeypatch):
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock(return_value=(b"deleted", b""))
    mock_process.returncode = 0

    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())

        monkeypatch.setattr("app.core.tools.shell.asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process))
        monkeypatch.setattr("app.core.tools.shell.asyncio.wait_for", AsyncMock(return_value=(b"deleted", b"")))

        command = f"{CONFIRMATION_TOKEN} rm test.txt"
        result_json = await executor.execute(command)
        result = json.loads(result_json)
        assert result.get("stdout") == "deleted"

@pytest.mark.asyncio
async def test_shell_execute_critical_path_denied(executor, monkeypatch):
    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())

        command = f"{CONFIRMATION_TOKEN} rm -rf /etc"
        
        # 拦截 Path 解析
        with patch("app.core.tools.shell.Path") as MockPath:
            mock_path_instance = MockPath.return_value
            mock_path_instance.is_absolute.return_value = True
            mock_resolved = MagicMock()
            mock_resolved.__str__.return_value = "/etc"
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
async def test_shell_execute_timeout(executor, monkeypatch):
    mock_process = MagicMock()
    mock_process.kill = MagicMock()

    with patch("app.core.tools.shell.AsyncSessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=MagicMock())

        monkeypatch.setattr("app.core.tools.shell.asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process))
        # 显式抛出 TimeoutError，确保触发 shell.py 的 except 逻辑
        monkeypatch.setattr("app.core.tools.shell.asyncio.wait_for", AsyncMock(side_effect=asyncio.TimeoutError))

        result_json = await executor.execute("sleep 10", timeout=1)
        result = json.loads(result_json)
        assert result.get("error") == "Command timed out"
