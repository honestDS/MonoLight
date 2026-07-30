import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_TERMINAL_ACTION_NOT_ALLOWED,
    ERR_TERMINAL_COMMAND_REQUEST_CONFLICT,
    ERR_TERMINAL_MUTATING_REQUEST_REQUIRED,
    ERR_TERMINAL_SESSION_ACCESS_DENIED,
    ERR_TERMINAL_SESSION_NOT_FOUND,
)
from app.core.crud.terminal_session import (
    terminal_control_command_crud,
    terminal_session_crud,
)
from app.core.exceptions import ForbiddenException, ParameterException, ResourceNotFoundException
from app.core.log import get_logger
from app.core.terminal.schemas import (
    TerminalAction,
    TerminalCloseRequest,
    TerminalOutputBufferState,
    TerminalPermissionScope,
    TerminalResizeRequest,
    TerminalSessionSnapshot,
    TerminalSignalRequest,
    TerminalStatusRequest,
    TerminalWriteRequest,
)
from app.models.terminal_session import TerminalControlCommand, TerminalSession
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

TERMINAL_SESSION_LEASE_SECONDS = 60
TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS = 20
TERMINAL_SESSION_POLL_INTERVAL_SECONDS = 0.2

type TerminalMutatingRequest = TerminalWriteRequest | TerminalResizeRequest | TerminalSignalRequest | TerminalCloseRequest

_TERMINAL_MUTATING_REQUEST_TYPES = (
    TerminalWriteRequest,
    TerminalResizeRequest,
    TerminalSignalRequest,
    TerminalCloseRequest,
)


class TerminalSessionManager:
    async def create_session(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        original_tool_call_id: str,
        audit_record_id: int,
        audit_execution_record_id: int,
        command: str,
        working_directory: str,
        allowed_actions: Iterable[TerminalAction | str],
        output_capacity_bytes: int = 1_048_576,
        terminal_session_id: str | None = None,
        commit: bool = True,
    ) -> TerminalSession:
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
        permission_scope = TerminalPermissionScope(
            owner_uid=terminal_session.uid,
            owner_session_id=terminal_session.session_id,
            original_tool_call_id=terminal_session.original_tool_call_id,
            audit_record_id=terminal_session.audit_record_id,
            audit_execution_record_id=terminal_session.audit_execution_record_id,
            allowed_actions=terminal_session.allowed_actions,
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
        return command, created

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


class TerminalWorkerCoordinator:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._worker_id = uuid.uuid4().hex
        self._owned_session_ids: set[str] = set()
        self._last_lease_renewal_at = 0.0

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
                try:
                    now = loop.time()
                    if now - self._last_lease_renewal_at >= TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS:
                        for terminal_session_id in tuple(self._owned_session_ids):
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
                                self._owned_session_ids.discard(terminal_session_id)
                                logger.exception(
                                    "Terminal session lease renewal failed",
                                    extra={"terminal_session_id": terminal_session_id},
                                )
                            else:
                                if not renewed:
                                    self._owned_session_ids.discard(terminal_session_id)
                        self._last_lease_renewal_at = now

                    async with AsyncSessionLocal() as db:
                        claimed = await terminal_session_crud.claim_next_starting(
                            db,
                            self._worker_id,
                            TERMINAL_SESSION_LEASE_SECONDS,
                        )
                    if claimed is not None:
                        self._owned_session_ids.add(claimed.terminal_session_id)
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
            for terminal_session_id in tuple(self._owned_session_ids):
                try:
                    async with AsyncSessionLocal() as db:
                        released = await terminal_session_crud.release_starting_claim(
                            db,
                            terminal_session_id,
                            self._worker_id,
                        )
                    if not released:
                        logger.error(
                            "Terminal session starting claim release failed",
                            extra={"terminal_session_id": terminal_session_id},
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Terminal session starting claim release failed",
                        extra={"terminal_session_id": terminal_session_id},
                    )
            self._owned_session_ids.clear()


terminal_session_manager = TerminalSessionManager()
terminal_worker_coordinator = TerminalWorkerCoordinator()


__all__ = [
    "TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS",
    "TERMINAL_SESSION_LEASE_SECONDS",
    "TERMINAL_SESSION_POLL_INTERVAL_SECONDS",
    "TerminalMutatingRequest",
    "TerminalSessionManager",
    "TerminalWorkerCoordinator",
    "terminal_session_manager",
    "terminal_worker_coordinator",
]
