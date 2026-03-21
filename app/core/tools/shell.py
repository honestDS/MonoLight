import asyncio
import json
import os
import shlex
from pathlib import Path

from sqlalchemy import select

from app.core.log import get_logger
from app.models.profile import Profile
from app.providers.database import AsyncSessionLocal

# 指令黑名单：严禁在任何情况下执行
BLACKLIST_COMMANDS = ["format", "mkfs", "dd"]
# 风险指令：执行前必须通过特定的二次确认
RISKY_COMMANDS = ["rm", "mv", "cp", "sed", "truncate", "tee"]
# 高危 Shell 逻辑符号
FORBIDDEN_OPERATORS = [";", "&&", "||", "|", ">", ">>", "<", "&", "`", "$("]
# 严禁递归删除的根级敏感路径
SENSITIVE_ROOT_PATHS = [
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", 
    "/proc", "/root", "/run", "/sbin", "/sys", "/usr", "/var"
]

# 特定确认标记（LLM 必须在确认后的下一轮指令前缀中包含此标记以绕过拦截）
CONFIRMATION_TOKEN = "FORCE_EXECUTE_CONFIRMED"

class ShellExecutor:
    logger = get_logger(__name__)

    def __init__(self, project_root: str, uid: str = "default"):
        self.project_root = Path(project_root)
        self.user_temp_dir = self.project_root / "temp" / f"temp_{uid}"
        self._ensure_temp_dir()

    def _ensure_temp_dir(self):
        if not self.user_temp_dir.exists():
            self.user_temp_dir.mkdir(parents=True, exist_ok=True)

    async def _get_profile_timeout(self) -> int:
        """从已激活的 Profile 中获取超时配置"""
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Profile).where(Profile.is_active)
                result = await session.execute(stmt)
                profile = result.scalars().first()
                if (
                    profile
                    and profile.extra_config
                    and isinstance(profile.extra_config, dict)
                ):
                    return profile.extra_config.get("shell_timeout", 30)
        except Exception as e:
            self.logger.error(f"Failed to get profile timeout: {e}")
        return 30

    async def execute(self, command: str, timeout: int = None) -> str:
        if timeout is None:
            timeout = await self._get_profile_timeout()

        try:
            raw_command = command
            is_confirmed = False
            if raw_command.startswith(CONFIRMATION_TOKEN):
                is_confirmed = True
                command = raw_command[len(CONFIRMATION_TOKEN):].strip()

            tokens = shlex.split(command)
            if not tokens:
                return json.dumps({"error": "Empty command"}, ensure_ascii=False)

            command_name = tokens[0]
            
            # 1. 递归删除硬性拦截 (优化参数解析)
            if command_name == "rm":
                has_recursive = any(tok.startswith("-") and ("r" in tok or "R" in tok) for tok in tokens) or "--recursive" in tokens
                has_force = any(tok.startswith("-") and "f" in tok for tok in tokens) or "--force" in tokens
                
                if has_recursive and has_force:
                    for token in tokens[1:]:
                        if token.startswith("-"): continue
                        try:
                            p = Path(token)
                            if p.is_absolute():
                                p = p.resolve()
                            else:
                                p = (self.user_temp_dir / p).resolve()
                            
                            if str(p).rstrip("/") in [path.rstrip("/") for path in SENSITIVE_ROOT_PATHS]:
                                return json.dumps({"error": f"Critical Security Alert: Deletion of '{p}' is forbidden."}, ensure_ascii=False)
                        except: pass

            # 2. 检查指令黑名单
            if command_name in BLACKLIST_COMMANDS:
                return json.dumps({"error": f"Security Alert: Forbidden binary '{command_name}'."}, ensure_ascii=False)

            # 3. 风险指令二次确认逻辑
            if command_name in RISKY_COMMANDS and not is_confirmed:
                reason = f"Security Check Required: Command '{command_name}' is classified as a RISKY operation. To proceed, please present this request to the USER for manual confirmation. If the user approves, you MUST re-send this command with the prefix '{CONFIRMATION_TOKEN} '."
                return json.dumps({
                    "error": "confirmation_required",
                    "reason": reason,
                    "risky_command": command
                }, ensure_ascii=False)

            # 4. 检查逻辑运算符
            is_python_eval = command_name == "python3" and "-c" in tokens
            if not is_python_eval:
                for op in FORBIDDEN_OPERATORS:
                    if op in command:
                        return json.dumps({"error": f"Operator '{op}' forbidden."}, ensure_ascii=False)

            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.user_temp_dir),
                env=os.environ.copy(),
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                return json.dumps({
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                    "exit_code": process.returncode
                }, ensure_ascii=False)
            except asyncio.TimeoutError:
                process.kill()
                return json.dumps({"error": "Command timed out"}, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


SHELL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_shell",
        "description": "Execute shell commands. IMPORTANT: 1. Risky commands (rm, mv, cp, sed, truncate, tee) require USER confirmation. 2. Upon user approval, you MUST prefix the command with 'FORCE_EXECUTE_CONFIRMED '. 3. System root deletion and logic operators are strictly forbidden.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (optional).",
                },
            },
            "required": ["command"],
        },
    },
}
