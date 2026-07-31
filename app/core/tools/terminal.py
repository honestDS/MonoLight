import hashlib
import json
from typing import Any

from app.core.constants import ERR_TOOL_RUNTIME_CONTEXT_MISSING
from app.core.i18n import t
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import (
    TerminalAction,
    TerminalActionReceipt,
    TerminalCloseRequest,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalResizeRequest,
    TerminalStatusRequest,
    TerminalWriteRequest,
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
        "description": "Write input data to an interactive terminal session. Use '\\n' to submit a line; the service handles platform-specific newline conversion.",
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


class _TerminalExecutor(BaseExecutor):
    def _require_runtime_context(self) -> tuple[Any, str, str]:
        tool_call_id = self.dispatch_context.tool_call_id if self.dispatch_context is not None else None
        if self.db is None or not self.session_id or not tool_call_id:
            raise RuntimeError(t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
        return self.db, self.session_id, tool_call_id

    def _derive_request_id(self, action: TerminalAction) -> str:
        _, session_id, tool_call_id = self._require_runtime_context()
        return hashlib.sha256(f"{session_id}:{tool_call_id}:{action.value}".encode()).hexdigest()

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

    async def _wait_for_command(self, command: Any) -> dict[str, Any]:
        db, _, _ = self._require_runtime_context()
        if command.id is None:
            raise RuntimeError(t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
        return await terminal_session_manager.wait_for_command_result(
            db,
            command.id,
            self._tool_timeout(),
        )

    async def _get_snapshot(self, terminal_session_id: str):
        db, session_id, _ = self._require_runtime_context()
        return await terminal_session_manager.get_snapshot(
            db,
            terminal_session_id,
            self.uid,
            session_id,
        )

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
        db, session_id, _ = self._require_runtime_context()
        request_id = self._derive_request_id(TerminalAction.READ)
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
            request_id,
        )
        result = TerminalReadResult.model_validate(await self._wait_for_command(command))
        return self._dump_model(result)


class TerminalWriteExecutor(_TerminalExecutor):
    requires_audit = True

    async def execute(self, terminal_session_id: str, data: str) -> str:
        request = TerminalWriteRequest(
            terminal_session_id=terminal_session_id,
            request_id=self._derive_request_id(TerminalAction.WRITE),
            data=data,
        )
        return await self._build_action_receipt(request)


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
