from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_FIELD_INVALID,
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_HANDLER_RESULT_INVALID,
    ERR_MEMORY_JOB_HANDLER_UNAVAILABLE,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_OPERATION_INVALID,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.i18n import t
from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryMutationOperation
from app.providers.database import AsyncSessionLocal

type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class MemoryJobExecutionError(RuntimeError):
    def __init__(self, safe_message: str, result: dict[str, Any] | None = None) -> None:
        if not isinstance(safe_message, str):
            raise TypeError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="safe_message"))
        if result is not None and not isinstance(result, dict):
            raise TypeError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="result"))
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.result = result


class MemoryJobRetryableError(MemoryJobExecutionError):
    pass


class MemoryJobDeterministicError(MemoryJobExecutionError):
    pass


class MemoryJobCancelledError(MemoryJobExecutionError):
    pass


class MemoryJobLeaseLostError(MemoryJobRetryableError):
    pass


class MemoryJobOperationUnavailableError(MemoryJobDeterministicError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryJobExecutionContext:
    job: LongTermMemoryMutationJob
    worker_id: str
    session_factory: SessionFactory = AsyncSessionLocal

    async def checkpoint(self) -> LongTermMemoryMutationJob:
        if self.job.id is None or not self.job.uid or not self.worker_id:
            raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))

        async with self.session_factory() as db:
            current_job = await memory_job_crud.get_active_claim(
                db,
                uid=self.job.uid,
                job_id=self.job.id,
                worker_id=self.worker_id,
            )

        if current_job is None:
            raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))

        try:
            operation = LongTermMemoryMutationOperation(current_job.operation)
        except (TypeError, ValueError) as exc:
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_OPERATION_INVALID)) from exc
        if current_job.cancel_requested_at is not None and operation != LongTermMemoryMutationOperation.DELETE_CLEANUP:
            raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
        return current_job


type Handler = Callable[[MemoryJobExecutionContext], Awaitable[dict[str, Any] | None]]


class MemoryJobExecutor:
    __slots__ = ("_handlers", "_session_factory")

    def __init__(
        self,
        handlers: Mapping[LongTermMemoryMutationOperation | str, Handler] | None = None,
        session_factory: SessionFactory = AsyncSessionLocal,
    ) -> None:
        normalized: dict[LongTermMemoryMutationOperation, Handler] = {}
        if handlers is not None:
            for raw_operation, handler in handlers.items():
                try:
                    operation = LongTermMemoryMutationOperation(raw_operation)
                except (TypeError, ValueError) as exc:
                    raise ValueError(t(ERR_MEMORY_JOB_OPERATION_INVALID)) from exc
                if not callable(handler):
                    raise TypeError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="handler"))
                normalized[operation] = handler
        self._handlers = MappingProxyType(normalized)
        self._session_factory = session_factory

    @property
    def handlers(self) -> Mapping[LongTermMemoryMutationOperation, Handler]:
        return self._handlers

    @property
    def enabled_operations(self) -> frozenset[LongTermMemoryMutationOperation]:
        return frozenset(self._handlers)

    async def execute_claimed(self, job: LongTermMemoryMutationJob, worker_id: str) -> dict[str, Any]:
        if job.id is None:
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="id"))
        if not isinstance(job.uid, str):
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="uid"))
        if not job.uid:
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="uid"))
        if not isinstance(worker_id, str):
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="worker_id"))
        if not worker_id:
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="worker_id"))
        if job.locked_by != worker_id:
            raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))

        try:
            operation = LongTermMemoryMutationOperation(job.operation)
        except (TypeError, ValueError) as exc:
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_OPERATION_INVALID)) from exc

        handler = self._handlers.get(operation)
        if handler is None:
            raise MemoryJobOperationUnavailableError(t(ERR_MEMORY_JOB_HANDLER_UNAVAILABLE))

        context = MemoryJobExecutionContext(
            job=job,
            worker_id=worker_id,
            session_factory=self._session_factory,
        )
        await context.checkpoint()
        result = await handler(context)
        await context.checkpoint()
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise MemoryJobDeterministicError(t(ERR_MEMORY_JOB_HANDLER_RESULT_INVALID))
        return result


memory_job_executor = MemoryJobExecutor()


__all__ = [
    "Handler",
    "MemoryJobCancelledError",
    "MemoryJobDeterministicError",
    "MemoryJobExecutionContext",
    "MemoryJobExecutionError",
    "MemoryJobExecutor",
    "MemoryJobLeaseLostError",
    "MemoryJobOperationUnavailableError",
    "MemoryJobRetryableError",
    "SessionFactory",
    "memory_job_executor",
]
