import asyncio
import json
import os
import subprocess
import sys

from app.core import constants
from app.core.crud.profile import profile_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
from app.core.prompts import CONFIRMATION_PREFIX
from app.core.utils.system import get_full_system_context
from app.models.profile import ProfileConfig
from app.providers.database import AsyncSessionLocal

from .base import BaseExecutor


class ShellExecutor(BaseExecutor):
    logger = get_logger(__name__)
    COMMAND_BLACKLIST = [
        "powershell",
    ]

    @classmethod
    def check_blacklist(cls, command: str) -> str | None:
        """检查命令是否在黑名单中，如果在则返回匹配的黑名单项"""
        for blacklisted in cls.COMMAND_BLACKLIST:
            if blacklisted.lower() in command.lower():
                return blacklisted
        return None

    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self.user_temp_dir = get_user_temp_dir(self.project_root, uid)
        self._ensure_temp_dir()

    def _ensure_temp_dir(self):
        if not self.user_temp_dir.exists():
            self.user_temp_dir.mkdir(parents=True, exist_ok=True)

    def _safe_decode(self, data: bytes) -> str:
        """尝试多种编码解码字节流，解决 Windows 下的乱码问题"""
        if not data:
            return ""
        for encoding in ["utf-8", "gbk", "cp936"]:
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    async def _get_profile_timeout(self) -> float:
        """从已激活的 Profile 中获取超时配置"""
        try:
            async with AsyncSessionLocal() as session:
                profile = await profile_crud.get_active(session, uid=self.uid)
                if profile and profile.configs:
                    cfg = ProfileConfig.model_validate(profile.configs)
                    return cfg.tool.tool_timeout
        except Exception as e:
            self.logger.error(t("LOG_SHELL_PROFILE_TIMEOUT_FAILED", error=str(e)))
        return 30.0

    async def execute(self, command: str) -> str:
        # 删除可能存在的确认前缀
        if command.startswith(CONFIRMATION_PREFIX):
            command = command.split(" ", 1)[-1]

        system_info = get_full_system_context()

        blacklisted = self.check_blacklist(command)
        if blacklisted:
            return json.dumps(
                {
                    "stdout": t(constants.ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted),
                    "stderr": "",
                    "exit_code": 1,
                    "system_info": system_info,
                },
                ensure_ascii=False,
            )

        t_logger = self.logger.bind(tool_call=True)
        # 获取当前循环类型进行诊断
        loop = asyncio.get_running_loop()
        loop_type = type(loop).__name__

        # 强制使用数据库配置的超时
        profile_timeout = await self._get_profile_timeout()

        # Windows 下的兼容性处理：如果不是 ProactorEventLoop，则使用同步运行+线程池降级
        if sys.platform == "win32" and "Proactor" not in loop_type:
            t_logger.warning(t("LOG_SHELL_WINDOWS_SYNC_FALLBACK", uid=self.uid, loop_type=loop_type))

            # 使用 subprocess.Popen 并配合 asyncio，以便可以主动终止进程
            process = None
            try:

                def run_sync():
                    nonlocal process
                    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.user_temp_dir), env=os.environ.copy())
                    return process.communicate(timeout=profile_timeout)

                # 为了能够在外部取消并终止，我们将 run_in_executor 包装在 wait_for 中，但是要注意 cancellation 的处理
                stdout, stderr = await asyncio.wait_for(loop.run_in_executor(None, run_sync), timeout=profile_timeout + 1.0)

                return json.dumps(
                    {
                        "stdout": self._safe_decode(stdout),
                        "stderr": self._safe_decode(stderr),
                        "exit_code": process.returncode if process else -1,
                        "system_info": system_info,
                    },
                    ensure_ascii=False,
                )
            except TimeoutError:
                if process:
                    process.kill()
                return json.dumps(
                    {"error": t(constants.ERR_TOOL_COMMAND_TIMEOUT, timeout=profile_timeout), "system_info": system_info},
                    ensure_ascii=False,
                )
            except subprocess.TimeoutExpired:
                if process:
                    process.kill()
                return json.dumps(
                    {"error": t(constants.ERR_TOOL_COMMAND_TIMEOUT, timeout=profile_timeout), "system_info": system_info},
                    ensure_ascii=False,
                )
            except asyncio.CancelledError:
                if process:
                    process.kill()
                raise
            except Exception as e:
                if process:
                    process.kill()
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        # 正常的异步处理（Linux 或已正确配置的 Windows）
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.user_temp_dir),
                env=os.environ.copy(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=profile_timeout)
                out = self._safe_decode(stdout)
                err = self._safe_decode(stderr)

                return json.dumps(
                    {
                        "stdout": out,
                        "stderr": err,
                        "exit_code": process.returncode,
                        "system_info": system_info,
                    },
                    ensure_ascii=False,
                )
            except TimeoutError:
                if process:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                return json.dumps(
                    {"error": t(constants.ERR_TOOL_COMMAND_TIMEOUT, timeout=profile_timeout), "system_info": system_info},
                    ensure_ascii=False,
                )
            except asyncio.CancelledError:
                if process:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


SHELL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_shell",
        "description": "Execute shell commands. Now protected by LLM Security Auditor at Orchestrator level.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
            },
            "required": ["command"],
        },
    },
}
