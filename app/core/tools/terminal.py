import asyncio
import hashlib
import json
from typing import Any

from app.core.constants import ERR_TOOL_RUNTIME_CONTEXT_MISSING
from app.core.i18n import t
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalActionReceipt,
    TerminalCloseRequest,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalResizeRequest,
    TerminalStatusRequest,
    TerminalWriteRequest,
    TerminalWriteResult,
)

from .base import BaseExecutor

_TERMINAL_SESSION_ID_SCHEMA = {
    "type": "string",
    "minLength": 32,
    "maxLength": 128,
    "pattern": r"^[A-Za-z0-9_-]+$",
}


TERMINAL_STATUS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal_status",
        "description": "Get the current status, output bounds, and exit result of an interactive terminal session.",
        "parameters": {
            "type": "object",
            "properties": {
                "terminal_session_id": _TERMINAL_SESSION_ID_SCHEMA,
            },
            "required": ["terminal_session_id"],
            "additionalProperties": False,
        },
    },
}

TERMINAL_READ_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal_read",
        "description": "Read retained merged output from an interactive terminal session by byte offset.",
        "parameters": {
            "type": "object",
            "properties": {
                "terminal_session_id": _TERMINAL_SESSION_ID_SCHEMA,
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1_048_576, "default": 65_536},
            },
            "required": ["terminal_session_id"],
            "additionalProperties": False,
        },
    },
}

TERMINAL_WRITE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal_write",
        "description": (
            "Write input data to an interactive terminal session, then automatically wait for and read newly merged "
            "output for up to the tool timeout. Use '\\n' to submit a line; the service handles platform-specific "
            "newline conversion. If read_timed_out is true, the terminal remains running; use terminal_read from "
            "read_offset to continue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "terminal_session_id": _TERMINAL_SESSION_ID_SCHEMA,
                "data": {"type": "string", "minLength": 1, "maxLength": 65_536},
            },
            "required": ["terminal_session_id", "data"],
            "additionalProperties": False,
        },
    },
}

TERMINAL_RESIZE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal_resize",
        "description": "Resize an interactive terminal session.",
        "parameters": {
            "type": "object",
            "properties": {
                "terminal_session_id": _TERMINAL_SESSION_ID_SCHEMA,
                "columns": {"type": "integer", "minimum": 1, "maximum": 1_000},
                "rows": {"type": "integer", "minimum": 1, "maximum": 1_000},
            },
            "required": ["terminal_session_id", "columns", "rows"],
            "additionalProperties": False,
        },
    },
}

TERMINAL_CLOSE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal_close",
        "description": "Close an interactive terminal session, optionally forcing process termination.",
        "parameters": {
            "type": "object",
            "properties": {
                "terminal_session_id": _TERMINAL_SESSION_ID_SCHEMA,
                "force": {"type": "boolean", "default": False},
            },
            "required": ["terminal_session_id"],
            "additionalProperties": False,
        },
    },
}


SHELL_COMPANION_TOOL_SCHEMAS = [
    TERMINAL_STATUS_TOOL_SCHEMA,
    TERMINAL_READ_TOOL_SCHEMA,
    TERMINAL_WRITE_TOOL_SCHEMA,
    TERMINAL_RESIZE_TOOL_SCHEMA,
    TERMINAL_CLOSE_TOOL_SCHEMA,
]
SHELL_COMPANION_TOOL_NAMES = frozenset(schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS)

_TERMINAL_WRITE_READ_POLL_INTERVAL_SECONDS = 0.05
_TERMINAL_WRITE_READ_STABILITY_SECONDS = 1.0


class _TerminalExecutor(BaseExecutor):
    def _require_runtime_context(self) -> tuple[Any, str, str]:
        tool_call_id = self.dispatch_context.tool_call_id if self.dispatch_context is not None else None
        if self.db is None or not self.session_id or not tool_call_id:
            raise RuntimeError(t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
        return self.db, self.session_id, tool_call_id

    def _derive_request_id(self, action: TerminalAction, discriminator: str | None = None) -> str:
        _, session_id, tool_call_id = self._require_runtime_context()
        request_identity = f"{session_id}:{tool_call_id}:{action.value}"
        if discriminator is not None:
            request_identity = f"{request_identity}:{discriminator}"
        return hashlib.sha256(request_identity.encode()).hexdigest()

    def _tool_timeout(self) -> float:
        try:
            timeout = self.cfg.tool.tool_timeout
        except Exception:
            return 30.0
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            return float(timeout)
        return 30.0

    @staticmethod
    def _dump_model(model: Any) -> str:
        return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)

    async def _wait_for_command(self, command: Any, timeout_seconds: float | None = None) -> dict[str, Any]:
        db, _, _ = self._require_runtime_context()
        if command.id is None:
            raise RuntimeError(t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
        return await terminal_session_manager.wait_for_command_result(
            db,
            command.id,
            self._tool_timeout() if timeout_seconds is None else timeout_seconds,
        )

    async def _get_snapshot(self, terminal_session_id: str):
        db, session_id, _ = self._require_runtime_context()
        return await terminal_session_manager.get_snapshot(
            db,
            terminal_session_id,
            self.uid,
            session_id,
        )

    async def _read_once(
        self,
        terminal_session_id: str,
        offset: int,
        max_bytes: int,
        *,
        request_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> TerminalReadResult:
        db, session_id, _ = self._require_runtime_context()
        request = TerminalReadRequest(
            terminal_session_id=terminal_session_id,
            offset=offset,
            max_bytes=max_bytes,
        )
        command, _ = await terminal_session_manager.enqueue_read(
            db,
            self.uid,
            session_id,
            request,
            request_id or self._derive_request_id(TerminalAction.READ),
        )
        return TerminalReadResult.model_validate(await self._wait_for_command(command, timeout_seconds))

    async def _build_action_receipt(
        self,
        request: TerminalWriteRequest | TerminalResizeRequest | TerminalCloseRequest,
    ) -> str:
        db, session_id, _ = self._require_runtime_context()
        command, created = await terminal_session_manager.enqueue_control(
            db,
            self.uid,
            session_id,
            request,
        )
        await self._wait_for_command(command)
        snapshot = await self._get_snapshot(request.terminal_session_id)
        receipt = TerminalActionReceipt(
            terminal_session_id=request.terminal_session_id,
            request_id=request.request_id,
            action=request.action,
            duplicate=not created,
            session_status=snapshot.status,
        )
        return self._dump_model(receipt)


class TerminalStatusExecutor(_TerminalExecutor):
    requires_audit = False

    async def execute(self, terminal_session_id: str) -> str:
        request = TerminalStatusRequest(terminal_session_id=terminal_session_id)
        self._derive_request_id(request.action)
        return self._dump_model(await self._get_snapshot(request.terminal_session_id))


class TerminalReadExecutor(_TerminalExecutor):
    requires_audit = False

    async def execute(self, terminal_session_id: str, offset: int = 0, max_bytes: int = 65_536) -> str:
        result = await self._read_once(terminal_session_id, offset, max_bytes)
        return self._dump_model(result)


class TerminalWriteExecutor(_TerminalExecutor):
    requires_audit = True

    async def execute(self, terminal_session_id: str, data: str) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._tool_timeout()
        snapshot_before_write = await self._get_snapshot(terminal_session_id)
        request_id = self._derive_request_id(TerminalAction.WRITE)
        request = TerminalWriteRequest(
            terminal_session_id=terminal_session_id,
            request_id=request_id,
            data=data,
        )
        db, session_id, _ = self._require_runtime_context()
        command, created = await terminal_session_manager.enqueue_control(
            db,
            self.uid,
            session_id,
            request,
        )
        write_result = await self._wait_for_command(command, max(0.0, deadline - loop.time()))
        bytes_written = write_result["bytes_written"]
        output_offset_before_write = write_result.get("output_offset_before_write")
        if not isinstance(output_offset_before_write, int) or isinstance(output_offset_before_write, bool) or output_offset_before_write < 0:
            output_offset_before_write = snapshot_before_write.output_buffer.next_offset

        latest_snapshot = snapshot_before_write
        observed_offset = output_offset_before_write
        stable_since: float | None = None
        read_ready = False
        while True:
            latest_snapshot = await self._get_snapshot(terminal_session_id)
            current_offset = latest_snapshot.output_buffer.next_offset
            now = loop.time()
            if current_offset > output_offset_before_write:
                if current_offset != observed_offset:
                    observed_offset = current_offset
                    stable_since = now
                elif stable_since is not None and now - stable_since >= _TERMINAL_WRITE_READ_STABILITY_SECONDS:
                    read_ready = True
            if read_ready or latest_snapshot.status in TERMINAL_SESSION_FINAL_STATUSES:
                break

            remaining = deadline - now
            if remaining <= 0:
                break
            await asyncio.sleep(min(_TERMINAL_WRITE_READ_POLL_INTERVAL_SECONDS, remaining))

        if not read_ready and latest_snapshot.status not in TERMINAL_SESSION_FINAL_STATUSES and loop.time() >= deadline:
            result = TerminalWriteResult(
                terminal_session_id=terminal_session_id,
                request_id=request_id,
                duplicate=not created,
                session_status=latest_snapshot.status,
                bytes_written=bytes_written,
                read_offset=output_offset_before_write,
                read_timed_out=True,
            )
            return self._dump_model(result)

        try:
            read_result = await self._read_once(
                terminal_session_id,
                output_offset_before_write,
                65_536,
                request_id=self._derive_request_id(
                    TerminalAction.READ,
                    f"{request_id}:{output_offset_before_write}",
                ),
                timeout_seconds=max(0.0, deadline - loop.time()),
            )
        except TimeoutError:
            result = TerminalWriteResult(
                terminal_session_id=terminal_session_id,
                request_id=request_id,
                duplicate=not created,
                session_status=latest_snapshot.status,
                bytes_written=bytes_written,
                read_offset=output_offset_before_write,
                read_timed_out=True,
            )
            return self._dump_model(result)

        latest_snapshot = await self._get_snapshot(terminal_session_id)
        result = TerminalWriteResult(
            terminal_session_id=terminal_session_id,
            request_id=request_id,
            duplicate=not created,
            session_status=latest_snapshot.status,
            bytes_written=bytes_written,
            read_offset=output_offset_before_write,
            read_timed_out=False,
            read_result=read_result,
        )
        return self._dump_model(result)


class TerminalResizeExecutor(_TerminalExecutor):
    requires_audit = False

    async def execute(self, terminal_session_id: str, columns: int, rows: int) -> str:
        request = TerminalResizeRequest(
            terminal_session_id=terminal_session_id,
            request_id=self._derive_request_id(TerminalAction.RESIZE),
            columns=columns,
            rows=rows,
        )
        return await self._build_action_receipt(request)


class TerminalCloseExecutor(_TerminalExecutor):
    requires_audit = True

    async def execute(self, terminal_session_id: str, force: bool = False) -> str:
        request = TerminalCloseRequest(
            terminal_session_id=terminal_session_id,
            request_id=self._derive_request_id(TerminalAction.CLOSE),
            force=force,
        )
        return await self._build_action_receipt(request)


__all__ = [
    "SHELL_COMPANION_TOOL_NAMES",
    "SHELL_COMPANION_TOOL_SCHEMAS",
    "TERMINAL_CLOSE_TOOL_SCHEMA",
    "TERMINAL_READ_TOOL_SCHEMA",
    "TERMINAL_RESIZE_TOOL_SCHEMA",
    "TERMINAL_STATUS_TOOL_SCHEMA",
    "TERMINAL_WRITE_TOOL_SCHEMA",
    "TerminalCloseExecutor",
    "TerminalReadExecutor",
    "TerminalResizeExecutor",
    "TerminalStatusExecutor",
    "TerminalWriteExecutor",
]
