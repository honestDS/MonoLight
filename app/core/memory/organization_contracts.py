from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.core.constants import (
    ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
    MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
)
from app.core.memory.errors import MemoryValidationError
from app.core.memory.organization_types import (
    MemoryOrganizationSnapshotItem,
)
from app.models.memory import (
    LongTermMemoryType,
)
from app.models.message import InternalMessage

__all__ = [
    "MemoryOrganizationSnapshot",
    "MemoryOrganizationModelConfig",
    "MemoryOrganizationPlanCheckpoint",
    "MemoryOrganizationExecutionPayload",
    "MemoryOrganizationExecutionBudget",
    "MemoryOrganizationExecutionRequest",
    "MemoryOrganizationContextExceededError",
    "MemoryOrganizationPlanCounts",
    "MemoryOrganizationValidatedSource",
    "MemoryOrganizationValidatedTarget",
    "MemoryOrganizationValidatedItem",
    "MemoryOrganizationValidatedPlan",
]


@dataclass(frozen=True, slots=True)
class MemoryOrganizationSnapshot:
    digest: str
    count: int
    active_embedding_revision: int
    index_revision: int
    policy_version: int
    items: tuple[MemoryOrganizationSnapshotItem, ...]

    def to_job_snapshot(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "count": self.count,
            "active_embedding_revision": self.active_embedding_revision,
            "index_revision": self.index_revision,
            "policy_version": self.policy_version,
            "items": [item.model_dump(mode="json") for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class MemoryOrganizationModelConfig:
    """Immutable organization model settings detached from database entities."""

    channel_id: int
    channel_name: str
    model_id: str
    usage: str
    protocol: str
    context_window_k: int
    context_window_tokens: int
    max_tokens: int
    snapshot_count: int
    required_output_tokens: int
    policy_version: int
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    http_proxy: str | None = field(default=None, repr=False)
    custom_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    temperature: float = 0.7
    top_p: float | None = None
    reasoning_effort: str | None = None
    timeout: float = MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "custom_headers", MappingProxyType(dict(self.custom_headers)))

    def to_job_snapshot(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "model_id": self.model_id,
            "usage": self.usage,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "http_proxy": self.http_proxy,
            "custom_headers": dict(self.custom_headers),
            "temperature": self.temperature,
            "top_p": self.top_p,
            "reasoning_effort": self.reasoning_effort,
            "timeout": self.timeout,
            "context_window_k": self.context_window_k,
            "context_window_tokens": self.context_window_tokens,
            "max_tokens": self.max_tokens,
            "snapshot_count": self.snapshot_count,
            "required_output_tokens": self.required_output_tokens,
            "policy_version": self.policy_version,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "model_id": self.model_id,
            "usage": self.usage,
            "protocol": self.protocol,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "reasoning_effort": self.reasoning_effort,
            "timeout": self.timeout,
            "context_window_k": self.context_window_k,
            "context_window_tokens": self.context_window_tokens,
            "max_tokens": self.max_tokens,
            "snapshot_count": self.snapshot_count,
            "required_output_tokens": self.required_output_tokens,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class MemoryOrganizationPlanCheckpoint:
    model_output: str
    usage: dict[str, Any]
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class MemoryOrganizationExecutionPayload:
    trigger: str
    snapshot: MemoryOrganizationSnapshot
    organization_model: MemoryOrganizationModelConfig
    plan_checkpoint: MemoryOrganizationPlanCheckpoint | None = None


@dataclass(frozen=True, slots=True)
class MemoryOrganizationExecutionBudget:
    required_input_tokens: int
    available_input_tokens: int
    context_window_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    system_tokens: int
    non_system_tokens: int
    message_tokens: int
    tools_tokens: int

    @property
    def exceeds_hard_window(self) -> bool:
        return self.required_input_tokens > self.available_input_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "required_input_tokens": self.required_input_tokens,
            "available_input_tokens": self.available_input_tokens,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "system_tokens": self.system_tokens,
            "non_system_tokens": self.non_system_tokens,
            "message_tokens": self.message_tokens,
            "tools_tokens": self.tools_tokens,
        }


@dataclass(frozen=True, slots=True)
class MemoryOrganizationExecutionRequest:
    trigger: str
    snapshot: MemoryOrganizationSnapshot
    organization_model: MemoryOrganizationModelConfig
    messages: tuple[InternalMessage, ...]
    budget: MemoryOrganizationExecutionBudget
    plan_checkpoint: MemoryOrganizationPlanCheckpoint | None = None


class MemoryOrganizationContextExceededError(MemoryValidationError):
    def __init__(self, budget: MemoryOrganizationExecutionBudget) -> None:
        super().__init__(
            message=ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
            params={
                "required_tokens": budget.required_input_tokens,
                "available_tokens": budget.available_input_tokens,
            },
            data={
                "status": "organization_context_exceeded",
                "required_tokens": budget.required_input_tokens,
                "available_tokens": budget.available_input_tokens,
                "budget": budget.to_dict(),
            },
        )
        self.budget = budget


@dataclass(frozen=True, slots=True)
class MemoryOrganizationPlanCounts:
    keep_count: int = 0
    update_count: int = 0
    merge_count: int = 0
    conflict_count: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "keep_count": self.keep_count,
            "update_count": self.update_count,
            "merge_count": self.merge_count,
            "conflict_count": self.conflict_count,
        }


@dataclass(frozen=True, slots=True)
class MemoryOrganizationValidatedSource:
    memory_id: int
    expected_version: int
    pinned: bool


@dataclass(frozen=True, slots=True)
class MemoryOrganizationValidatedTarget:
    content: str
    memory_key: str
    memory_type: LongTermMemoryType
    content_token_count: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class MemoryOrganizationValidatedItem:
    action: str
    sources: tuple[MemoryOrganizationValidatedSource, ...]
    target: MemoryOrganizationValidatedTarget | None = None
    primary_memory_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryOrganizationValidatedPlan:
    items: tuple[MemoryOrganizationValidatedItem, ...]
    final_record_count: int

    @property
    def keep_count(self) -> int:
        return sum(item.action == "keep" for item in self.items)

    @property
    def update_count(self) -> int:
        return sum(item.action == "update" for item in self.items)

    @property
    def merge_count(self) -> int:
        return sum(item.action == "merge" for item in self.items)

    @property
    def conflict_count(self) -> int:
        return sum(item.action == "conflict" for item in self.items)

    @property
    def counts(self) -> MemoryOrganizationPlanCounts:
        return MemoryOrganizationPlanCounts(
            keep_count=self.keep_count,
            update_count=self.update_count,
            merge_count=self.merge_count,
            conflict_count=self.conflict_count,
        )

    @property
    def plan_summary(self) -> dict[str, Any]:
        summary_items: list[dict[str, Any]] = []
        for item in self.items:
            summary: dict[str, Any] = {"action": item.action}
            source_values = [{"memory_id": source.memory_id, "expected_version": source.expected_version} for source in item.sources]
            if item.action in {"keep", "update"}:
                summary["source"] = source_values[0]
            else:
                summary["sources"] = source_values
            if item.primary_memory_id is not None:
                summary["primary_memory_id"] = item.primary_memory_id
            if item.target is not None:
                summary["target"] = {
                    "memory_key": item.target.memory_key,
                    "memory_type": item.target.memory_type.value,
                    "content_token_count": item.target.content_token_count,
                    "content_hash": item.target.content_hash,
                }
            if item.reason is not None:
                summary["reason"] = item.reason
            summary_items.append(summary)
        return {"items": summary_items, "final_record_count": self.final_record_count}
