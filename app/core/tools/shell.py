import asyncio
import os
import shlex
import json
from pathlib import Path

# 命令黑名单，防止破坏性操作
BLACKLIST = ["rm -rf"]

class ShellExecutor:
    def __init__(self, project_root: str, uid: str = "test"):
        self.project_root = Path(project_root)
        # TODO: 当用户系统上线后，应取消下面 test 目录的硬编码，改回使用 f"temp_{uid}"
        # 目前由于用户系统尚未对接，统一使用 test 目录进行开发调试
        self.user_temp_dir = self.project_root / "temp" / "test" 
        self._ensure_temp_dir()

    def _ensure_temp_dir(self):
        if not self.user_temp_dir.exists():
            self.user_temp_dir.mkdir(parents=True, exist_ok=True)

    async def execute(self, command: str, timeout: int = 30) -> str:
        # 安全检查：检查命令是否包含黑名单关键词
        for forbidden in BLACKLIST:
            if forbidden in command:
                return json.dumps({
                    "error": f"Security Alert: The command contains forbidden pattern '{forbidden}'. Execution blocked.",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": -1
                }, ensure_ascii=False)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.user_temp_dir),
                env=os.environ.copy()
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                result = {
                    "stdout": stdout.decode('utf-8', errors='replace'),
                    "stderr": stderr.decode('utf-8', errors='replace'),
                    "exit_code": process.returncode,
                    "cwd": str(self.user_temp_dir)
                }
            except asyncio.TimeoutError:
                process.kill()
                result = {
                    "error": f"Command timed out after {timeout} seconds",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": -1
                }

            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

SHELL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_shell",
        "description": "Execute shell commands in an isolated environment.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds.",
                    "default": 30
                }
            },
            "required": ["command"]
        }
    }
}
