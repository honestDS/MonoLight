from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlmodel import select

from app.core.constants import (
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_OWNER_MISMATCH,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)

__all__ = [
    "MemoryJobRecoveryResult",
    "MemoryJobRecoveryTerminal",
    "MemoryJobCancelResult",
]


def _input_data(obj_in: Any) -> dict[str, Any]:
    if obj_in is None:
        return {}
    if isinstance(obj_in, dict):
        return dict(obj_in)
    return obj_in.model_dump(exclude_unset=True)


def _resolve_owner(owner: str | None, worker_id: str | None) -> str:
    if owner is None:
        owner = worker_id
    elif worker_id is not None and worker_id != owner:
        raise ValueError(t(ERR_MEMORY_JOB_OWNER_MISMATCH))
    if owner is None:
        raise TypeError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="owner"))
    if not owner:
        raise ValueError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="owner"))
    return owner


def _validate_duration(value: int, *, field: str, minimum: int = 0) -> int:
    if value < minimum:
        error = ERR_VALUE_MUST_BE_POSITIVE if minimum >= 1 else ERR_VALUE_MUST_BE_NON_NEGATIVE
        raise ValueError(t(error, field=field))
    return value


def _nullable_equal(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


def _claimable_statement(
    *,
    uid: str | None,
    operations: list[LongTermMemoryMutationOperation],
    now: datetime,
    limit: int,
):
    conditions: list[Any] = [
        LongTermMemoryMutationJob.status.in_(
            [
                LongTermMemoryMutationStatus.PENDING,
                LongTermMemoryMutationStatus.RETRY,
            ]
        ),
        LongTermMemoryMutationJob.available_at <= now,
        LongTermMemoryMutationJob.cancel_requested_at.is_(None),
        LongTermMemoryMutationJob.operation.in_(operations),
    ]
    if uid is not None:
        conditions.insert(0, LongTermMemoryMutationJob.uid == uid)
    return select(LongTermMemoryMutationJob).where(*conditions).order_by(LongTermMemoryMutationJob.available_at.asc(), LongTermMemoryMutationJob.id.asc()).limit(limit)


@dataclass(frozen=True, slots=True)
class MemoryJobRecoveryResult:
    retried: int = 0
    failed: int = 0
    cancelled: int = 0
    terminal_jobs: tuple["MemoryJobRecoveryTerminal", ...] = ()

    @property
    def recovered(self) -> int:
        return self.retried + self.failed + self.cancelled


@dataclass(frozen=True, slots=True)
class MemoryJobRecoveryTerminal:
    job: LongTermMemoryMutationJob
    status: LongTermMemoryMutationStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryJobCancelResult:
    job: LongTermMemoryMutationJob | None
    accepted: bool
    changed: bool
    error: str | None = None
