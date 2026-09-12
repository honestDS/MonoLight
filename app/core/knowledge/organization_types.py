from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from app.core.i18n import t


class _KnowledgeOrganizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgeOrganizationSourceReference(_KnowledgeOrganizationModel):
    knowledge_id: StrictInt = Field(gt=0)
    expected_version: StrictInt = Field(gt=0)


class KnowledgeOrganizationTarget(_KnowledgeOrganizationModel):
    knowledge_key: StrictStr = Field(min_length=1, max_length=255)
    content: StrictStr = Field(min_length=1)


class KnowledgeOrganizationKeep(_KnowledgeOrganizationModel):
    action: Literal["keep"]
    source: KnowledgeOrganizationSourceReference
    summary: StrictStr = Field(min_length=1)


class KnowledgeOrganizationUpdate(_KnowledgeOrganizationModel):
    action: Literal["update"]
    source: KnowledgeOrganizationSourceReference
    target: KnowledgeOrganizationTarget
    summary: StrictStr = Field(min_length=1)


class KnowledgeOrganizationMerge(_KnowledgeOrganizationModel):
    action: Literal["merge"]
    sources: tuple[KnowledgeOrganizationSourceReference, ...] = Field(min_length=2)
    primary_knowledge_id: StrictInt = Field(gt=0)
    target: KnowledgeOrganizationTarget
    summary: StrictStr = Field(min_length=1)


class KnowledgeOrganizationConflict(_KnowledgeOrganizationModel):
    action: Literal["conflict"]
    sources: tuple[KnowledgeOrganizationSourceReference, ...] = Field(min_length=2)
    reason: StrictStr = Field(min_length=1, max_length=1000)
    summary: StrictStr = Field(min_length=1)

    @field_validator("reason", "summary")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(t("blank organization text"))
        return value


type KnowledgeOrganizationPlanItem = Annotated[
    KnowledgeOrganizationKeep | KnowledgeOrganizationUpdate | KnowledgeOrganizationMerge | KnowledgeOrganizationConflict,
    Field(discriminator="action"),
]


class KnowledgeOrganizationPlan(_KnowledgeOrganizationModel):
    items: tuple[KnowledgeOrganizationPlanItem, ...]


class KnowledgeOrganizationAnalysisResult(_KnowledgeOrganizationModel):
    summary: StrictStr = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def _summary_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(t("blank organization summary"))
        return value


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationScopeItem:
    sources: tuple[tuple[int, int], ...]
    knowledge_key: str
    content: str
    content_hash: str
    source_type: str
    source_reference: Mapping[str, Any] | None = None
    vector_item_ids: tuple[str, ...] = ()
    related_ids: frozenset[int] = frozenset()
    effective_item: KnowledgeOrganizationPlanItem | None = None

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError(t("organization scope item requires sources"))
        if len({knowledge_id for knowledge_id, _version in self.sources}) != len(self.sources):
            raise ValueError(t("organization scope item has duplicate sources"))
        if self.source_reference is not None:
            object.__setattr__(self, "source_reference", MappingProxyType(dict(self.source_reference)))
        object.__setattr__(self, "vector_item_ids", tuple(self.vector_item_ids))
        object.__setattr__(self, "related_ids", frozenset(self.related_ids))

    @property
    def source_ids(self) -> frozenset[int]:
        return frozenset(knowledge_id for knowledge_id, _version in self.sources)


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationFragmentInput:
    fragment_index: int
    scope: tuple[KnowledgeOrganizationScopeItem, ...]
    input_tokens: int


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationFragmentResult:
    fragment_index: int
    scope: tuple[KnowledgeOrganizationScopeItem, ...]
    plan: KnowledgeOrganizationPlan
    output_scope: tuple[KnowledgeOrganizationScopeItem, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationPipelineStats:
    max_active_tasks: int
    max_input_queue_size: int
    max_result_queue_size: int
    max_reorder_size: int


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationExecutionResult:
    plan: KnowledgeOrganizationPlan
    model_id: str | None
    stage_count: int
    snapshot_id: int | None


__all__ = [
    "KnowledgeOrganizationAnalysisResult",
    "KnowledgeOrganizationConflict",
    "KnowledgeOrganizationExecutionResult",
    "KnowledgeOrganizationFragmentInput",
    "KnowledgeOrganizationFragmentResult",
    "KnowledgeOrganizationKeep",
    "KnowledgeOrganizationMerge",
    "KnowledgeOrganizationPipelineStats",
    "KnowledgeOrganizationPlan",
    "KnowledgeOrganizationPlanItem",
    "KnowledgeOrganizationScopeItem",
    "KnowledgeOrganizationSourceReference",
    "KnowledgeOrganizationTarget",
    "KnowledgeOrganizationUpdate",
]
