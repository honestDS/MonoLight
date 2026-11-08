import asyncio
import json
import ntpath
import posixpath
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.core.constants import (
    ERR_TOOL_COMMAND_TIMEOUT,
    ERR_TOOL_SHELL_BLACKLISTED,
    ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED,
    SHELL_TOOL_NAME,
)
from app.core.crud.audit.audit import audit_crud
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
from app.core.terminal.recovery import (
    PROCESS_WAIT_TIMEOUT_SECONDS,
    capture_terminal_process_identity,
    cleanup_terminal_process_identity,
)
from app.core.utils.system import get_full_system_context

from .base import BaseExecutor
from .terminal_watch import ensure_terminal_auto_watch


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

    @classmethod
    def get_blacklist_description(cls) -> str:
        return f"Shell command blacklist (case-insensitive substring matching): {json.dumps(cls.COMMAND_BLACKLIST, ensure_ascii=False)}. Commands containing any listed substring are rejected before execution; do not attempt them."

    def __init__(
        self,
        project_root: str,
        uid: str = "default",
        *,
        working_directory: str | Path | None = None,
    ):
        super().__init__(project_root, uid)
        self.user_temp_dir = Path(working_directory) if working_directory is not None else get_user_temp_dir(self.project_root, uid)
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

    async def _capture_process_identity_at_start(
        self,
        process: asyncio.subprocess.Process | subprocess.Popen,
    ) -> dict[str, Any] | None:
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return None

        if isinstance(process, asyncio.subprocess.Process) and process.returncode is not None:
            return None

        try:
            process_identity = await capture_terminal_process_identity(pid)
        except Exception as error:
            self.logger.bind(pid=pid, error=str(error)).error("Failed to capture shell process identity")
            return None

        if isinstance(process, asyncio.subprocess.Process):
            if process.returncode is not None:
                return None
        elif isinstance(process, subprocess.Popen):
            if process.poll() is not None:
                return None

        if process_identity is None:
            self.logger.bind(pid=pid).error("Shell process identity was not captured")
        return process_identity

    def _spawn_selector_process(self, command: str, managed_argv: list[str] | None) -> subprocess.Popen:
        process_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(self.user_temp_dir),
            "env": self._build_subprocess_env(),
        }
        if managed_argv is not None:
            return subprocess.Popen(managed_argv, shell=False, **process_kwargs)
        return subprocess.Popen(command, shell=True, **process_kwargs)

    async def _start_non_interactive_process(
        self,
        command: str,
        managed_argv: list[str] | None,
        use_selector: bool,
    ) -> tuple[asyncio.subprocess.Process | subprocess.Popen, dict[str, Any] | None]:
        if use_selector:
            process = await asyncio.to_thread(self._spawn_selector_process, command, managed_argv)
        elif managed_argv is not None:
            process = await asyncio.create_subprocess_exec(
                *managed_argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.user_temp_dir),
                env=self._build_subprocess_env(),
                **({"start_new_session": True} if sys.platform != "win32" else {}),
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.user_temp_dir),
                env=self._build_subprocess_env(),
                **({"start_new_session": True} if sys.platform != "win32" else {}),
            )

        process_identity = await self._capture_process_identity_at_start(process)
        return process, process_identity

    async def _cleanup_process_tree(
        self,
        startup_task: asyncio.Task[tuple[asyncio.subprocess.Process | subprocess.Popen, dict[str, Any] | None]],
        read_task: asyncio.Task[tuple[bytes, bytes]] | None,
        profile_timeout: float,
        use_selector: bool,
    ) -> None:
        try:
            process, process_identity = await asyncio.shield(startup_task)
        except Exception:
            return

        pid = process.pid
        if process_identity is not None:
            try:
                cleanup_result = await cleanup_terminal_process_identity(process_identity)
            except Exception as error:
                self.logger.bind(pid=pid, error=str(error)).error("Shell process tree cleanup failed")
            else:
                if cleanup_result.errors:
                    self.logger.bind(pid=pid, errors=cleanup_result.errors).error("Shell process tree cleanup reported errors")

        if sys.platform == "win32":
            try:
                if process.returncode is None:
                    # Windows uses the held process handle; POSIX must not fall back to a PID.
                    process.kill()
            except ProcessLookupError:
                pass
            except Exception as error:
                self.logger.bind(pid=pid, error=str(error)).error("Failed to kill shell process")

        if read_task is None:
            if use_selector:
                read_task = asyncio.create_task(asyncio.to_thread(process.communicate, timeout=profile_timeout))
            else:
                read_task = asyncio.create_task(process.communicate())

        try:
            await asyncio.wait_for(
                asyncio.shield(read_task),
                timeout=PROCESS_WAIT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            self.logger.bind(pid=pid, timeout=PROCESS_WAIT_TIMEOUT_SECONDS).warning("Shell output collection timed out")
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
        except asyncio.CancelledError:
            if read_task.cancelled():
                await asyncio.gather(read_task, return_exceptions=True)
            else:
                raise
        except Exception:
            pass

        try:
            if process.returncode is None:
                if use_selector:
                    await asyncio.to_thread(process.wait, timeout=PROCESS_WAIT_TIMEOUT_SECONDS)
                else:
                    await asyncio.wait_for(process.wait(), timeout=PROCESS_WAIT_TIMEOUT_SECONDS)
        except (TimeoutError, subprocess.TimeoutExpired):
            self.logger.bind(pid=pid, timeout=PROCESS_WAIT_TIMEOUT_SECONDS).warning("Shell process reap timed out")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.logger.bind(pid=pid, error=str(error)).error("Failed to reap shell process")

    async def _await_cleanup_task(self, cleanup_task: asyncio.Task[None]) -> bool:
        cancellation_seen = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancellation_seen = True

        try:
            await cleanup_task
        except asyncio.CancelledError:
            cancellation_seen = True
        return cancellation_seen

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
        await ensure_terminal_auto_watch(
            self.db,
            uid=self.uid,
            session_id=self.session_id,
            profile_id=self.profile.id,
            terminal_session_id=snapshot.terminal_session_id,
            after_offset=snapshot.output_buffer.next_offset,
            session_status=snapshot.status,
        )
        handoff = ShellInteractiveHandoffResult(
            terminal_session_id=snapshot.terminal_session_id,
            status=snapshot.status,
            output_buffer=snapshot.output_buffer,
            exit_code=snapshot.exit_code,
            failure_reason=snapshot.failure_reason,
        )
        return json.dumps(handoff.model_dump(mode="json"), ensure_ascii=False)

    async def _execute_non_interactive(
        self,
        command: str,
        managed_argv: list[str] | None,
        profile_timeout: float,
        system_info: str,
        use_selector: bool,
    ) -> str:
        process: asyncio.subprocess.Process | subprocess.Popen | None = None
        read_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        successful = False
        startup_task: asyncio.Task[tuple[asyncio.subprocess.Process | subprocess.Popen, dict[str, Any] | None]] = asyncio.create_task(self._start_non_interactive_process(command, managed_argv, use_selector))

        try:
            process, _ = await asyncio.shield(startup_task)
            if use_selector:
                read_task = asyncio.create_task(asyncio.to_thread(process.communicate, timeout=profile_timeout))
            else:
                read_task = asyncio.create_task(process.communicate())

            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(read_task),
                timeout=profile_timeout,
            )
            result = self._build_result(stdout, stderr, process.returncode, system_info)
            successful = True
            return result
        except (TimeoutError, subprocess.TimeoutExpired):
            return self._build_timeout_result(profile_timeout, system_info)
        except Exception as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)
        finally:
            if not successful:
                cleanup_task = asyncio.create_task(
                    self._cleanup_process_tree(
                        startup_task,
                        read_task,
                        profile_timeout,
                        use_selector,
                    )
                )
                if await self._await_cleanup_task(cleanup_task):
                    raise asyncio.CancelledError

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

        use_selector = sys.platform == "win32" and "Proactor" not in loop_type
        if use_selector:
            t_logger.warning(t("LOG_SHELL_WINDOWS_SYNC_FALLBACK", uid=self.uid, loop_type=loop_type))

        return await self._execute_non_interactive(
            command,
            managed_argv,
            profile_timeout,
            system_info,
            use_selector,
        )


SHELL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": SHELL_TOOL_NAME,
        "description": (
            "Execute the original command and return its result. "
            f"{ShellExecutor.get_blacklist_description()} "
            "The required execution_mode describes the input/output and lifecycle of the same command process, not a programming language; this mode is independent of programming language. "
            "Both modes run the same original command unchanged; never replace or wrap the command in Python, a Python REPL, or another executable to satisfy this choice. "
            "Prefer non_interactive when the command needs no later stdin and should exit within tool_timeout. Continuous or streaming output, or a command that is merely slow, is not by itself a reason to choose interactive. "
            "Choose interactive only when the same original command process needs later terminal input, depends on TTY behavior, or must keep running after this tool call returns. REPLs, debuggers, TUIs, interactive prompts, and persistent services are categories that may meet these conditions. "
            "For local file operations, use the dedicated file or knowledge-base tools instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Execute the exact original command itself; do not replace or wrap the command in Python, a Python REPL, or another executable to satisfy execution_mode.",
                },
                "execution_mode": {
                    "type": "string",
                    "enum": [mode.value for mode in ShellExecutionMode],
                    "description": (
                        "Select how the same command process handles input/output and lifecycle; execution_mode is not a programming language and is independent of programming language. "
                        "Both modes run the same original command unchanged; never replace or wrap the command in Python, a Python REPL, or another executable to satisfy this choice. "
                        "Prefer non_interactive when the command needs no later stdin and should exit within tool_timeout. Continuous or streaming output, or a command that is merely slow, is not by itself a reason to choose interactive. "
                        "Choose interactive only when the same original command process needs later terminal input, depends on TTY behavior, or must keep running after this tool call returns. REPLs, debuggers, TUIs, interactive prompts, and persistent services are categories that may meet these conditions."
                    ),
                },
            },
            "required": ["command", "execution_mode"],
            "additionalProperties": False,
        },
    },
}
