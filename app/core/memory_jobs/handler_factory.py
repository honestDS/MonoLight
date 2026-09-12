from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.core.memory_jobs.executor import (
    Handler,
    MemoryJobExecutor,
    SessionFactory,
)
from app.core.memory_jobs.maintenance_handlers import create_memory_maintenance_job_handlers
from app.core.memory_jobs.organization_handler import create_memory_organization_job_handlers
from app.core.memory_jobs.vector_cleanup import (
    execute_vector_cleanup,
)
from app.models.memory import (
    LongTermMemoryMutationOperation,
)
from app.providers.database import AsyncSessionLocal

from .handler_cleanup import _handle_delete_cleanup
from .handler_execution import (
    _handle_create,
    _handle_create_with_eviction,
    _handle_organization_merge,
    _handle_update,
)

__all__ = [
    "create_memory_job_handlers",
    "create_default_memory_job_executor",
    "default_memory_job_executor",
]


def create_memory_job_handlers() -> Mapping[LongTermMemoryMutationOperation, Handler]:
    handlers = {
        **create_memory_maintenance_job_handlers(),
        **create_memory_organization_job_handlers(),
        LongTermMemoryMutationOperation.CREATE: _handle_create,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION: _handle_create_with_eviction,
        LongTermMemoryMutationOperation.UPDATE: _handle_update,
        LongTermMemoryMutationOperation.DELETE_CLEANUP: _handle_delete_cleanup,
        LongTermMemoryMutationOperation.ORGANIZE_MERGE: _handle_organization_merge,
        LongTermMemoryMutationOperation.VECTOR_CLEANUP: execute_vector_cleanup,
    }
    return MappingProxyType(handlers)


def create_default_memory_job_executor(session_factory: SessionFactory = AsyncSessionLocal) -> MemoryJobExecutor:
    return MemoryJobExecutor(create_memory_job_handlers(), session_factory=session_factory)


default_memory_job_executor = create_default_memory_job_executor()
