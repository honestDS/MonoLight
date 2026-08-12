from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.core.constants import (
    ERR_MEMORY_ORGANIZATION_PLAN_INVALID,
    MEMORY_CONTENT_MAX_CHARS,
    MEMORY_KEY_MAX_CHARS,
    MEMORY_ORGANIZE_CONFLICT_REASON_MAX_CHARS,
)
from app.core.i18n import t
from app.models.memory import LongTermMemoryType


class _MemoryOrganizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryOrganizationSnapshotItem(_MemoryOrganizationModel):
    memory_id: StrictInt = Field(gt=0)
    expected_version: StrictInt = Field(ge=0)
    memory_key: StrictStr = Field(min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    memory_type: LongTermMemoryType
    content: StrictStr = Field(min_length=1, max_length=MEMORY_CONTENT_MAX_CHARS)
    content_token_count: StrictInt = Field(ge=0)
    pinned: StrictBool


class MemoryOrganizationSourceReference(_MemoryOrganizationModel):
    memory_id: StrictInt = Field(gt=0)
    expected_version: StrictInt = Field(ge=0)


class MemoryOrganizationTarget(_MemoryOrganizationModel):
    content: StrictStr = Field(min_length=1, max_length=MEMORY_CONTENT_MAX_CHARS)
    memory_key: StrictStr = Field(min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    memory_type: LongTermMemoryType


class MemoryOrganizationKeep(_MemoryOrganizationModel):
    action: Literal["keep"]
    source: MemoryOrganizationSourceReference


class MemoryOrganizationUpdate(_MemoryOrganizationModel):
    action: Literal["update"]
    source: MemoryOrganizationSourceReference
    target: MemoryOrganizationTarget


class MemoryOrganizationMerge(_MemoryOrganizationModel):
    action: Literal["merge"]
    sources: tuple[MemoryOrganizationSourceReference, ...] = Field(min_length=2)
    primary_memory_id: StrictInt = Field(gt=0)
    target: MemoryOrganizationTarget


class MemoryOrganizationConflict(_MemoryOrganizationModel):
    action: Literal["conflict"]
    sources: tuple[MemoryOrganizationSourceReference, ...] = Field(min_length=1)
    reason: StrictStr = Field(min_length=1, max_length=MEMORY_ORGANIZE_CONFLICT_REASON_MAX_CHARS)

    @field_validator("reason")
    @classmethod
    def _validate_reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(t(ERR_MEMORY_ORGANIZATION_PLAN_INVALID))
        return value


type MemoryOrganizationPlanItem = Annotated[
    MemoryOrganizationKeep | MemoryOrganizationUpdate | MemoryOrganizationMerge | MemoryOrganizationConflict,
    Field(discriminator="action"),
]


class MemoryOrganizationPlan(_MemoryOrganizationModel):
    items: tuple[MemoryOrganizationPlanItem, ...]


__all__ = [
    "MemoryOrganizationConflict",
    "MemoryOrganizationKeep",
    "MemoryOrganizationMerge",
    "MemoryOrganizationPlan",
    "MemoryOrganizationPlanItem",
    "MemoryOrganizationSnapshotItem",
    "MemoryOrganizationSourceReference",
    "MemoryOrganizationTarget",
    "MemoryOrganizationUpdate",
]
