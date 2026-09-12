from typing import Any

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import (
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryType,
)

__all__ = []

_MEMORY_RECORD_SORT_COLUMNS = {
    "updated_at": LongTermMemoryRecord.updated_at,
    "created_at": LongTermMemoryRecord.created_at,
    "version": LongTermMemoryRecord.version,
}


def _organization_record_conditions(uid_condition: Any) -> list[Any]:
    return [
        uid_condition,
        LongTermMemoryRecord.is_active.is_(True),
        LongTermMemoryRecord.deleted_at.is_(None),
        LongTermMemoryRecord.suppress_recall.is_(False),
        LongTermMemoryRecord.index_status == LongTermMemoryRecordIndexStatus.READY,
        LongTermMemoryRecord.indexed_version == LongTermMemoryRecord.version,
        LongTermMemoryRecord.vector_item_id.is_not(None),
        LongTermMemoryRecord.vector_item_id != "",
    ]


def _recallable_conditions(uid: str) -> list[Any]:
    return _organization_record_conditions(LongTermMemoryRecord.uid == uid)


def _eviction_candidate_conditions(
    *,
    uid: str,
    memory_id: int | None = None,
    version: int | None = None,
    vector_item_id: str | None = None,
    pending_job_id: int | None = None,
) -> list[Any]:
    conditions: list[Any] = [
        LongTermMemoryRecord.uid == uid,
        LongTermMemoryRecord.is_active.is_(True),
        LongTermMemoryRecord.deleted_at.is_(None),
        LongTermMemoryRecord.suppress_recall.is_(False),
        LongTermMemoryRecord.index_status == LongTermMemoryRecordIndexStatus.READY,
        LongTermMemoryRecord.indexed_version == LongTermMemoryRecord.version,
        LongTermMemoryRecord.vector_item_id.is_not(None),
        LongTermMemoryRecord.vector_item_id != "",
        LongTermMemoryRecord.pinned.is_(False),
        LongTermMemoryRecord.pending_mutation_job_id.is_(None) if pending_job_id is None else LongTermMemoryRecord.pending_mutation_job_id == pending_job_id,
    ]
    if memory_id is not None:
        conditions.append(LongTermMemoryRecord.id == memory_id)
    if version is not None:
        conditions.append(LongTermMemoryRecord.version == version)
    if vector_item_id is not None:
        conditions.append(LongTermMemoryRecord.vector_item_id == vector_item_id)
    return conditions


def _input_data(obj_in: Any) -> dict[str, Any]:
    if obj_in is None:
        return {}
    if isinstance(obj_in, dict):
        return dict(obj_in)
    return obj_in.model_dump(exclude_unset=True)


def _memory_record_conditions(
    *,
    uid: str,
    keyword: str | None = None,
    memory_type: LongTermMemoryType | str | None = None,
) -> list[Any]:
    conditions: list[Any] = [LongTermMemoryRecord.uid == uid]
    if keyword:
        keyword_pattern = f"%{keyword}%"
        conditions.append(
            or_(
                LongTermMemoryRecord.content.ilike(keyword_pattern),
                LongTermMemoryRecord.memory_key.ilike(keyword_pattern),
            )
        )
    if memory_type is not None:
        conditions.append(LongTermMemoryRecord.memory_type == memory_type)
    return conditions


def _memory_record_order_by(*, sort_by: str | None, sort_order: str):
    if sort_by is None:
        column = LongTermMemoryRecord.id
    else:
        column = _MEMORY_RECORD_SORT_COLUMNS.get(sort_by, LongTermMemoryRecord.updated_at)
    ascending = sort_order.lower() == "asc"
    return (column.asc(), LongTermMemoryRecord.id.asc()) if ascending else (column.desc(), LongTermMemoryRecord.id.desc())


async def _finish(db: AsyncSession, *, commit: bool) -> None:
    if commit:
        await db.commit()
    else:
        await db.flush()
