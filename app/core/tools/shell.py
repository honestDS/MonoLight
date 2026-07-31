import asyncio
import json
import ntpath
import posixpath
import re
import shlex
import subprocess
import sys

from app.core.constants import (
    ERR_TOOL_COMMAND_TIMEOUT,
    ERR_TOOL_SHELL_BLACKLISTED,
    ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED,
)
from app.core.crud.audit import audit_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
from app.core.terminal import (
    ALL_TERMINAL_ACTIONS,
    ShellExecutionMode,
    ShellInteractiveHandoffResult,
    validate_shell_execution_mode,
)
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.process_config import build_subprocess_env
from app.core.utils.system import get_full_system_context

from .base import BaseExecutor


class ShellExecutor(BaseExecutor):
    requires_audit = True
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
        return build_subprocess_env()

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
        """从当前执行配置获取超时配置"""
        try:
            return self.cfg.tool.tool_timeout
        except Exception as e:
            self.logger.error(t("LOG_SHELL_PROFILE_TIMEOUT_FAILED", error=str(e)))
        return 30.0

    async def _execute_interactive(self, command: str) -> str:
        dispatch_context = self.dispatch_context
        if self.cfg is None or self.db is None or self.profile is None or self.profile.id is None or not self.session_id or dispatch_context is None or not dispatch_context.tool_call_id:
            raise RuntimeError(t(ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED))

        from app.core.audit.service import is_audit_configured

        tool_call_id = dispatch_context.tool_call_id
        original_tool_call_id = tool_call_id
        audit_record_id = None
        audit_execution_record_id = None
        if is_audit_configured(self.cfg):
            binding = await audit_crud.get_running_execution_binding(
                self.db,
                new_tool_call_id=tool_call_id,
            )
            if binding is None:
                raise RuntimeError(t(ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED))

            audit_record, audit_execution = binding
            if audit_record.uid != self.uid or audit_record.session_id != self.session_id:
                raise RuntimeError(t(ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED))
            if audit_record.id is None or audit_execution.id is None:
                raise RuntimeError(t(ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED))

            details = await audit_crud.list_tool_details(self.db, audit_record.id)
            detail = next(
                (item for item in details if item.id == audit_execution.audit_tool_detail_id),
                None,
            )
            if detail is None:
                raise RuntimeError(t(ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED))
            original_tool_call_id = detail.original_tool_call_id
            audit_record_id = audit_record.id
            audit_execution_record_id = audit_execution.id

        terminal_session = await terminal_session_manager.get_or_create_session_for_execution(
            self.db,
            uid=self.uid,
            session_id=self.session_id,
            profile_id=self.profile.id,
            original_tool_call_id=original_tool_call_id,
            audit_record_id=audit_record_id,
            audit_execution_record_id=audit_execution_record_id,
            command=command,
            working_directory=str(self.user_temp_dir),
            allowed_actions=ALL_TERMINAL_ACTIONS,
        )
        snapshot = await terminal_session_manager.get_snapshot(
            self.db,
            terminal_session.terminal_session_id,
            self.uid,
            self.session_id,
        )
        handoff = ShellInteractiveHandoffResult(
            terminal_session_id=snapshot.terminal_session_id,
            status=snapshot.status,
            output_buffer=snapshot.output_buffer,
            exit_code=snapshot.exit_code,
            failure_reason=snapshot.failure_reason,
        )
        return json.dumps(handoff.model_dump(mode="json"), ensure_ascii=False)

    async def execute(self, command: str, execution_mode: ShellExecutionMode | str) -> str:
        execution_mode = validate_shell_execution_mode(execution_mode)

        blacklisted = self.check_blacklist(command)
        if blacklisted:
            system_info = get_full_system_context()
            return json.dumps(
                {
                    "stdout": t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted),
                    "stderr": "",
                    "exit_code": 1,
                    "system_info": system_info,
                },
                ensure_ascii=False,
            )

        if execution_mode is ShellExecutionMode.INTERACTIVE:
            try:
                return await self._execute_interactive(command)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return json.dumps({"error": str(exc)}, ensure_ascii=False)

        system_info = get_full_system_context()
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
            "Execute a shell command using the required execution_mode and return its output. "
            "Choose non_interactive for commands that exit on their own without terminal input, or interactive for commands that require a live terminal session. "
            "For local file operations, use the dedicated file or knowledge-base tools instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute. Select the execution_mode that matches whether the command exits on its own or requires a live terminal session.",
                },
                "execution_mode": {
                    "type": "string",
                    "enum": [mode.value for mode in ShellExecutionMode],
                    "description": "Execution mode: choose non_interactive for commands that exit on their own without terminal input; choose interactive for commands that require a live terminal session.",
                },
            },
            "required": ["command", "execution_mode"],
            "additionalProperties": False,
        },
    },
}
