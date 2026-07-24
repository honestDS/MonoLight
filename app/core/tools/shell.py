import asyncio
import json
import ntpath
import os
import posixpath
import re
import shlex
import subprocess
import sys
import sysconfig

from app.core.constants import ERR_TOOL_COMMAND_TIMEOUT, ERR_TOOL_SHELL_BLACKLISTED
from app.core.crud.profile import profile_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
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

    def _build_result(self, stdout: bytes, stderr: bytes, exit_code: int, system_info: str) -> str:
        return json.dumps(
            {
                "stdout": self._safe_decode(stdout),
                "stderr": self._safe_decode(stderr),
                "exit_code": exit_code,
                "system_info": system_info,
            },
            ensure_ascii=False,
        )

    def _build_timeout_result(self, profile_timeout: float, system_info: str) -> str:
        return json.dumps(
            {"error": t(ERR_TOOL_COMMAND_TIMEOUT, timeout=profile_timeout), "system_info": system_info},
            ensure_ascii=False,
        )

    def _build_subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        scripts_dir = sysconfig.get_path("scripts")
        if scripts_dir:
            env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")
        if sys.prefix != sys.base_prefix:
            env["VIRTUAL_ENV"] = sys.prefix
        return env

    async def _execute_argv(self, argv: list[str], profile_timeout: float, system_info: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.user_temp_dir),
            env=self._build_subprocess_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=profile_timeout)
            return self._build_result(stdout, stderr, process.returncode, system_info)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return self._build_timeout_result(profile_timeout, system_info)
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise

    def _execute_argv_sync(self, argv: list[str], profile_timeout: float, system_info: str) -> str:
        process = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.user_temp_dir),
                env=self._build_subprocess_env(),
            )
            stdout, stderr = process.communicate(timeout=profile_timeout)
            return self._build_result(stdout, stderr, process.returncode, system_info)
        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            return self._build_timeout_result(profile_timeout, system_info)

    def _normalize_python_inline_code(self, code: str) -> str:
        compound_keywords = r"async\s+def|class|def|if|elif|else|for|while|try|except|finally|with|match"
        return re.sub(rf";[ \t]*(?=(?:{compound_keywords})\b)", "\n", code)

    def _resolve_python_inline_executable(self, executable: str) -> str:
        if ntpath.dirname(executable) or posixpath.dirname(executable):
            return executable
        return sys.executable

    def _has_shell_composition(self, args: list[str]) -> bool:
        return any(re.search(r"[&|;<>]", arg) for arg in args)

    def _extract_python_inline_command(self, command: str) -> list[str] | None:
        try:
            parts = shlex.split(command, posix=True)
        except ValueError:
            return None

        if len(parts) < 3:
            return None

        executable = ntpath.basename(posixpath.basename(parts[0])).lower()
        if executable not in {"python", "python3", "python.exe", "py", "py.exe"}:
            return None

        try:
            code_flag_index = parts.index("-c")
        except ValueError:
            return None

        if code_flag_index + 1 >= len(parts):
            return None

        if self._has_shell_composition(parts[code_flag_index + 2 :]):
            return None

        parts[0] = self._resolve_python_inline_executable(parts[0])
        parts[code_flag_index + 1] = self._normalize_python_inline_code(parts[code_flag_index + 1])
        return parts

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
        system_info = get_full_system_context()

        blacklisted = self.check_blacklist(command)
        if blacklisted:
            return json.dumps(
                {
                    "stdout": t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted),
                    "stderr": "",
                    "exit_code": 1,
                    "system_info": system_info,
                },
                ensure_ascii=False,
            )

        t_logger = self.logger.bind(tool_call=True)
        loop = asyncio.get_running_loop()
        loop_type = type(loop).__name__
        profile_timeout = await self._get_profile_timeout()
        managed_argv = self._extract_python_inline_command(command)

        if sys.platform == "win32" and "Proactor" not in loop_type:
            t_logger.warning(t("LOG_SHELL_WINDOWS_SYNC_FALLBACK", uid=self.uid, loop_type=loop_type))

            process = None
            try:

                def run_sync():
                    nonlocal process
                    if managed_argv is not None:
                        return self._execute_argv_sync(managed_argv, profile_timeout, system_info)
                    process = subprocess.Popen(command, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.user_temp_dir), env=self._build_subprocess_env())
                    return process.communicate(timeout=profile_timeout)

                result = await asyncio.wait_for(loop.run_in_executor(None, run_sync), timeout=profile_timeout + 1.0)

                if isinstance(result, str):
                    return result

                stdout, stderr = result
                return self._build_result(stdout, stderr, process.returncode if process else -1, system_info)
            except TimeoutError:
                if process:
                    process.kill()
                return self._build_timeout_result(profile_timeout, system_info)
            except subprocess.TimeoutExpired:
                if process:
                    process.kill()
                return self._build_timeout_result(profile_timeout, system_info)
            except asyncio.CancelledError:
                if process:
                    process.kill()
                raise
            except Exception as e:
                if process:
                    process.kill()
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        try:
            if managed_argv is not None:
                return await self._execute_argv(managed_argv, profile_timeout, system_info)

            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.user_temp_dir),
                env=self._build_subprocess_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=profile_timeout)
                return self._build_result(stdout, stderr, process.returncode, system_info)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return self._build_timeout_result(profile_timeout, system_info)
            except asyncio.CancelledError:
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
        "description": (
            "Execute a single non-interactive shell command and return its output. "
            "Do not use interactive programs (for example: vim, less, more, top, htop, nano, ipython, bpython, python -i, notepad, textedit, open, start, explorer, sqlite3, mysql, psql, gdb, lldb, man, woman, most, pg, watch, tail -f, less +F, journalctl -f), "
            "and do not use commands that spawn long-running or GUI child processes (for example: editors, file managers, browsers, image viewers, terminal emulators, IDEs, or any command that waits for user input before exiting). "
            "This tool runs with stdin closed, and interactive or child processes can block the shell until timeout. "
            "For local file operations, use the dedicated file or knowledge-base tools instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Single non-interactive shell command. Do not pass interactive commands, GUI launchers, or commands that wait for user input; only use commands that exit on their own.",
                },
            },
            "required": ["command"],
        },
    },
}
