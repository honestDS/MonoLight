import asyncio
import json
import os
import subprocess
import sys

from app.core.crud.profile import profile_crud
from app.core.log import get_logger
from app.core.prompts import CONFIRMATION_PREFIX
from app.core.utils.system import get_full_system_context
from app.models.profile import ProfileConfig
from app.providers.database import AsyncSessionLocal

from .base import BaseExecutor


class ShellExecutor(BaseExecutor):
    logger = get_logger(__name__)
    COMMAND_BLACKLIST = [
        "python -c",
        "python3 -c",
        "powershell",
    ]

    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self.user_temp_dir = self.project_root / "temp" / f"temp_{uid}"
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
                profile = await profile_crud.get_active(session)
                if profile and profile.configs:
                    cfg = ProfileConfig.model_validate(profile.configs)
                    return cfg.tool.shell_timeout
        except Exception as e:
            self.logger.error(f"Failed to get profile timeout: {e}")
        return 30.0

    async def execute(self, command: str) -> str:
        # 删除可能存在的确认前缀
        if command.startswith(CONFIRMATION_PREFIX):
            command = command.split(" ", 1)[-1]

        system_info = get_full_system_context()

        for blacklisted in self.COMMAND_BLACKLIST:
            if blacklisted.lower() in command.lower():
                return json.dumps(
                    {
                        "stdout": f"不允许使用shell工具执行该命令: {blacklisted},禁止命令列表: {self.COMMAND_BLACKLIST}",
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
            t_logger.warning(f"[{self.uid}] Using synchronous fallback for Windows {loop_type}")
            try:

                def run_sync():
                    return subprocess.run(
                        command,
                        shell=True,
                        capture_output=True,
                        text=False,
                        cwd=str(self.user_temp_dir),
                        env=os.environ.copy(),
                        timeout=profile_timeout,
                    )

                result = await loop.run_in_executor(None, run_sync)

                return json.dumps(
                    {
                        "stdout": self._safe_decode(result.stdout),
                        "stderr": self._safe_decode(result.stderr),
                        "exit_code": result.returncode,
                        "system_info": system_info,
                    },
                    ensure_ascii=False,
                )
            except subprocess.TimeoutExpired:
                return json.dumps(
                    {"error": f"Command timed out after {profile_timeout}s system_info: {system_info}"},
                    ensure_ascii=False,
                )
            except Exception as e:
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
                    process.kill()
                return json.dumps(
                    {"error": f"Command timed out after {profile_timeout}s system_info: {system_info}"},
                    ensure_ascii=False,
                )
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
