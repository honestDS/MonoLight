from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryRecord


class MemoryMutationStatus(StrEnum):
    ACCEPTED = "accepted"
    EXISTING = "existing"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    RESUMED = "resumed"


@dataclass(frozen=True, slots=True)
class MemoryMutationResult:
    status: MemoryMutationStatus
    job: LongTermMemoryMutationJob | None = None
    record: LongTermMemoryRecord | None = None
    job_id: int | None = field(init=False)
    memory_id: int | None = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", self.job.id if self.job is not None else None)
        object.__setattr__(
            self,
            "memory_id",
            self.record.id if self.record is not None else self.job.memory_id if self.job is not None else None,
        )


class MemoryRecallStatus(StrEnum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    EMPTY = "empty"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class MemoryRecallItem:
    memory_id: int
    memory_key: str
    content: str
    memory_type: str
    importance: int
    scope: str | None
    version: int
    updated_at: datetime
    source: str
    dense_distance: float | None = None
    dense_rank: int | None = None
    sparse_score: float | None = None
    sparse_rank: int | None = None
    fusion_score: float | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class MemoryRecallResult:
    status: MemoryRecallStatus
    items: tuple[MemoryRecallItem, ...] = ()
    error_key: str | None = None


__all__ = [
    "MemoryMutationResult",
    "MemoryMutationStatus",
    "MemoryRecallItem",
    "MemoryRecallResult",
    "MemoryRecallStatus",
]
