import asyncio
import json
import os
from pathlib import Path
from app.core.log import get_logger
from app.models.profile import ProfileConfig
from app.providers.database import AsyncSessionLocal
from app.core.crud.profile import profile_crud
from app.core.prompts import CONFIRMATION_PREFIX
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

        # 强制使用数据库配置的超时
        profile_timeout = await self._get_profile_timeout()

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
                return json.dumps(
                    {
                        "stdout": stdout.decode("utf-8", errors="replace"),
                        "stderr": stderr.decode("utf-8", errors="replace"),
                        "exit_code": process.returncode,
                    },
                    ensure_ascii=False,
                )
            except asyncio.TimeoutError:
                process.kill()
                return json.dumps(
                    {"error": f"Command timed out after {profile_timeout}s"},
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
