from __future__ import annotations

from typing import Annotated, Literal

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
    sources: tuple[KnowledgeOrganizationSourceReference, ...] = Field(min_length=1)
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


__all__ = [
    "KnowledgeOrganizationAnalysisResult",
    "KnowledgeOrganizationConflict",
    "KnowledgeOrganizationKeep",
    "KnowledgeOrganizationMerge",
    "KnowledgeOrganizationPlan",
    "KnowledgeOrganizationPlanItem",
    "KnowledgeOrganizationSourceReference",
    "KnowledgeOrganizationTarget",
    "KnowledgeOrganizationUpdate",
]
