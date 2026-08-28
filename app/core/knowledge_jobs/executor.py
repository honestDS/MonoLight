from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_CANCELLATION_REQUESTED,
    ERR_KNOWLEDGE_JOB_FIELD_INVALID,
    ERR_KNOWLEDGE_JOB_FIELD_REQUIRED,
    ERR_KNOWLEDGE_JOB_HANDLER_RESULT_INVALID,
    ERR_KNOWLEDGE_JOB_HANDLER_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_OPERATION_INVALID,
)
from app.core.crud.knowledge_job import knowledge_job_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.models.knowledge_base import KnowledgeJob, KnowledgeJobOperation, KnowledgeJobStatus
from app.providers.database import AsyncSessionLocal

type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class KnowledgeJobExecutionError(BaseBusinessException):
    def __init__(self, safe_message: str, result: dict[str, Any] | None = None) -> None:
        if not isinstance(safe_message, str):
            raise TypeError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field="safe_message"))
        if result is not None and not isinstance(result, dict):
            raise TypeError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field="result"))
        super().__init__(message=safe_message, default_message=safe_message)
        self.safe_message = self.render_message()
        self.result = result


class KnowledgeJobRetryableError(KnowledgeJobExecutionError):
    pass


class KnowledgeJobDeterministicError(KnowledgeJobExecutionError):
    pass


class KnowledgeJobCancelledError(KnowledgeJobExecutionError):
    pass


class KnowledgeJobLeaseLostError(KnowledgeJobRetryableError):
    pass


class KnowledgeJobOperationUnavailableError(KnowledgeJobDeterministicError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeJobExecutionContext:
    job: KnowledgeJob
    worker_id: str
    session_factory: SessionFactory = AsyncSessionLocal

    async def checkpoint(self) -> KnowledgeJob:
        if self.job.id is None or not self.job.uid or not self.worker_id:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        async with self.session_factory() as db:
            current = await knowledge_job_crud.get_active_claim(
                db,
                uid=self.job.uid,
                job_id=self.job.id,
                worker_id=self.worker_id,
            )
        if current is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        if current.cancel_requested_at is not None:
            raise KnowledgeJobCancelledError(t(ERR_KNOWLEDGE_JOB_CANCELLATION_REQUESTED))
        return current


@dataclass(frozen=True, slots=True)
class KnowledgeJobExecutionResult:
    result: dict[str, Any]
    finalized: bool = False


type Handler = Callable[
    [KnowledgeJobExecutionContext],
    Awaitable[dict[str, Any] | None | KnowledgeJobExecutionResult],
]


class KnowledgeJobExecutor:
    __slots__ = ("_handlers", "_session_factory")

    def __init__(
        self,
        handlers: Mapping[KnowledgeJobOperation | str, Handler] | None = None,
        session_factory: SessionFactory = AsyncSessionLocal,
    ) -> None:
        normalized: dict[KnowledgeJobOperation, Handler] = {}
        if handlers is not None:
            for raw_operation, handler in handlers.items():
                try:
                    operation = KnowledgeJobOperation(raw_operation)
                except (TypeError, ValueError) as exc:
                    raise ValueError(t(ERR_KNOWLEDGE_JOB_OPERATION_INVALID)) from exc
                if not callable(handler):
                    raise TypeError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field="handler"))
                normalized[operation] = handler
        self._handlers = MappingProxyType(normalized)
        self._session_factory = session_factory

    @property
    def enabled_operations(self) -> frozenset[KnowledgeJobOperation]:
        return frozenset(self._handlers)

    async def execute_claimed(self, job: KnowledgeJob, worker_id: str) -> KnowledgeJobExecutionResult:
        if job.id is None:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_FIELD_REQUIRED, field="id"))
        if not isinstance(job.uid, str) or not job.uid:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field="uid"))
        if not isinstance(worker_id, str) or not worker_id:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field="worker_id"))
        if job.locked_by != worker_id:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        try:
            operation = KnowledgeJobOperation(job.operation)
        except (TypeError, ValueError) as exc:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_OPERATION_INVALID)) from exc
        handler = self._handlers.get(operation)
        if handler is None:
            raise KnowledgeJobOperationUnavailableError(t(ERR_KNOWLEDGE_JOB_HANDLER_UNAVAILABLE))

        context = KnowledgeJobExecutionContext(job=job, worker_id=worker_id, session_factory=self._session_factory)
        await context.checkpoint()
        result = await handler(context)
        if isinstance(result, KnowledgeJobExecutionResult):
            if result.finalized:
                async with self._session_factory() as db:
                    finalized = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                if (
                    finalized is None
                    or finalized.status != KnowledgeJobStatus.SUCCEEDED
                    or finalized.active_change_key is not None
                    or finalized.locked_by is not None
                    or finalized.lock_until is not None
                ):
                    raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_HANDLER_RESULT_INVALID))
                return result
            await context.checkpoint()
            return result
        await context.checkpoint()
        if result is None:
            return KnowledgeJobExecutionResult(result={})
        if not isinstance(result, dict):
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_HANDLER_RESULT_INVALID))
        return KnowledgeJobExecutionResult(result=result)


__all__ = [
    "Handler",
    "KnowledgeJobCancelledError",
    "KnowledgeJobDeterministicError",
    "KnowledgeJobExecutionContext",
    "KnowledgeJobExecutionError",
    "KnowledgeJobExecutionResult",
    "KnowledgeJobExecutor",
    "KnowledgeJobLeaseLostError",
    "KnowledgeJobOperationUnavailableError",
    "KnowledgeJobRetryableError",
    "SessionFactory",
]
