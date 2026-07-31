from collections.abc import Iterable
from typing import Any

from sqlalchemy import exists, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalOutputBufferState,
    TerminalSessionStatus,
    generate_terminal_session_id,
    validate_terminal_status_transition,
)
from app.models.terminal_session import (
    TerminalControlCommand,
    TerminalControlCommandStatus,
    TerminalSession,
)
from app.providers.database.time import get_database_time, get_database_timestamp


class CRUDTerminalSession:
    async def get(self, db: AsyncSession, terminal_session_id: str) -> TerminalSession | None:
        result = await db.execute(select(TerminalSession).where(TerminalSession.terminal_session_id == terminal_session_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_audit_execution_record_id(
        self,
        db: AsyncSession,
        audit_execution_record_id: int,
    ) -> TerminalSession | None:
        result = await db.execute(select(TerminalSession).where(TerminalSession.audit_execution_record_id == audit_execution_record_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def list_active_audit_execution_record_ids(
        self,
        db: AsyncSession,
        audit_record_id: int,
    ) -> set[int]:
        result = await db.execute(
            select(TerminalSession.audit_execution_record_id).where(
                TerminalSession.audit_record_id == audit_record_id,
                TerminalSession.status.not_in(TERMINAL_SESSION_FINAL_STATUSES),
            )
        )
        return {execution_record_id for execution_record_id in result.scalars().all() if isinstance(execution_record_id, int) and not isinstance(execution_record_id, bool) and execution_record_id > 0}

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
        normalized_allowed_actions = sorted({TerminalAction(action).value for action in allowed_actions})
        terminal_session = TerminalSession(
            terminal_session_id=terminal_session_id if terminal_session_id is not None else generate_terminal_session_id(),
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            original_tool_call_id=original_tool_call_id,
            audit_record_id=audit_record_id,
            audit_execution_record_id=audit_execution_record_id,
            command=command,
            working_directory=working_directory,
            allowed_actions=normalized_allowed_actions,
            output_capacity_bytes=output_capacity_bytes,
        )
        db.add(terminal_session)
        if commit:
            await db.commit()
        else:
            await db.flush()
        await db.refresh(terminal_session)
        return terminal_session

    async def try_claim_starting(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> TerminalSession | None:
        now = await get_database_timestamp(db)
        updated_at = await get_database_time(db)
        claimable = or_(
            TerminalSession.locked_by.is_(None),
            TerminalSession.locked_by == worker_id,
            TerminalSession.lock_until < now,
        )
        result = await db.execute(
            update(TerminalSession)
            .where(
                TerminalSession.terminal_session_id == terminal_session_id,
                TerminalSession.status == TerminalSessionStatus.STARTING,
                claimable,
            )
            .values(
                locked_by=worker_id,
                lock_until=now + lease_seconds,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if result.rowcount != 1:
            return None
        return await self.get(db, terminal_session_id)

    async def claim_next_starting(
        self,
        db: AsyncSession,
        worker_id: str,
        lease_seconds: int,
    ) -> TerminalSession | None:
        now = await get_database_timestamp(db)
        updated_at = await get_database_time(db)
        candidate = (
            select(TerminalSession.terminal_session_id)
            .where(
                TerminalSession.status == TerminalSessionStatus.STARTING,
                or_(
                    TerminalSession.locked_by.is_(None),
                    TerminalSession.lock_until < now,
                ),
            )
            .order_by(TerminalSession.created_at.asc(), TerminalSession.terminal_session_id.asc())
            .limit(1)
            .scalar_subquery()
        )
        result = await db.execute(
            update(TerminalSession)
            .where(
                TerminalSession.terminal_session_id == candidate,
                TerminalSession.status == TerminalSessionStatus.STARTING,
                or_(
                    TerminalSession.locked_by.is_(None),
                    TerminalSession.lock_until < now,
                ),
            )
            .values(
                locked_by=worker_id,
                lock_until=now + lease_seconds,
                updated_at=updated_at,
            )
            .returning(TerminalSession)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        claimed = result.scalars().first()
        await db.commit()
        return claimed

    async def renew_lease(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        now = await get_database_timestamp(db)
        updated_at = await get_database_time(db)
        result = await db.execute(
            update(TerminalSession)
            .where(
                TerminalSession.terminal_session_id == terminal_session_id,
                TerminalSession.locked_by == worker_id,
                TerminalSession.lock_until >= now,
            )
            .values(
                lock_until=now + lease_seconds,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def update_runtime_snapshot(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        worker_id: str,
        status: TerminalSessionStatus,
        output_buffer: TerminalOutputBufferState,
        exit_code: int | None = None,
        failure_reason: str | None = None,
        *,
        commit: bool = True,
    ) -> bool:
        terminal_session = await self.get(db, terminal_session_id)
        if terminal_session is None:
            return False

        target_status = validate_terminal_status_transition(terminal_session.status, status)
        now = await get_database_timestamp(db)
        database_time = await get_database_time(db)
        values = {
            "status": target_status,
            "oldest_output_offset": output_buffer.oldest_offset,
            "next_output_offset": output_buffer.next_offset,
            "oldest_output_sequence": output_buffer.oldest_sequence,
            "next_output_sequence": output_buffer.next_sequence,
            "exit_code": exit_code,
            "failure_reason": failure_reason,
            "updated_at": database_time,
        }
        if target_status is TerminalSessionStatus.RUNNING and terminal_session.started_at is None:
            values["started_at"] = database_time
        if target_status in TERMINAL_SESSION_FINAL_STATUSES:
            values["finished_at"] = database_time

        result = await db.execute(
            update(TerminalSession)
            .where(
                TerminalSession.terminal_session_id == terminal_session_id,
                TerminalSession.status == terminal_session.status,
                TerminalSession.locked_by == worker_id,
                TerminalSession.lock_until >= now,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return result.rowcount == 1

    async def release_starting_claim(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        worker_id: str,
    ) -> bool:
        updated_at = await get_database_time(db)
        result = await db.execute(
            update(TerminalSession)
            .where(
                TerminalSession.terminal_session_id == terminal_session_id,
                TerminalSession.status == TerminalSessionStatus.STARTING,
                TerminalSession.locked_by == worker_id,
            )
            .values(
                locked_by=None,
                lock_until=None,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


class CRUDTerminalControlCommand:
    @staticmethod
    def _session_lease_available(worker_id: str, now: Any):
        return exists(
            select(TerminalSession.terminal_session_id).where(
                TerminalSession.terminal_session_id == TerminalControlCommand.terminal_session_id,
                TerminalSession.locked_by == worker_id,
                TerminalSession.lock_until >= now,
            )
        )

    async def get(self, db: AsyncSession, command_id: int) -> TerminalControlCommand | None:
        result = await db.execute(select(TerminalControlCommand).where(TerminalControlCommand.id == command_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_session_request(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        request_id: str,
    ) -> TerminalControlCommand | None:
        result = await db.execute(
            select(TerminalControlCommand)
            .where(
                TerminalControlCommand.terminal_session_id == terminal_session_id,
                TerminalControlCommand.request_id == request_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def enqueue(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        request_id: str,
        action: TerminalAction,
        payload: dict[str, Any],
        payload_hash: str,
    ) -> tuple[TerminalControlCommand, bool]:
        existing = await self.get_by_session_request(db, terminal_session_id, request_id)
        if existing is not None:
            return existing, False

        command = TerminalControlCommand(
            terminal_session_id=terminal_session_id,
            request_id=request_id,
            action=action,
            payload=payload,
            payload_hash=payload_hash,
            status=TerminalControlCommandStatus.PENDING,
        )
        db.add(command)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_session_request(db, terminal_session_id, request_id)
            if existing is None:
                raise
            return existing, False
        await db.refresh(command)
        return command, True

    async def list_by_session(
        self,
        db: AsyncSession,
        terminal_session_id: str,
    ) -> list[TerminalControlCommand]:
        result = await db.execute(select(TerminalControlCommand).where(TerminalControlCommand.terminal_session_id == terminal_session_id).order_by(TerminalControlCommand.id.asc()))
        return list(result.scalars().all())

    async def claim_next(
        self,
        db: AsyncSession,
        terminal_session_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> TerminalControlCommand | None:
        now = await get_database_timestamp(db)
        updated_at = await get_database_time(db)
        session_available = exists(
            select(TerminalSession.terminal_session_id).where(
                TerminalSession.terminal_session_id == TerminalControlCommand.terminal_session_id,
                TerminalSession.locked_by == worker_id,
                TerminalSession.lock_until >= now,
            )
        )
        candidate = (
            select(TerminalControlCommand.id)
            .where(
                TerminalControlCommand.terminal_session_id == terminal_session_id,
                TerminalControlCommand.status == TerminalControlCommandStatus.PENDING,
                session_available,
            )
            .order_by(TerminalControlCommand.id.asc())
            .limit(1)
            .scalar_subquery()
        )
        result = await db.execute(
            update(TerminalControlCommand)
            .where(
                TerminalControlCommand.id == candidate,
                TerminalControlCommand.status == TerminalControlCommandStatus.PENDING,
                session_available,
            )
            .values(
                status=TerminalControlCommandStatus.PROCESSING,
                locked_by=worker_id,
                lock_until=now + lease_seconds,
                updated_at=updated_at,
            )
            .returning(TerminalControlCommand)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        claimed = result.scalars().first()
        await db.commit()
        return claimed

    async def mark_succeeded(
        self,
        db: AsyncSession,
        command_id: int,
        worker_id: str,
        result: dict[str, Any],
    ) -> bool:
        now = await get_database_timestamp(db)
        finished_at = await get_database_time(db)
        session_lease_available = self._session_lease_available(worker_id, now)
        update_result = await db.execute(
            update(TerminalControlCommand)
            .where(
                TerminalControlCommand.id == command_id,
                TerminalControlCommand.status == TerminalControlCommandStatus.PROCESSING,
                TerminalControlCommand.locked_by == worker_id,
                TerminalControlCommand.lock_until >= now,
                session_lease_available,
            )
            .values(
                status=TerminalControlCommandStatus.SUCCEEDED,
                result=result,
                error=None,
                finished_at=finished_at,
                locked_by=None,
                lock_until=None,
                updated_at=finished_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return update_result.rowcount == 1

    async def mark_failed(
        self,
        db: AsyncSession,
        command_id: int,
        worker_id: str,
        error: str,
    ) -> bool:
        now = await get_database_timestamp(db)
        finished_at = await get_database_time(db)
        session_lease_available = self._session_lease_available(worker_id, now)
        update_result = await db.execute(
            update(TerminalControlCommand)
            .where(
                TerminalControlCommand.id == command_id,
                TerminalControlCommand.status == TerminalControlCommandStatus.PROCESSING,
                TerminalControlCommand.locked_by == worker_id,
                TerminalControlCommand.lock_until >= now,
                session_lease_available,
            )
            .values(
                status=TerminalControlCommandStatus.FAILED,
                result=None,
                error=error,
                finished_at=finished_at,
                locked_by=None,
                lock_until=None,
                updated_at=finished_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return update_result.rowcount == 1


terminal_session_crud = CRUDTerminalSession()
terminal_control_command_crud = CRUDTerminalControlCommand()


__all__ = [
    "CRUDTerminalControlCommand",
    "CRUDTerminalSession",
    "terminal_control_command_crud",
    "terminal_session_crud",
]
