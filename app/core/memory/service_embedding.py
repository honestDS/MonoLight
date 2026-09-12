from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_MIGRATION_DELTA_CONFLICT,
    ERR_MEMORY_SNAPSHOT_UID_FORBIDDEN,
)
from app.core.crud.memory.store import (
    memory_embedding_delta_crud,
    memory_store_crud,
)
from app.core.memory.errors import MemoryConflictError, MemoryValidationError
from app.core.memory.normalization import (
    _normalize_enum,
    _require_non_negative,
    _require_positive,
    _validate_commit,
)
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryMigrationStatus,
    LongTermMemoryStore,
)

from .service_common import (
    _ACTIVE_MIGRATION_STATUSES,
)

__all__ = [
    "append_memory_embedding_delta",
]


async def append_memory_embedding_delta(
    db: AsyncSession,
    store: LongTermMemoryStore,
    action: LongTermMemoryEmbeddingDeltaAction | str,
    memory_id: int,
    memory_version: int,
    source_mutation_job_id: int | None,
    snapshot: dict[str, Any],
    commit: bool = False,
) -> LongTermMemoryEmbeddingDelta | None:
    _validate_commit(commit)
    _require_positive(memory_id, field="memory_id")
    _require_non_negative(memory_version, field="memory_version")
    if source_mutation_job_id is not None:
        _require_positive(source_mutation_job_id, field="source_mutation_job_id")
    normalized_action = _normalize_enum(action, LongTermMemoryEmbeddingDeltaAction, field="action")
    if not isinstance(snapshot, dict):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "snapshot"})
    if "uid" in snapshot:
        raise MemoryValidationError(ERR_MEMORY_SNAPSHOT_UID_FORBIDDEN)
    if store.migration_job_id is None:
        return None
    try:
        migration_status = LongTermMemoryMigrationStatus(store.migration_status)
    except (TypeError, ValueError):
        return None
    if migration_status not in _ACTIVE_MIGRATION_STATUSES:
        return None
    if isinstance(store.migration_delta_high_watermark, bool) or not isinstance(store.migration_delta_high_watermark, int) or store.migration_delta_high_watermark < 0:
        raise MemoryConflictError(ERR_MEMORY_MIGRATION_DELTA_CONFLICT)
    sequence = await memory_store_crud.reserve_migration_delta_sequence(
        db,
        uid=store.uid,
        migration_job_id=store.migration_job_id,
        expected_high_watermark=store.migration_delta_high_watermark,
        commit=False,
    )
    if sequence is None:
        raise MemoryConflictError(ERR_MEMORY_MIGRATION_DELTA_CONFLICT)
    store.migration_delta_high_watermark = sequence
    return await memory_embedding_delta_crud.create(
        db,
        uid=store.uid,
        migration_job_id=store.migration_job_id,
        sequence=sequence,
        memory_id=memory_id,
        memory_version=memory_version,
        action=normalized_action,
        source_mutation_job_id=source_mutation_job_id,
        snapshot=dict(snapshot),
        commit=commit,
    )
