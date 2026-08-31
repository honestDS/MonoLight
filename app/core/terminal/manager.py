import asyncio
import hashlib
import json
import uuid
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
    ERR_TERMINAL_PTY_EXIT_CODE_MISSING,
    ERR_TERMINAL_PTY_NOT_STARTED,
    ERR_TERMINAL_PTY_STATE_INVALID,
    ERR_TERMINAL_READ_OFFSET_AHEAD,
    ERR_TERMINAL_SESSION_ACCESS_DENIED,
    ERR_TERMINAL_SESSION_DELETED,
    ERR_TERMINAL_SESSION_LEASE_LOST,
    ERR_TERMINAL_SESSION_NOT_FOUND,
    ERR_TERMINAL_WORKER_STOPPED,
    ERR_TOOL_SHELL_INTERACTIVE_AUDIT_BINDING_REQUIRED,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.terminal.session import (
    terminal_control_command_crud,
    terminal_session_crud,
)
from app.core.exceptions import ForbiddenException, ParameterException, ResourceNotFoundException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.terminal.process_config import build_interactive_shell_argv, build_subprocess_env
from app.core.terminal.pty_base import PtyDriver, PtyProcessConfig
from app.core.terminal.pty_factory import create_pty_driver
from app.core.terminal.recovery import capture_terminal_process_identity, cleanup_terminal_process_identity
from app.core.terminal.schemas import (
    ALL_TERMINAL_ACTIONS,
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalCloseRequest,
    TerminalOutputBufferState,
    TerminalOutputReadStatus,
    TerminalPermissionScope,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalResizeRequest,
    TerminalSessionSnapshot,
    TerminalSessionStatus,
    TerminalStatusRequest,
    TerminalWriteRequest,
)
from app.core.utils.background_task_result import serialize_execution_summary
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

TERMINAL_SESSION_LEASE_SECONDS = 60
TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS = 20
TERMINAL_SESSION_POLL_INTERVAL_SECONDS = 0.2
TERMINAL_PROCESS_IDENTITY_REFRESH_INTERVAL_SECONDS = 1.0
TERMINAL_FINAL_CLAIM_CLEANUP_INTERVAL_SECONDS = 1.0

type TerminalMutatingRequest = TerminalWriteRequest | TerminalResizeRequest | TerminalCloseRequest

_TERMINAL_MUTATING_REQUEST_TYPES = (
    TerminalWriteRequest,
    TerminalResizeRequest,
    TerminalCloseRequest,
)


async def _update_terminal_confirmation_status(db: AsyncSession, *, audit_record_id: int) -> None:
    from app.core.audit.confirmation import update_confirmation_message_status

    await update_confirmation_message_status(db, audit_record_id=audit_record_id)


async def finalize_terminal_session_audit(
    db: AsyncSession,
    terminal_session: TerminalSession,
    *,
    status: TerminalSessionStatus,
    exit_code: int | None,
    failure_reason: str | None,
) -> int | None:
    audit_record_id = terminal_session.audit_record_id
    audit_execution_record_id = terminal_session.audit_execution_record_id
    if (audit_record_id is None) != (audit_execution_record_id is None):
        logger.error(
            "Terminal session has a partial audit binding",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": audit_execution_record_id,
                "terminal_status": status.value,
            },
        )
        return None
    if audit_record_id is None or audit_execution_record_id is None:
        return None

    execution_record_id = audit_execution_record_id
    execution = await audit_crud.get_execution_record(db, execution_record_id)
    if execution is None:
        logger.warning(
            "Terminal session audit execution record not found",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "terminal_status": status.value,
            },
        )
        return None
    if execution.audit_record_id != audit_record_id:
        logger.error(
            "Terminal session audit execution binding mismatch",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "execution_audit_record_id": execution.audit_record_id,
            },
        )
        return None
    if execution.status != AuditExecutionStatus.RUNNING:
        logger.warning(
            "Terminal session audit execution is already finalized",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "execution_status": execution.status.value,
            },
        )
        return None

    audit_record = await audit_crud.get_record(db, audit_record_id)
    if audit_record is None:
        logger.warning(
            "Terminal session audit record not found",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
            },
        )
        return None
    if audit_record.status != AuditRecordStatus.EXECUTING or audit_record.execution_claim_token != execution.claim_token:
        logger.warning(
            "Terminal session audit round is already finalized",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "audit_status": audit_record.status.value,
            },
        )
        return None

    if status is TerminalSessionStatus.EXITED:
        execution_status = AuditExecutionStatus.SUCCEEDED if exit_code == 0 else AuditExecutionStatus.FAILED
    elif status is TerminalSessionStatus.FAILED:
        execution_status = AuditExecutionStatus.FAILED
    else:
        execution_status = AuditExecutionStatus.EXECUTION_UNKNOWN
    result_summary = serialize_execution_summary(
        {
            "terminal_session_id": terminal_session.terminal_session_id,
            "status": status.value,
            "exit_code": exit_code,
            "failure_reason": failure_reason,
        },
        max_chars=1000,
    )
    execution_finished = await audit_crud.finish_execution_attempt(
        db,
        execution_record_id=execution_record_id,
        status=execution_status,
        result_summary=result_summary,
        error=None if execution_status is AuditExecutionStatus.SUCCEEDED else result_summary,
        commit=False,
    )
    if not execution_finished:
        logger.warning(
            "Terminal session audit execution was not finalized",
            extra={
                "terminal_session_id": terminal_session.terminal_session_id,
                "audit_record_id": audit_record_id,
                "audit_execution_record_id": execution_record_id,
                "terminal_status": status.value,
            },
        )
        return None

    round_status = await audit_crud.finish_execution_round_if_complete(
        db,
        audit_record_id=audit_record_id,
        claim_token=execution.claim_token,
        commit=False,
    )
    return audit_record_id if round_status is not None else None


async def cleanup_terminal_sessions_by_chat_session(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
) -> int:
    terminal_sessions = await terminal_session_crud.list_by_chat_session(
        db,
        session_id=session_id,
        uid=uid,
    )
    failure_reason = t(ERR_TERMINAL_SESSION_DELETED)
    for terminal_session in terminal_sessions:
        try:
            cleanup_result = await cleanup_terminal_process_identity(terminal_session.process_identity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Terminal session process cleanup failed",
                extra={
                    "terminal_session_id": terminal_session.terminal_session_id,
                    "session_id": session_id,
                    "uid": uid,
                    "error": str(exc),
                },
            )
        else:
            if cleanup_result.errors:
                logger.error(
                    "Terminal session process cleanup reported errors",
                    extra={
                        "terminal_session_id": terminal_session.terminal_session_id,
                        "session_id": session_id,
                        "uid": uid,
                        "errors": cleanup_result.errors,
                    },
                )

        if terminal_session.status not in TERMINAL_SESSION_FINAL_STATUSES:
            await finalize_terminal_session_audit(
                db,
                terminal_session,
                status=TerminalSessionStatus.LOST,
                exit_code=None,
                failure_reason=failure_reason,
            )

    return await terminal_session_crud.delete_by_chat_session(
        db,
        session_id=session_id,
        uid=uid,
        commit=False,
    )


def _terminal_output_buffer(terminal_session: TerminalSession) -> TerminalOutputBufferState:
    return TerminalOutputBufferState(
        capacity_bytes=terminal_session.output_capacity_bytes,
        oldest_offset=terminal_session.oldest_output_offset,
        next_offset=terminal_session.next_output_offset,
        oldest_sequence=terminal_session.oldest_output_sequence,
        next_sequence=terminal_session.next_output_sequence,
    )


def _read_without_driver_result(
    terminal_session_id: str,
    output_buffer: TerminalOutputBufferState,
    payload: dict[str, Any],
) -> dict[str, Any]:
    offset = payload["offset"]
    latest_offset = output_buffer.next_offset
    if offset > latest_offset:
        raise ValueError(t(ERR_TERMINAL_READ_OFFSET_AHEAD))

    if offset == latest_offset and output_buffer.oldest_offset == latest_offset:
        result = TerminalReadResult(
            terminal_session_id=terminal_session_id,
            read_status=TerminalOutputReadStatus.EMPTY,
            requested_offset=offset,
            start_offset=latest_offset,
            next_offset=latest_offset,
            oldest_available_offset=latest_offset,
            latest_offset=latest_offset,
            sequence=0,
            output="",
            eof=True,
        )
    else:
        result = TerminalReadResult(
            terminal_session_id=terminal_session_id,
            read_status=TerminalOutputReadStatus.EXPIRED,
            requested_offset=offset,
            start_offset=latest_offset,
            next_offset=latest_offset,
            oldest_available_offset=latest_offset,
            latest_offset=latest_offset,
            sequence=max(0, output_buffer.next_sequence - 1),
            output="",
            eof=True,
        )
    return result.model_dump(mode="json")


class _TerminalLeaseLost(Exception):
    pass


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


class _TerminalSessionRuntime:
    def __init__(
        self,
        terminal_session: TerminalSession,
        worker_id: str,
    ) -> None:
        self.terminal_session = terminal_session
        self.terminal_session_id = terminal_session.terminal_session_id
        self.worker_id = worker_id
        self._stop_event = asyncio.Event()
        self._driver_start_finished = asyncio.Event()
        self._driver: PtyDriver | None = None
        self._wait_task: asyncio.Task[int] | None = None
        self._status = terminal_session.status
        self._exit_code = terminal_session.exit_code
        self._failure_reason = terminal_session.failure_reason
        self._process_identity = terminal_session.process_identity
        self._stop_reason: str | None = None
        self._lease_lost_confirmed = False
        self._last_process_identity_refresh_at = 0.0

    def request_stop(self, reason: str | None = None, *, lease_lost: bool = False) -> None:
        if reason is not None and (self._stop_reason is None or lease_lost):
            self._stop_reason = reason
        elif self._stop_reason is None:
            self._stop_reason = t(ERR_TERMINAL_WORKER_STOPPED)
        if lease_lost:
            self._lease_lost_confirmed = True
        self._stop_event.set()

    async def run(self) -> None:
        try:
            if await self._initialize():
                await self._serve()
        except _TerminalLeaseLost:
            self._lease_lost_confirmed = True
        except asyncio.CancelledError:
            self.request_stop(t(ERR_TERMINAL_WORKER_STOPPED))
            raise
        except Exception:
            self.request_stop(t(ERR_TERMINAL_WORKER_STOPPED))
            logger.exception(
                "Terminal session runtime failed",
                extra={"terminal_session_id": self.terminal_session_id},
            )
        finally:
            close_cancelled = False
            try:
                await self.force_close()
            except asyncio.CancelledError:
                close_cancelled = True
            except Exception:
                logger.exception(
                    "Terminal session driver force close failed",
                    extra={"terminal_session_id": self.terminal_session_id},
                )
            finally:
                try:
                    await self._await_wait_task()
                except asyncio.CancelledError:
                    close_cancelled = True
            await self._finalize_after_run()
            if close_cancelled:
                raise asyncio.CancelledError

    async def force_close(self) -> None:
        self._stop_event.set()
        await self._driver_start_finished.wait()
        driver = self._driver
        if driver is not None:
            await driver.close(force=True)

    async def _initialize(self) -> bool:
        try:
            config = PtyProcessConfig(
                argv=build_interactive_shell_argv(self.terminal_session.command),
                cwd=self.terminal_session.working_directory,
                env=build_subprocess_env(),
                output_capacity_bytes=self.terminal_session.output_capacity_bytes,
            )
            self._driver = create_pty_driver(config)
            await self._driver.start()
            process_identity = await capture_terminal_process_identity(self._driver.pid, self._process_identity) if self._driver.pid is not None else None
            if process_identity is not None:
                self._process_identity = process_identity
            self._last_process_identity_refresh_at = asyncio.get_running_loop().time()
            self._wait_task = asyncio.create_task(self._driver.wait())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Terminal session startup failed",
                extra={"terminal_session_id": self.terminal_session_id},
            )
            driver = self._driver
            if driver is not None:
                try:
                    await driver.close(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Terminal session startup cleanup failed",
                        extra={"terminal_session_id": self.terminal_session_id},
                    )
                finally:
                    self._driver = None
            try:
                await self._update_runtime_snapshot(
                    TerminalSessionStatus.FAILED,
                    output_buffer=self._get_output_buffer(),
                    failure_reason=str(exc),
                )
            except _TerminalLeaseLost:
                return False
            return True
        finally:
            self._driver_start_finished.set()

        if self._stop_event.is_set():
            return False
        try:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.RUNNING,
                output_buffer=self._get_output_buffer(),
            )
        except _TerminalLeaseLost:
            return False
        return True

    async def _serve(self) -> None:
        while not self._stop_event.is_set():
            driver = self._driver
            if driver is None:
                if self._status not in TERMINAL_SESSION_FINAL_STATUSES:
                    await self._update_runtime_snapshot(
                        self._status,
                        output_buffer=self._get_output_buffer(),
                        exit_code=self._exit_code,
                        failure_reason=self._failure_reason,
                    )
            else:
                snapshot = driver.resource_snapshot()
                wait_task_done = self._wait_task is not None and self._wait_task.done()
                if wait_task_done and self._status not in TERMINAL_SESSION_FINAL_STATUSES:
                    await self._complete_natural_exit()
                elif self._status not in TERMINAL_SESSION_FINAL_STATUSES:
                    await self._refresh_process_identity()
                    snapshot = driver.resource_snapshot()
                    await self._update_runtime_snapshot(
                        self._status,
                        output_buffer=snapshot.output_buffer,
                        exit_code=self._exit_code,
                        failure_reason=self._failure_reason,
                    )

            if self._stop_event.is_set():
                break
            command = await self._claim_next_command()
            if command is not None:
                await self._process_command(command)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=TERMINAL_SESSION_POLL_INTERVAL_SECONDS,
                )
            except TimeoutError:
                pass

    async def _complete_natural_exit(self) -> None:
        driver = self._driver
        wait_task = self._wait_task
        if driver is None or wait_task is None:
            return

        exit_code: int | None = None
        failure_reason: str | None = None
        try:
            exit_code = wait_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_reason = str(exc)

        try:
            await driver.close(force=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if failure_reason is None:
                failure_reason = str(exc)

        snapshot = driver.resource_snapshot()
        if failure_reason is not None:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=snapshot.output_buffer,
                failure_reason=failure_reason,
            )
            return

        if exit_code is None:
            exit_code = snapshot.exit_code
        if exit_code is None:
            failure_reason = t(ERR_TERMINAL_PTY_EXIT_CODE_MISSING)
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=snapshot.output_buffer,
                failure_reason=failure_reason,
            )
            return

        await self._update_runtime_snapshot(
            TerminalSessionStatus.EXITED,
            output_buffer=snapshot.output_buffer,
            exit_code=exit_code,
        )

    async def _claim_next_command(self) -> TerminalControlCommand | None:
        async with AsyncSessionLocal() as db:
            return await terminal_control_command_crud.claim_next(
                db,
                self.terminal_session_id,
                self.worker_id,
                TERMINAL_SESSION_LEASE_SECONDS,
            )

    async def _refresh_process_identity(self) -> None:
        driver = self._driver
        if driver is None or driver.pid is None:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now - self._last_process_identity_refresh_at < TERMINAL_PROCESS_IDENTITY_REFRESH_INTERVAL_SECONDS:
            return
        process_identity = await capture_terminal_process_identity(driver.pid, self._process_identity)
        self._last_process_identity_refresh_at = now
        if process_identity is not None:
            self._process_identity = process_identity

    async def _process_command(self, command: TerminalControlCommand) -> None:
        if command.id is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_STATE_INVALID))

        try:
            result = await self._execute_command(command)
        except asyncio.CancelledError:
            raise
        except _TerminalLeaseLost:
            raise
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                marked = await terminal_control_command_crud.mark_failed(
                    db,
                    command.id,
                    self.worker_id,
                    error=str(exc),
                )
            if not marked:
                self._lease_lost_confirmed = True
                logger.error(
                    "Terminal control command lease lost while marking failure",
                    extra={"terminal_session_id": self.terminal_session_id, "command_id": command.id},
                )
                raise _TerminalLeaseLost
            return

        async with AsyncSessionLocal() as db:
            marked = await terminal_control_command_crud.mark_succeeded(
                db,
                command.id,
                self.worker_id,
                result,
            )
        if not marked:
            self._lease_lost_confirmed = True
            logger.error(
                "Terminal control command lease lost while marking success",
                extra={"terminal_session_id": self.terminal_session_id, "command_id": command.id},
            )
            raise _TerminalLeaseLost

    async def _execute_command(self, command: TerminalControlCommand) -> dict[str, Any]:
        try:
            action = TerminalAction(command.action)
        except (TypeError, ValueError) as exc:
            raise ValueError(t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=command.action)) from exc

        driver = self._driver
        payload = command.payload
        if driver is None:
            if action is TerminalAction.READ:
                return self._read_without_driver(payload)
            if action is TerminalAction.CLOSE:
                return self._close_result()
            if action in {
                TerminalAction.WRITE,
                TerminalAction.RESIZE,
            }:
                error_key = ERR_TERMINAL_PTY_CLOSED if self._status in TERMINAL_SESSION_FINAL_STATUSES else ERR_TERMINAL_PTY_NOT_STARTED
                raise RuntimeError(t(error_key))
            raise RuntimeError(t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=command.action))

        if action is TerminalAction.READ:
            read = driver.read_output(payload["offset"], payload["max_bytes"])
            if read.data:
                read_status = TerminalOutputReadStatus.TRUNCATED if read.truncated else TerminalOutputReadStatus.OK
            else:
                read_status = TerminalOutputReadStatus.EMPTY
            return TerminalReadResult(
                terminal_session_id=self.terminal_session_id,
                read_status=read_status,
                requested_offset=read.requested_offset,
                start_offset=read.start_offset,
                next_offset=read.next_offset,
                oldest_available_offset=read.oldest_available_offset,
                latest_offset=read.latest_offset,
                sequence=read.sequence,
                output=read.data.decode("utf-8", errors="replace"),
                eof=read.eof or self._status in TERMINAL_SESSION_FINAL_STATUSES,
            ).model_dump(mode="json")

        if action is TerminalAction.WRITE:
            output_offset_before_write = driver.resource_snapshot().output_buffer.next_offset
            written = await driver.write(payload["data"])
            return {
                "bytes_written": written,
                "output_offset_before_write": output_offset_before_write,
            }

        if action is TerminalAction.RESIZE:
            columns = payload["columns"]
            rows = payload["rows"]
            await driver.resize(columns, rows)
            return {"columns": columns, "rows": rows}

        if action is TerminalAction.CLOSE:
            return await self._execute_close(bool(payload["force"]))

        raise RuntimeError(t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=command.action))

    def _read_without_driver(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _read_without_driver_result(self.terminal_session_id, self._get_output_buffer(), payload)

    async def _execute_close(self, force: bool) -> dict[str, Any]:
        driver = self._driver
        if driver is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_NOT_STARTED))

        was_final = self._status in TERMINAL_SESSION_FINAL_STATUSES
        if not was_final:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.CLOSING,
                output_buffer=self._get_output_buffer(),
            )

        try:
            await driver.close(force=force)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await driver.close(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Terminal session close cleanup failed",
                    extra={"terminal_session_id": self.terminal_session_id},
                )
            if not was_final:
                await self._update_runtime_snapshot(
                    TerminalSessionStatus.FAILED,
                    output_buffer=self._get_output_buffer(),
                    failure_reason=str(exc),
                )
            raise

        if was_final:
            return self._close_result()

        wait_task = self._wait_task
        exit_code: int | None = None
        try:
            if wait_task is not None:
                exit_code = await wait_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=self._get_output_buffer(),
                failure_reason=str(exc),
            )
            raise

        snapshot = driver.resource_snapshot()
        if exit_code is None:
            exit_code = snapshot.exit_code
        if exit_code is None:
            failure_reason = t(ERR_TERMINAL_PTY_EXIT_CODE_MISSING)
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=snapshot.output_buffer,
                failure_reason=failure_reason,
            )
            raise RuntimeError(failure_reason)

        await self._update_runtime_snapshot(
            TerminalSessionStatus.EXITED,
            output_buffer=snapshot.output_buffer,
            exit_code=exit_code,
        )
        return self._close_result()

    def _close_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self._status.value}
        if self._exit_code is not None:
            result["exit_code"] = self._exit_code
        return result

    async def _update_runtime_snapshot(
        self,
        status: TerminalSessionStatus,
        *,
        output_buffer: TerminalOutputBufferState,
        exit_code: int | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if self._status is status and status in TERMINAL_SESSION_FINAL_STATUSES:
            return

        audit_round_finished_id: int | None = None
        async with AsyncSessionLocal() as db:
            updated = await terminal_session_crud.update_runtime_snapshot(
                db,
                self.terminal_session_id,
                self.worker_id,
                status,
                output_buffer,
                exit_code=exit_code,
                failure_reason=failure_reason,
                process_identity=self._process_identity,
                commit=False,
            )
            if updated and status in TERMINAL_SESSION_FINAL_STATUSES:
                audit_round_finished_id = await finalize_terminal_session_audit(
                    db,
                    self.terminal_session,
                    status=status,
                    exit_code=exit_code,
                    failure_reason=failure_reason,
                )
            await db.commit()
        if not updated:
            self._lease_lost_confirmed = True
            logger.error(
                "Terminal session runtime lease lost",
                extra={"terminal_session_id": self.terminal_session_id},
            )
            raise _TerminalLeaseLost

        self._status = status
        if status is TerminalSessionStatus.EXITED:
            self._exit_code = exit_code
            self._failure_reason = None
        elif status is TerminalSessionStatus.FAILED:
            self._exit_code = None
            self._failure_reason = failure_reason
        else:
            self._exit_code = None
            self._failure_reason = None

        if audit_round_finished_id is not None:
            async with AsyncSessionLocal() as db:
                try:
                    await _update_terminal_confirmation_status(db, audit_record_id=audit_round_finished_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Terminal session confirmation status projection failed",
                        extra={
                            "terminal_session_id": self.terminal_session_id,
                            "audit_record_id": audit_round_finished_id,
                        },
                    )

    async def _finalize_after_run(self) -> None:
        if self._status not in TERMINAL_SESSION_FINAL_STATUSES and not self._lease_lost_confirmed:
            try:
                await self._update_runtime_snapshot(
                    TerminalSessionStatus.LOST,
                    output_buffer=self._get_output_buffer(),
                    failure_reason=self._stop_reason or t(ERR_TERMINAL_WORKER_STOPPED),
                )
            except _TerminalLeaseLost:
                self._lease_lost_confirmed = True
            except Exception:
                logger.exception(
                    "Terminal session lost-state persistence failed",
                    extra={"terminal_session_id": self.terminal_session_id},
                )
                return

        if self._lease_lost_confirmed:
            return

        command_failure_reason = self._stop_reason or (t(ERR_TERMINAL_PTY_CLOSED) if self._status in TERMINAL_SESSION_FINAL_STATUSES else t(ERR_TERMINAL_WORKER_STOPPED))
        async with AsyncSessionLocal() as db:
            await terminal_session_crud.fail_unfinished_commands(
                db,
                self.terminal_session_id,
                command_failure_reason,
                worker_id=self.worker_id,
                commit=False,
            )
            released = await terminal_session_crud.release_claim(
                db,
                self.terminal_session_id,
                self.worker_id,
                commit=False,
            )
            if released:
                await db.commit()
            else:
                await db.rollback()

    def _get_output_buffer(self) -> TerminalOutputBufferState:
        driver = self._driver
        if driver is None:
            return TerminalOutputBufferState(
                capacity_bytes=self.terminal_session.output_capacity_bytes,
                oldest_offset=self.terminal_session.oldest_output_offset,
                next_offset=self.terminal_session.next_output_offset,
                oldest_sequence=self.terminal_session.oldest_output_sequence,
                next_sequence=self.terminal_session.next_output_sequence,
            )
        return driver.resource_snapshot().output_buffer

    async def _await_wait_task(self) -> None:
        wait_task = self._wait_task
        if wait_task is None:
            return
        try:
            await wait_task
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


class TerminalWorkerCoordinator:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._worker_id = uuid.uuid4().hex
        self._owned_session_ids: set[str] = set()
        self._runtime_tasks: dict[str, asyncio.Task[None]] = {}
        self._runtimes: dict[str, _TerminalSessionRuntime] = {}
        self._last_lease_renewal_at = 0.0
        self._last_final_claim_cleanup_at = 0.0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is None:
            await self._shutdown_runtimes()
            return
        try:
            await task
        finally:
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        self._last_lease_renewal_at = loop.time()
        try:
            while not self._stop_event.is_set():
                claimed = None
                recoverable = None
                try:
                    now = loop.time()
                    if now - self._last_lease_renewal_at >= TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS:
                        for terminal_session_id in tuple(self._owned_session_ids):
                            runtime = self._runtimes.get(terminal_session_id)
                            try:
                                async with AsyncSessionLocal() as db:
                                    renewed = await terminal_session_crud.renew_lease(
                                        db,
                                        terminal_session_id,
                                        self._worker_id,
                                        TERMINAL_SESSION_LEASE_SECONDS,
                                    )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                if runtime is not None:
                                    runtime.request_stop(t(ERR_TERMINAL_SESSION_LEASE_LOST), lease_lost=True)
                                    try:
                                        await runtime.force_close()
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception:
                                        logger.exception(
                                            "Terminal session lease-loss cleanup failed",
                                            extra={"terminal_session_id": terminal_session_id},
                                        )
                                logger.exception(
                                    "Terminal session lease renewal failed",
                                    extra={"terminal_session_id": terminal_session_id},
                                )
                            else:
                                if not renewed:
                                    if runtime is None:
                                        self._owned_session_ids.discard(terminal_session_id)
                                    else:
                                        runtime.request_stop(t(ERR_TERMINAL_SESSION_LEASE_LOST), lease_lost=True)
                                        try:
                                            await runtime.force_close()
                                        except asyncio.CancelledError:
                                            raise
                                        except Exception:
                                            logger.exception(
                                                "Terminal session lease-loss cleanup failed",
                                                extra={"terminal_session_id": terminal_session_id},
                                            )
                        self._last_lease_renewal_at = now

                    if now - self._last_final_claim_cleanup_at >= TERMINAL_FINAL_CLAIM_CLEANUP_INTERVAL_SECONDS:
                        async with AsyncSessionLocal() as db:
                            await terminal_session_crud.cleanup_expired_final_claims(db)
                        self._last_final_claim_cleanup_at = now

                    async with AsyncSessionLocal() as db:
                        recoverable = await terminal_session_crud.claim_next_recoverable(
                            db,
                            self._worker_id,
                            TERMINAL_SESSION_LEASE_SECONDS,
                        )
                    if recoverable is not None:
                        await self._recover_session(recoverable)
                        claimed = recoverable
                    else:
                        async with AsyncSessionLocal() as db:
                            claimed = await terminal_session_crud.claim_next_starting(
                                db,
                                self._worker_id,
                                TERMINAL_SESSION_LEASE_SECONDS,
                            )
                    if claimed is not None and recoverable is None:
                        terminal_session_id = claimed.terminal_session_id
                        if terminal_session_id not in self._runtime_tasks:
                            runtime = _TerminalSessionRuntime(claimed, self._worker_id)
                            self._owned_session_ids.add(terminal_session_id)
                            self._runtimes[terminal_session_id] = runtime
                            self._runtime_tasks[terminal_session_id] = asyncio.create_task(self._run_session(runtime))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Terminal worker coordinator loop failed")

                if claimed is None:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=TERMINAL_SESSION_POLL_INTERVAL_SECONDS,
                        )
                    except TimeoutError:
                        pass
        finally:
            await self._shutdown_runtimes()

    async def _run_session(self, runtime: _TerminalSessionRuntime) -> None:
        try:
            await runtime.run()
        except asyncio.CancelledError:
            raise
        except _TerminalLeaseLost:
            pass
        except Exception:
            logger.exception(
                "Terminal session runtime failed",
                extra={"terminal_session_id": runtime.terminal_session_id},
            )
        finally:
            terminal_session_id = runtime.terminal_session_id
            self._owned_session_ids.discard(terminal_session_id)
            self._runtimes.pop(terminal_session_id, None)
            self._runtime_tasks.pop(terminal_session_id, None)

    async def _recover_session(self, terminal_session: TerminalSession) -> None:
        cleanup_result = await cleanup_terminal_process_identity(terminal_session.process_identity)
        if cleanup_result.errors:
            logger.error(
                "Terminal session orphan process cleanup failed",
                extra={
                    "terminal_session_id": terminal_session.terminal_session_id,
                    "errors": cleanup_result.errors,
                },
            )

        failure_reason = t(ERR_TERMINAL_SESSION_LEASE_LOST)
        audit_record_id: int | None = None
        async with AsyncSessionLocal() as db:
            updated = await terminal_session_crud.update_runtime_snapshot(
                db,
                terminal_session.terminal_session_id,
                self._worker_id,
                TerminalSessionStatus.LOST,
                _terminal_output_buffer(terminal_session),
                failure_reason=failure_reason,
                process_identity=terminal_session.process_identity,
                commit=False,
            )
            if not updated:
                await db.rollback()
                return
            audit_record_id = await finalize_terminal_session_audit(
                db,
                terminal_session,
                status=TerminalSessionStatus.LOST,
                exit_code=None,
                failure_reason=failure_reason,
            )
            await terminal_session_crud.fail_unfinished_commands(
                db,
                terminal_session.terminal_session_id,
                failure_reason,
                worker_id=self._worker_id,
                commit=False,
            )
            await terminal_session_crud.release_claim(
                db,
                terminal_session.terminal_session_id,
                self._worker_id,
                commit=False,
            )
            await db.commit()

        if audit_record_id is not None:
            async with AsyncSessionLocal() as db:
                try:
                    await _update_terminal_confirmation_status(db, audit_record_id=audit_record_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Terminal session confirmation status projection failed",
                        extra={
                            "terminal_session_id": terminal_session.terminal_session_id,
                            "audit_record_id": audit_record_id,
                        },
                    )

    async def _shutdown_runtimes(self) -> None:
        owned_session_ids = tuple(self._owned_session_ids)
        runtimes = tuple(self._runtimes.values())
        shutdown_cancelled: asyncio.CancelledError | None = None
        for runtime in runtimes:
            runtime.request_stop(t(ERR_TERMINAL_WORKER_STOPPED))
        if runtimes:
            close_results = await asyncio.gather(
                *(runtime.force_close() for runtime in runtimes),
                return_exceptions=True,
            )
            for runtime, result in zip(runtimes, close_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    shutdown_cancelled = result
                    continue
                if isinstance(result, BaseException):
                    logger.exception(
                        "Terminal session force close failed",
                        exc_info=(type(result), result, result.__traceback__),
                        extra={"terminal_session_id": runtime.terminal_session_id},
                    )

        tasks = tuple(self._runtime_tasks.values())
        if tasks:
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, task_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    shutdown_cancelled = result
                    continue
                if isinstance(result, BaseException):
                    logger.exception(
                        "Terminal session runtime task failed",
                        exc_info=(type(result), result, result.__traceback__),
                    )

        if owned_session_ids:
            async with AsyncSessionLocal() as db:
                for terminal_session_id in owned_session_ids:
                    await terminal_session_crud.release_claim(
                        db,
                        terminal_session_id,
                        self._worker_id,
                        commit=False,
                    )
                await db.commit()

        self._owned_session_ids.clear()
        self._runtimes.clear()
        self._runtime_tasks.clear()
        if shutdown_cancelled is not None:
            raise shutdown_cancelled


terminal_session_manager = TerminalSessionManager()
terminal_worker_coordinator = TerminalWorkerCoordinator()


__all__ = [
    "TERMINAL_FINAL_CLAIM_CLEANUP_INTERVAL_SECONDS",
    "TERMINAL_PROCESS_IDENTITY_REFRESH_INTERVAL_SECONDS",
    "TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS",
    "TERMINAL_SESSION_LEASE_SECONDS",
    "TERMINAL_SESSION_POLL_INTERVAL_SECONDS",
    "TerminalMutatingRequest",
    "TerminalSessionManager",
    "TerminalWorkerCoordinator",
    "cleanup_terminal_sessions_by_chat_session",
    "finalize_terminal_session_audit",
    "terminal_session_manager",
    "terminal_worker_coordinator",
]
