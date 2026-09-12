import asyncio
import hashlib
import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_TERMINAL_ACTION_NOT_ALLOWED,
    ERR_TERMINAL_COMMAND_FAILED,
    ERR_TERMINAL_COMMAND_REQUEST_CONFLICT,
    ERR_TERMINAL_COMMAND_TIMEOUT,
    ERR_TERMINAL_MUTATING_REQUEST_REQUIRED,
    ERR_TERMINAL_PROCESS_ACTION_INVALID,
    ERR_TERMINAL_PTY_CLOSED,
    ERR_TERMINAL_SESSION_ACCESS_DENIED,
    ERR_TERMINAL_SESSION_NOT_FOUND,
    ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED,
)
from app.core.crud.terminal.session import (
    terminal_control_command_crud,
    terminal_session_crud,
)
from app.core.exceptions import ForbiddenException, ParameterException, ResourceNotFoundException
from app.core.i18n import t
from app.core.terminal.schemas import (
    ALL_TERMINAL_ACTIONS,
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalOutputBufferState,
    TerminalPermissionScope,
    TerminalReadRequest,
    TerminalSessionSnapshot,
    TerminalStatusRequest,
)
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession

from .manager_common import (
    _TERMINAL_MUTATING_REQUEST_TYPES,
    TerminalMutatingRequest,
)
from .session_runtime_common import (
    _read_without_driver_result,
    _terminal_output_buffer,
)

__all__ = [
    "TerminalSessionManager",
]


class TerminalSessionManager:
    async def create_session(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        original_tool_call_id: str,
        audit_record_id: int | None,
        audit_execution_record_id: int | None,
        command: str,
        working_directory: str,
        allowed_actions: Iterable[TerminalAction | str],
        output_capacity_bytes: int = 1_048_576,
        terminal_session_id: str | None = None,
        commit: bool = True,
    ) -> TerminalSession:
        self._validate_audit_binding(audit_record_id, audit_execution_record_id)
        if terminal_session_id is not None:
            terminal_session_id = TerminalStatusRequest(terminal_session_id=terminal_session_id).terminal_session_id
        output_buffer = TerminalOutputBufferState(
            capacity_bytes=output_capacity_bytes,
            oldest_offset=0,
            next_offset=0,
            oldest_sequence=1,
            next_sequence=1,
        )
        permission_scope = TerminalPermissionScope(
            owner_uid=uid,
            owner_session_id=session_id,
            original_tool_call_id=original_tool_call_id,
            audit_record_id=audit_record_id,
            audit_execution_record_id=audit_execution_record_id,
            allowed_actions=allowed_actions,
        )
        return await terminal_session_crud.create_session(
            db,
            uid=permission_scope.owner_uid,
            session_id=permission_scope.owner_session_id,
            profile_id=profile_id,
            original_tool_call_id=permission_scope.original_tool_call_id,
            audit_record_id=permission_scope.audit_record_id,
            audit_execution_record_id=permission_scope.audit_execution_record_id,
            command=command,
            working_directory=working_directory,
            allowed_actions=permission_scope.allowed_actions,
            output_capacity_bytes=output_buffer.capacity_bytes,
            terminal_session_id=terminal_session_id,
            commit=commit,
        )

    async def get_or_create_session_for_execution(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        original_tool_call_id: str,
        audit_record_id: int | None,
        audit_execution_record_id: int | None,
        command: str,
        working_directory: str,
        allowed_actions: Iterable[TerminalAction | str],
    ) -> TerminalSession:
        self._validate_audit_binding(audit_record_id, audit_execution_record_id)
        expected_allowed_actions = frozenset(TerminalAction(action).value for action in allowed_actions)
        has_audit_binding = audit_record_id is not None and audit_execution_record_id is not None
        if has_audit_binding:
            existing = await terminal_session_crud.get_by_audit_execution_record_id(db, audit_execution_record_id)
        else:
            existing = await terminal_session_crud.get_by_unaudited_identity(
                db,
                uid=uid,
                session_id=session_id,
                original_tool_call_id=original_tool_call_id,
            )
        if existing is not None:
            self._validate_execution_session(
                existing,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                original_tool_call_id=original_tool_call_id,
                audit_record_id=audit_record_id,
                audit_execution_record_id=audit_execution_record_id,
                command=command,
                working_directory=working_directory,
                allowed_actions=expected_allowed_actions,
            )
            return existing

        try:
            return await self.create_session(
                db,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                original_tool_call_id=original_tool_call_id,
                audit_record_id=audit_record_id,
                audit_execution_record_id=audit_execution_record_id,
                command=command,
                working_directory=working_directory,
                allowed_actions=expected_allowed_actions,
            )
        except IntegrityError:
            await db.rollback()
            if has_audit_binding:
                existing = await terminal_session_crud.get_by_audit_execution_record_id(db, audit_execution_record_id)
            else:
                existing = await terminal_session_crud.get_by_unaudited_identity(
                    db,
                    uid=uid,
                    session_id=session_id,
                    original_tool_call_id=original_tool_call_id,
                )
            if existing is None:
                raise
            self._validate_execution_session(
                existing,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                original_tool_call_id=original_tool_call_id,
                audit_record_id=audit_record_id,
                audit_execution_record_id=audit_execution_record_id,
                command=command,
                working_directory=working_directory,
                allowed_actions=expected_allowed_actions,
            )
            return existing

    @staticmethod
    def _validate_execution_session(
        terminal_session: TerminalSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        original_tool_call_id: str,
        audit_record_id: int | None,
        audit_execution_record_id: int | None,
        command: str,
        working_directory: str,
        allowed_actions: frozenset[str],
    ) -> None:
        if (
            terminal_session.uid != uid
            or terminal_session.session_id != session_id
            or terminal_session.profile_id != profile_id
            or terminal_session.original_tool_call_id != original_tool_call_id
            or terminal_session.audit_record_id != audit_record_id
            or terminal_session.audit_execution_record_id != audit_execution_record_id
            or terminal_session.command != command
            or terminal_session.working_directory != working_directory
            or frozenset(action for action in terminal_session.allowed_actions if action in {available_action.value for available_action in ALL_TERMINAL_ACTIONS}) != allowed_actions
        ):
            raise ForbiddenException(
                ERR_TERMINAL_SESSION_ACCESS_DENIED,
                terminal_session_id=terminal_session.terminal_session_id,
            )

    @staticmethod
    def _validate_audit_binding(
        audit_record_id: int | None,
        audit_execution_record_id: int | None,
    ) -> None:
        if (audit_record_id is None) != (audit_execution_record_id is None):
            raise RuntimeError(t(ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED))

    async def get_owned_session(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        uid: str,
        session_id: str,
    ) -> TerminalSession:
        terminal_session = await terminal_session_crud.get(db, terminal_session_id)
        if terminal_session is None:
            raise ResourceNotFoundException(
                ERR_TERMINAL_SESSION_NOT_FOUND,
                terminal_session_id=terminal_session_id,
            )
        if terminal_session.uid != uid or terminal_session.session_id != session_id:
            raise ForbiddenException(
                ERR_TERMINAL_SESSION_ACCESS_DENIED,
                terminal_session_id=terminal_session_id,
            )
        return terminal_session

    async def get_snapshot(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        uid: str,
        session_id: str,
    ) -> TerminalSessionSnapshot:
        terminal_session = await self.get_owned_session(db, terminal_session_id, uid, session_id)
        if TerminalAction.STATUS.value not in terminal_session.allowed_actions:
            raise ForbiddenException(
                ERR_TERMINAL_ACTION_NOT_ALLOWED,
                terminal_session_id=terminal_session_id,
                action=TerminalAction.STATUS.value,
            )
        permission_scope = TerminalPermissionScope(
            owner_uid=terminal_session.uid,
            owner_session_id=terminal_session.session_id,
            original_tool_call_id=terminal_session.original_tool_call_id,
            audit_record_id=terminal_session.audit_record_id,
            audit_execution_record_id=terminal_session.audit_execution_record_id,
            allowed_actions=frozenset(action for action in ALL_TERMINAL_ACTIONS if action.value in terminal_session.allowed_actions),
        )
        output_buffer = TerminalOutputBufferState(
            capacity_bytes=terminal_session.output_capacity_bytes,
            oldest_offset=terminal_session.oldest_output_offset,
            next_offset=terminal_session.next_output_offset,
            oldest_sequence=terminal_session.oldest_output_sequence,
            next_sequence=terminal_session.next_output_sequence,
        )
        return TerminalSessionSnapshot(
            terminal_session_id=terminal_session.terminal_session_id,
            status=terminal_session.status,
            permission_scope=permission_scope,
            output_buffer=output_buffer,
            exit_code=terminal_session.exit_code,
            failure_reason=terminal_session.failure_reason,
        )

    async def enqueue_control(
        self,
        db: AsyncSession,
        uid: str,
        session_id: str,
        request: TerminalMutatingRequest,
    ) -> tuple[TerminalControlCommand, bool]:
        if not isinstance(request, _TERMINAL_MUTATING_REQUEST_TYPES):
            raise ParameterException(ERR_TERMINAL_MUTATING_REQUEST_REQUIRED)

        terminal_session = await self.get_owned_session(db, request.terminal_session_id, uid, session_id)
        if request.action.value not in terminal_session.allowed_actions:
            raise ForbiddenException(
                ERR_TERMINAL_ACTION_NOT_ALLOWED,
                terminal_session_id=request.terminal_session_id,
                action=request.action.value,
            )

        payload = request.model_dump(mode="json", exclude={"terminal_session_id", "request_id"})
        serialized_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload_hash = hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()
        existing = await terminal_control_command_crud.get_by_session_request(
            db,
            request.terminal_session_id,
            request.request_id,
        )
        if existing is not None:
            self._validate_request_identity(existing, request.action, payload_hash, request.terminal_session_id, request.request_id)
            await self._complete_unowned_terminal_command(db, terminal_session, existing)
            return existing, False

        command, created = await terminal_control_command_crud.enqueue(
            db,
            request.terminal_session_id,
            request.request_id,
            request.action,
            payload,
            payload_hash,
        )
        if not created:
            self._validate_request_identity(command, request.action, payload_hash, request.terminal_session_id, request.request_id)
        await self._complete_unowned_terminal_command(db, terminal_session, command)
        return command, created

    async def enqueue_read(
        self,
        db: AsyncSession,
        uid: str,
        session_id: str,
        request: TerminalReadRequest,
        request_id: str,
    ) -> tuple[TerminalControlCommand, bool]:
        terminal_session = await self.get_owned_session(db, request.terminal_session_id, uid, session_id)
        if request.action.value not in terminal_session.allowed_actions:
            raise ForbiddenException(
                ERR_TERMINAL_ACTION_NOT_ALLOWED,
                terminal_session_id=request.terminal_session_id,
                action=request.action.value,
            )

        payload = request.model_dump(mode="json", exclude={"terminal_session_id"})
        serialized_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload_hash = hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()
        existing = await terminal_control_command_crud.get_by_session_request(
            db,
            request.terminal_session_id,
            request_id,
        )
        if existing is not None:
            self._validate_request_identity(existing, request.action, payload_hash, request.terminal_session_id, request_id)
            await self._complete_unowned_terminal_command(db, terminal_session, existing)
            return existing, False

        command, created = await terminal_control_command_crud.enqueue(
            db,
            request.terminal_session_id,
            request_id,
            request.action,
            payload,
            payload_hash,
        )
        if not created:
            self._validate_request_identity(command, request.action, payload_hash, request.terminal_session_id, request_id)
        await self._complete_unowned_terminal_command(db, terminal_session, command)
        return command, created

    async def _complete_unowned_terminal_command(
        self,
        db: AsyncSession,
        terminal_session: TerminalSession,
        command: TerminalControlCommand,
    ) -> None:
        if terminal_session.status not in TERMINAL_SESSION_FINAL_STATUSES or terminal_session.locked_by is not None or command.id is None or command.status != TerminalControlCommandStatus.PENDING:
            return

        try:
            action = TerminalAction(command.action)
        except (TypeError, ValueError):
            await terminal_control_command_crud.complete_unowned_pending(
                db,
                terminal_session.terminal_session_id,
                command.id,
                error=t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=command.action),
            )
            return

        if action is TerminalAction.READ:
            try:
                result = _read_without_driver_result(
                    terminal_session.terminal_session_id,
                    _terminal_output_buffer(terminal_session),
                    command.payload,
                )
            except Exception as exc:
                await terminal_control_command_crud.complete_unowned_pending(
                    db,
                    terminal_session.terminal_session_id,
                    command.id,
                    error=str(exc),
                )
            else:
                await terminal_control_command_crud.complete_unowned_pending(
                    db,
                    terminal_session.terminal_session_id,
                    command.id,
                    result=result,
                )
            return

        if action is TerminalAction.CLOSE:
            result: dict[str, Any] = {"status": terminal_session.status.value}
            if terminal_session.exit_code is not None:
                result["exit_code"] = terminal_session.exit_code
            await terminal_control_command_crud.complete_unowned_pending(
                db,
                terminal_session.terminal_session_id,
                command.id,
                result=result,
            )
            return

        await terminal_control_command_crud.complete_unowned_pending(
            db,
            terminal_session.terminal_session_id,
            command.id,
            error=t(ERR_TERMINAL_PTY_CLOSED),
        )

    async def wait_for_command_result(
        self,
        db: AsyncSession,
        command_id: int,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            command = await terminal_control_command_crud.get(db, command_id)
            if command is not None:
                if command.status == TerminalControlCommandStatus.SUCCEEDED:
                    return command.result or {}
                if command.status == TerminalControlCommandStatus.FAILED:
                    raise RuntimeError(
                        t(
                            ERR_TERMINAL_COMMAND_FAILED,
                            error=command.error or "",
                        )
                    )

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    t(
                        ERR_TERMINAL_COMMAND_TIMEOUT,
                        command_id=command_id,
                        timeout=timeout_seconds,
                    )
                )
            await asyncio.sleep(min(poll_interval_seconds, remaining))

    @staticmethod
    def _validate_request_identity(
        command: TerminalControlCommand,
        action: TerminalAction,
        payload_hash: str,
        terminal_session_id: str,
        request_id: str,
    ) -> None:
        if command.action != action or command.payload_hash != payload_hash:
            raise ParameterException(
                ERR_TERMINAL_COMMAND_REQUEST_CONFLICT,
                terminal_session_id=terminal_session_id,
                action=action.value,
                request_id=request_id,
            )
