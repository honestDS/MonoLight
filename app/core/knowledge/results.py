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


__all__ = [
    "ManagedKnowledgeMutationResult",
    "ManagedKnowledgeMutationStatus",
]
