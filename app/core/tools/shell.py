import asyncio
import json
import os
import subprocess
import sys

from app.core.crud.profile import profile_crud
from app.core.log import get_logger
from app.core.prompts import CONFIRMATION_PREFIX
from app.models.profile import ProfileConfig
from app.providers.database import AsyncSessionLocal

from .base import BaseExecutor


class ShellExecutor(BaseExecutor):
    logger = get_logger(__name__)

    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self.user_temp_dir = self.project_root / "temp" / f"temp_{uid}"
        self._ensure_temp_dir()

    def _ensure_temp_dir(self):
        if not self.user_temp_dir.exists():
            self.user_temp_dir.mkdir(parents=True, exist_ok=True)

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
        # Handle confirmation prefix internally
        if command.startswith(CONFIRMATION_PREFIX):
            command = command.split(" ", 1)[-1]

        t_logger = self.logger.bind(tool_call=True)
        # 获取当前循环类型进行诊断
        loop = asyncio.get_running_loop()
        loop_type = type(loop).__name__
        t_logger.info(f"[{self.uid}] Loop Type: {loop_type} | Executing command: {command}")

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
                        text=True,
                        cwd=str(self.user_temp_dir),
                        env=os.environ.copy(),
                        timeout=profile_timeout,
                        errors="replace",
                    )

                result = await loop.run_in_executor(None, run_sync)

                t_logger.bind(tool_result=True).info(
                    f"[{self.uid}] Sync exit code: {result.returncode}"
                )
                if result.stdout.strip():
                    t_logger.bind(tool_result=True).debug(
                        f"[{self.uid}] STDOUT: {result.stdout[:500]}{'...' if len(result.stdout) > 500 else ''}"
                    )
                if result.stderr.strip():
                    t_logger.bind(tool_result=True).warning(
                        f"[{self.uid}] STDERR: {result.stderr[:500]}{'...' if len(result.stderr) > 500 else ''}"
                    )

                return json.dumps(
                    {
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.returncode,
                    },
                    ensure_ascii=False,
                )
            except subprocess.TimeoutExpired:
                t_logger.error(
                    f"[{self.uid}] Sync command timed out after {profile_timeout}s"
                )
                return json.dumps(
                    {"error": f"Command timed out after {profile_timeout}s"},
                    ensure_ascii=False,
                )
            except Exception as e:
                t_logger.exception(f"[{self.uid}] Sync fallback failed")
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
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=profile_timeout
                )
                out = stdout.decode("utf-8", errors="replace")
                err = stderr.decode("utf-8", errors="replace")

                t_logger.bind(tool_result=True).info(
                    f"[{self.uid}] Command exit code: {process.returncode}"
                )
                if out.strip():
                    out_msg = f"[{self.uid}] STDOUT: {out[:500]}{'...' if len(out) > 500 else ''}"
                    t_logger.bind(tool_result=True).debug(out_msg)
                if err.strip():
                    err_msg = f"[{self.uid}] STDERR: {err[:500]}{'...' if len(err) > 500 else ''}"
                    t_logger.bind(tool_result=True).warning(err_msg)

                return json.dumps(
                    {
                        "stdout": out,
                        "stderr": err,
                        "exit_code": process.returncode,
                    },
                    ensure_ascii=False,
                )
            except TimeoutError:
                if process:
                    process.kill()
                t_logger.error(
                    f"[{self.uid}] Command timed_out after {profile_timeout}s"
                )
                return json.dumps(
                    {"error": f"Command timed out after {profile_timeout}s"},
                    ensure_ascii=False,
                )
        except Exception as e:
            t_logger.exception(f"[{self.uid}] Failed to execute command")
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
