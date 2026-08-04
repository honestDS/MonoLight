from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.core.memory_jobs.executor import Handler
from app.core.memory_jobs.migration_handler import handle_embedding_migration
from app.core.memory_jobs.reindex_handler import handle_reindex
from app.models.memory import LongTermMemoryMutationOperation


def create_memory_maintenance_job_handlers() -> Mapping[LongTermMemoryMutationOperation, Handler]:
    return MappingProxyType(
        {
            LongTermMemoryMutationOperation.REINDEX: handle_reindex,
            LongTermMemoryMutationOperation.EMBEDDING_MIGRATION: handle_embedding_migration,
        }
    )


__all__ = ["create_memory_maintenance_job_handlers"]
