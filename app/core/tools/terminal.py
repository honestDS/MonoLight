import asyncio
import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_TERMINAL_COMMAND_TIMEOUT,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    TERMINAL_CLOSE_TOOL_NAME,
    TERMINAL_READ_MAX_BYTES,
    TERMINAL_READ_TOOL_NAME,
    TERMINAL_RESIZE_TOOL_NAME,
    TERMINAL_WRITE_TOOL_NAME,
)
from app.core.crud.session.terminal_watch import terminal_watch_reply_crud
from app.core.exceptions import ResourceNotFoundException
from app.core.i18n import t
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import (
    TerminalAction,
    TerminalActionReceipt,
    TerminalCloseRequest,
    TerminalOutputReadStatus,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalResizeRequest,
    TerminalSessionSnapshot,
    TerminalStatusRequest,
    TerminalWriteRequest,
    TerminalWriteResult,
)
from app.providers.database import AsyncSessionLocal

from .base import BaseExecutor
from .terminal_watch import TERMINAL_WATCH_TOOL_SCHEMA, ensure_terminal_auto_watch

_TERMINAL_SESSION_ID_SCHEMA = {
    "type": "string",
    "minLength": 32,
    "maxLength": 128,
    "pattern": r"^[A-Za-z0-9_-]+$",
}

_TERMINAL_REQUEST_ID_SCHEMA = {
    "type": "string",
    "minLength": 16,
    "maxLength": 128,
    "pattern": r"^[A-Za-z0-9_-]+$",
}


TERMINAL_READ_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TERMINAL_READ_TOOL_NAME,
        "description": (
            "Each response includes terminal status, exit/failure result, and output-buffer bounds. When request_id "
            "is provided, it is the original WRITE request ID; write_request reports only write submission/receipt "
            "status, not CLI task completion. Read one page of retained merged output from an interactive terminal "
            "session by absolute byte offset. "
            "This does not paginate automatically; continue with next_offset, and next_offset < latest_offset means "
            "output remains unread. eof only means the terminal has ended. Set read_output=false to query the "
            "database status and optional write_request without enqueueing a terminal read, waiting for the Worker, "
            "or registering an automatic watch. Pagination is available only while the terminal is running; after "
            "the command ends, final output is delivered by this tool or an observation result, and a reclaimed "
            "terminal cannot be read again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "terminal_session_id": _TERMINAL_SESSION_ID_SCHEMA,
                "request_id": _TERMINAL_REQUEST_ID_SCHEMA,
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": TERMINAL_READ_MAX_BYTES,
                    "default": TERMINAL_READ_MAX_BYTES,
                },
                "read_output": {"type": "boolean", "default": True},
            },
            "required": ["terminal_session_id"],
            "additionalProperties": False,
        },
    },
}

TERMINAL_WRITE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TERMINAL_WRITE_TOOL_NAME,
        "description": (
            "Return only after the Worker completes the PTY write and saves a successful receipt. Does not wait for "
            "output, CLI processing, or task completion. Use '\\n' to submit a line; the service handles "
            f"platform-specific newline conversion. Read output separately with {TERMINAL_READ_TOOL_NAME} from "
            "read_offset. On the first submission, omitting request_id keeps automatic ID derivation. A timeout "
            f"means the write result is unknown; first query {TERMINAL_READ_TOOL_NAME} with the original request_id "
            "and read_output=false. To reuse the same operation, submit again with that original request_id, never "
            "a new ID; do not automatically resubmit."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "terminal_session_id": _TERMINAL_SESSION_ID_SCHEMA,
                "data": {"type": "string", "minLength": 1, "maxLength": 65_536},
                "request_id": _TERMINAL_REQUEST_ID_SCHEMA,
            },
            "required": ["terminal_session_id", "data"],
            "additionalProperties": False,
        },
    },
}

TERMINAL_RESIZE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TERMINAL_RESIZE_TOOL_NAME,
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
        "name": TERMINAL_CLOSE_TOOL_NAME,
        "description": "Close an interactive terminal session, optionally forcing process termination. Reclaim all terminal resources and return the final bounded output.",
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
    TERMINAL_READ_TOOL_SCHEMA,
    TERMINAL_WRITE_TOOL_SCHEMA,
    TERMINAL_RESIZE_TOOL_SCHEMA,
    TERMINAL_CLOSE_TOOL_SCHEMA,
    TERMINAL_WATCH_TOOL_SCHEMA,
]
SHELL_COMPANION_TOOL_NAMES = frozenset(schema["function"]["name"] for schema in SHELL_COMPANION_TOOL_SCHEMAS)


class _TerminalExecutor(BaseExecutor):
    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self._completed_snapshot: TerminalSessionSnapshot | None = None

    def _require_runtime_context(self) -> tuple[Any, str, str]:
        tool_call_id = self.dispatch_context.tool_call_id if self.dispatch_context is not None else None
        if self.db is None or not self.session_id or not tool_call_id:
            raise RuntimeError(t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
        return self.db, self.session_id, tool_call_id

    def _derive_request_id(self, action: TerminalAction) -> str:
        _, session_id, tool_call_id = self._require_runtime_context()
        request_identity = f"{session_id}:{tool_call_id}:{action.value}"
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
            terminal_session_id=command.terminal_session_id,
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
            self._derive_request_id(TerminalAction.READ),
        )
        result = await self._wait_for_command(command)
        snapshot = result.pop("_terminal_snapshot", None)
        self._completed_snapshot = TerminalSessionSnapshot.model_validate(snapshot) if snapshot is not None else None
        return TerminalReadResult.model_validate(result)

    async def _wait_for_resource_release(self, terminal_session_id: str, command_id: int) -> None:
        _, session_id, _ = self._require_runtime_context()
        timeout_seconds = self._tool_timeout()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            try:
                async with AsyncSessionLocal() as status_db:
                    await terminal_session_manager.get_owned_session(
                        status_db,
                        terminal_session_id,
                        self.uid,
                        session_id,
                    )
            except ResourceNotFoundException:
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    t(
                        ERR_TERMINAL_COMMAND_TIMEOUT,
                        command_id=command_id,
                        timeout=timeout_seconds,
                    )
                )
            await asyncio.sleep(min(0.05, remaining))

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
        result = await self._wait_for_command(command)
        snapshot_data = result.pop("_terminal_snapshot", None)
        if snapshot_data is not None:
            snapshot = TerminalSessionSnapshot.model_validate(snapshot_data)
            self._completed_snapshot = snapshot
        else:
            snapshot = await self._get_snapshot(request.terminal_session_id)
        receipt = TerminalActionReceipt(
            terminal_session_id=request.terminal_session_id,
            request_id=request.request_id,
            action=request.action,
            duplicate=not created,
            session_status=snapshot.status,
        )
        response = receipt.model_dump(mode="json")
        if request.action is TerminalAction.CLOSE:
            await self._wait_for_resource_release(request.terminal_session_id, command.id)
            response.update(
                {
                    "exit_code": result.get("exit_code"),
                    "failure_reason": result.get("failure_reason"),
                    "output": result.get("output", ""),
                    "start_offset": result.get("start_offset"),
                    "next_offset": result.get("next_offset"),
                    "latest_offset": result.get("latest_offset"),
                    "output_truncated": result.get("output_truncated", False),
                    "resources_released": True,
                }
            )
        return json.dumps(response, ensure_ascii=False)


class TerminalReadExecutor(_TerminalExecutor):
    requires_audit = False

    async def execute(
        self,
        terminal_session_id: str,
        offset: int = 0,
        max_bytes: int = TERMINAL_READ_MAX_BYTES,
        request_id: str | None = None,
        read_output: bool = True,
    ) -> str:
        status_request = TerminalStatusRequest(terminal_session_id=terminal_session_id, request_id=request_id)
        result = await self._read_once(status_request.terminal_session_id, offset, max_bytes) if read_output else None
        snapshot = self._completed_snapshot or await self._get_snapshot(status_request.terminal_session_id)
        response = snapshot.model_dump(mode="json")
        if status_request.request_id is not None:
            db, session_id, _ = self._require_runtime_context()
            response["write_request"] = await terminal_session_manager.get_write_status(
                db,
                status_request.terminal_session_id,
                self.uid,
                session_id,
                status_request.request_id,
            )
        if result is not None:
            response.update(result.model_dump(mode="json"))
            profile_id = getattr(self.profile, "id", None)
            if result.eof is True and result.read_status is not TerminalOutputReadStatus.EXPIRED and result.next_offset == result.latest_offset and isinstance(self.db, AsyncSession) and isinstance(profile_id, int) and not isinstance(profile_id, bool) and profile_id > 0:
                await terminal_watch_reply_crud.record_terminal_read(
                    self.db,
                    uid=self.uid,
                    session_id=self.session_id,
                    profile_id=profile_id,
                    terminal_session_id=result.terminal_session_id,
                    start_offset=result.start_offset,
                    next_offset=result.next_offset,
                )
        return json.dumps(response, ensure_ascii=False)


class TerminalWriteExecutor(_TerminalExecutor):
    requires_audit = True

    async def execute(self, terminal_session_id: str, data: str, request_id: str | None = None) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._tool_timeout()
        await self._get_snapshot(terminal_session_id)
        request_id = self._derive_request_id(TerminalAction.WRITE) if request_id is None else request_id
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
        try:
            write_result = await self._wait_for_command(command, max(0.0, deadline - loop.time()))
        except TimeoutError as exc:
            return json.dumps(
                {
                    "status": "execution_unknown",
                    "tool_name": TERMINAL_WRITE_TOOL_NAME,
                    "terminal_session_id": terminal_session_id,
                    "request_id": request_id,
                    "error_code": ERR_TERMINAL_COMMAND_TIMEOUT,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        snapshot_data = write_result.pop("_terminal_snapshot", None)
        if snapshot_data is not None:
            latest_snapshot = TerminalSessionSnapshot.model_validate(snapshot_data)
            self._completed_snapshot = latest_snapshot
        else:
            latest_snapshot = await self._get_snapshot(terminal_session_id)
        bytes_written = write_result["bytes_written"]
        output_offset_before_write = write_result.get("output_offset_before_write")
        if not isinstance(output_offset_before_write, int) or isinstance(output_offset_before_write, bool) or output_offset_before_write < 0:
            output_offset_before_write = latest_snapshot.output_buffer.next_offset
        await ensure_terminal_auto_watch(
            self.db,
            uid=self.uid,
            session_id=session_id,
            profile_id=getattr(self.profile, "id", None),
            terminal_session_id=terminal_session_id,
            after_offset=output_offset_before_write,
            session_status=latest_snapshot.status,
        )
        result = TerminalWriteResult(
            terminal_session_id=terminal_session_id,
            request_id=request_id,
            duplicate=not created,
            session_status=latest_snapshot.status,
            bytes_written=bytes_written,
            read_offset=output_offset_before_write,
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
    "TERMINAL_WRITE_TOOL_SCHEMA",
    "TERMINAL_WATCH_TOOL_SCHEMA",
    "TerminalCloseExecutor",
    "TerminalReadExecutor",
    "TerminalResizeExecutor",
    "TerminalWriteExecutor",
]
