from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models.knowledge_base import ManagedKnowledgeItem


class ManagedKnowledgeMutationStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    UNCHANGED = "unchanged"
    EXISTING_KEY = "existing_key"
    EXISTING_CONTENT = "existing_content"


@dataclass(frozen=True, slots=True)
class ManagedKnowledgeMutationResult:
    status: ManagedKnowledgeMutationStatus
    item: ManagedKnowledgeItem | None = None


class KnowledgeRecallSourceType(StrEnum):
    MANAGED_KNOWLEDGE = "managed_knowledge"
    USER_KNOWLEDGE = "user_knowledge"


@dataclass(frozen=True, slots=True)
class KnowledgeRecallItem:
    knowledge_base_id: int
    knowledge_base_name: str
    source_type: KnowledgeRecallSourceType
    source: str
    content: str
    truncated: bool = False
    llm_maintainable: bool = False
    document_id: int | None = None
    knowledge_id: int | None = None
    knowledge_key: str | None = None
    knowledge_expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeRecallResult:
    items: tuple[KnowledgeRecallItem, ...] = ()


__all__ = [
    "KnowledgeRecallItem",
    "KnowledgeRecallResult",
    "KnowledgeRecallSourceType",
    "ManagedKnowledgeMutationResult",
    "ManagedKnowledgeMutationStatus",
]
