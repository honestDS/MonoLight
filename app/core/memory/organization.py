from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from numbers import Real
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
    ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
    ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED,
    ERR_MEMORY_ORGANIZATION_PLAN_INVALID,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_ORGANIZE_CONTEXT_SAFETY_MARGIN_TOKENS,
    MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
    MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS,
    MEMORY_ORGANIZE_POLICY_VERSION,
)
from app.core.crud.channel import channel_crud
from app.core.crud.memory import memory_record_crud, memory_store_crud
from app.core.exceptions import LLMException
from app.core.memory.errors import MemoryConflictError, MemoryContentTooLongError, MemoryValidationError
from app.core.memory.normalization import (
    _normalize_dedupe_key,
    _normalize_uid,
    _validate_commit,
    build_memory_content_hash,
    normalize_memory_content_for_publication,
    normalize_memory_key,
)
from app.core.memory.organization_types import (
    MemoryOrganizationConflict,
    MemoryOrganizationKeep,
    MemoryOrganizationMerge,
    MemoryOrganizationPlan,
    MemoryOrganizationPlanItem,
    MemoryOrganizationSnapshotItem,
    MemoryOrganizationSourceReference,
    MemoryOrganizationTarget,
    MemoryOrganizationUpdate,
)
from app.core.prompts import MEMORY_ORGANIZATION_SYSTEM_PROMPT
from app.core.utils.context_budget import measure_context_request_usage
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import normalize_model_custom_headers
from app.models.channel import (
    MODEL_PROTOCOLS_BY_USAGE,
    ChannelModelItem,
    ModelProtocol,
    ModelUsage,
    resolve_model_protocol,
)
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryRecord,
    LongTermMemoryStore,
    LongTermMemoryType,
)
from app.models.message import InternalMessage, InternalResponse, MessageRole
from app.providers.llm.client import LLMClient


class MemoryOrganizationPinPolicyStatus(StrEnum):
    MERGE = "merge"
    INVALID_PRIMARY = "invalid_primary"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class MemoryOrganizationPinPolicyResult:
    status: MemoryOrganizationPinPolicyStatus
    primary_memory_id: int | None
    pinned_memory_ids: tuple[int, ...]
    tombstone_memory_ids: tuple[int, ...]


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
            "timeout": self.timeout,
            "context_window_k": self.context_window_k,
            "context_window_tokens": self.context_window_tokens,
            "max_tokens": self.max_tokens,
            "snapshot_count": self.snapshot_count,
            "required_output_tokens": self.required_output_tokens,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class MemoryOrganizationExecutionPayload:
    trigger: str
    snapshot: MemoryOrganizationSnapshot
    organization_model: MemoryOrganizationModelConfig


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


def _empty_organization_plan_summary() -> dict[str, Any]:
    return {"items": [], "final_record_count": 0}


class MemoryOrganizationPlanInvalidError(MemoryValidationError):
    def __init__(
        self,
        validation_errors: Iterable[Mapping[str, Any]],
        *,
        action_counts: MemoryOrganizationPlanCounts | Mapping[str, int] | None = None,
        plan_summary: Mapping[str, Any] | None = None,
        validation_error_count: int | None = None,
        validation_errors_truncated: bool = False,
    ) -> None:
        if action_counts is None:
            counts = MemoryOrganizationPlanCounts()
        elif isinstance(action_counts, MemoryOrganizationPlanCounts):
            counts = action_counts
        else:
            counts = MemoryOrganizationPlanCounts(
                keep_count=int(action_counts.get("keep_count", 0)),
                update_count=int(action_counts.get("update_count", 0)),
                merge_count=int(action_counts.get("merge_count", 0)),
                conflict_count=int(action_counts.get("conflict_count", 0)),
            )
        safe_errors: list[dict[str, Any]] = []
        for error in validation_errors:
            location = error.get("location")
            safe_location = dict(location) if isinstance(location, Mapping) else {}
            safe_errors.append(
                {
                    "code": str(error.get("code", "organization_plan_invalid")),
                    "location": safe_location,
                }
            )
        total_errors = len(safe_errors) if validation_error_count is None else validation_error_count
        safe_summary = dict(plan_summary) if isinstance(plan_summary, Mapping) else _empty_organization_plan_summary()
        data = {
            "status": "organization_plan_invalid",
            **counts.to_dict(),
            "plan_summary": safe_summary,
            "validation_errors": safe_errors,
            "validation_error_count": total_errors,
            "validation_errors_truncated": validation_errors_truncated,
        }
        super().__init__(message=ERR_MEMORY_ORGANIZATION_PLAN_INVALID, data=data)
        self.validation_errors = tuple(safe_errors)
        self.validation_error_count = total_errors
        self.validation_errors_truncated = validation_errors_truncated
        self.action_counts = MappingProxyType(counts.to_dict())
        self.plan_summary = safe_summary


_PLAN_VALIDATION_ERROR_LIMIT = 64


class _OrganizationPlanValidationCollector:
    __slots__ = ("errors", "total")

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.total = 0

    def add(
        self,
        code: str,
        *,
        item_index: int | None = None,
        source_index: int | None = None,
        memory_id: int | None = None,
        field: str | None = None,
    ) -> None:
        self.total += 1
        if len(self.errors) >= _PLAN_VALIDATION_ERROR_LIMIT:
            return
        location: dict[str, Any] = {}
        if item_index is not None:
            location["item_index"] = item_index
        if source_index is not None:
            location["source_index"] = source_index
        if memory_id is not None:
            location["memory_id"] = memory_id
        if field is not None:
            location["field"] = field
        self.errors.append({"code": code, "location": location})


def _safe_schema_validation_errors(exc: ValidationError) -> tuple[list[dict[str, Any]], int, bool]:
    errors: list[dict[str, Any]] = []
    raw_errors = exc.errors()
    for error in raw_errors[:_PLAN_VALIDATION_ERROR_LIMIT]:
        raw_type = error.get("type")
        error_type = str(raw_type) if isinstance(raw_type, str) and raw_type else "invalid"
        raw_location = error.get("loc", ())
        location_path: list[str | int] = []
        if isinstance(raw_location, (list, tuple)):
            for part in raw_location:
                if isinstance(part, int) and not isinstance(part, bool):
                    location_path.append(part)
                elif isinstance(part, str):
                    location_path.append(part)
        errors.append({"code": f"schema_{error_type}", "location": {"path": location_path}})
    return errors, len(raw_errors), len(raw_errors) > len(errors)


def _organization_plan_counts(plan: MemoryOrganizationPlan) -> MemoryOrganizationPlanCounts:
    counts = {"keep": 0, "update": 0, "merge": 0, "conflict": 0}
    for item in plan.items:
        counts[item.action] += 1
    return MemoryOrganizationPlanCounts(
        keep_count=counts["keep"],
        update_count=counts["update"],
        merge_count=counts["merge"],
        conflict_count=counts["conflict"],
    )


def _normalize_organization_target(
    target: MemoryOrganizationTarget,
    *,
    collector: _OrganizationPlanValidationCollector,
    item_index: int,
) -> MemoryOrganizationValidatedTarget | None:
    content_result = None
    normalized_key = None
    normalized_type = None
    try:
        content_result = normalize_memory_content_for_publication(target.content)
    except MemoryContentTooLongError:
        collector.add("target_content_too_long", item_index=item_index, field="target.content")
    except Exception:
        collector.add("target_content_invalid", item_index=item_index, field="target.content")
    try:
        normalized_key = normalize_memory_key(target.memory_key)
    except Exception:
        collector.add("target_memory_key_invalid", item_index=item_index, field="target.memory_key")
    try:
        normalized_type = LongTermMemoryType(target.memory_type)
    except (TypeError, ValueError):
        collector.add("target_memory_type_invalid", item_index=item_index, field="target.memory_type")
    if content_result is None or normalized_key is None or normalized_type is None:
        return None
    return MemoryOrganizationValidatedTarget(
        content=content_result.content,
        memory_key=normalized_key,
        memory_type=normalized_type,
        content_token_count=content_result.content_token_count,
        content_hash=content_result.content_hash,
    )


def _snapshot_record_identity(item: MemoryOrganizationSnapshotItem) -> tuple[str, str]:
    return item.memory_key, build_memory_content_hash(item.content)


def validate_organization_model_output(
    model_output: Any,
    snapshot: MemoryOrganizationSnapshot,
) -> MemoryOrganizationValidatedPlan:
    collector = _OrganizationPlanValidationCollector()
    if not isinstance(model_output, str):
        collector.add("model_output_not_string", field="model_output")
        raise MemoryOrganizationPlanInvalidError(
            collector.errors,
            validation_error_count=collector.total,
        )
    if not model_output.strip():
        collector.add("model_output_empty", field="model_output")
        raise MemoryOrganizationPlanInvalidError(
            collector.errors,
            validation_error_count=collector.total,
        )
    schema_errors: tuple[list[dict[str, Any]], int, bool] | None = None
    try:
        plan = MemoryOrganizationPlan.model_validate_json(model_output)
    except ValidationError as exc:
        schema_errors = _safe_schema_validation_errors(exc)
    if schema_errors is not None:
        errors, total, truncated = schema_errors
        raise MemoryOrganizationPlanInvalidError(
            errors,
            validation_error_count=total,
            validation_errors_truncated=truncated,
        )

    counts = _organization_plan_counts(plan)
    snapshot_by_id: dict[int, MemoryOrganizationSnapshotItem] = {}
    for snapshot_item in snapshot.items:
        if snapshot_item.memory_id in snapshot_by_id:
            collector.add("snapshot_duplicate_memory_id", memory_id=snapshot_item.memory_id)
        else:
            snapshot_by_id[snapshot_item.memory_id] = snapshot_item

    source_occurrences: dict[int, int] = {}
    validated_items: list[MemoryOrganizationValidatedItem] = []
    summary_items: list[dict[str, Any]] = []
    final_records: list[tuple[int, str, str]] = []

    for item_index, plan_item in enumerate(plan.items):
        raw_sources = (plan_item.source,) if plan_item.action in {"keep", "update"} else plan_item.sources
        source_ids = [source.memory_id for source in raw_sources]
        source_issue = False
        validated_sources: list[MemoryOrganizationValidatedSource] = []
        pinned_ids: list[int] = []
        for source_index, source in enumerate(raw_sources):
            snapshot_item = snapshot_by_id.get(source.memory_id)
            if snapshot_item is None:
                collector.add(
                    "source_unknown_memory_id",
                    item_index=item_index,
                    source_index=source_index,
                    memory_id=source.memory_id,
                )
                source_issue = True
                continue
            source_occurrences[source.memory_id] = source_occurrences.get(source.memory_id, 0) + 1
            if source.expected_version != snapshot_item.expected_version:
                collector.add(
                    "source_version_mismatch",
                    item_index=item_index,
                    source_index=source_index,
                    memory_id=source.memory_id,
                    field="expected_version",
                )
                source_issue = True
            if source_occurrences[source.memory_id] > 1:
                collector.add(
                    "source_repeated",
                    item_index=item_index,
                    source_index=source_index,
                    memory_id=source.memory_id,
                )
                source_issue = True
            if snapshot_item.pinned:
                pinned_ids.append(source.memory_id)
            validated_sources.append(
                MemoryOrganizationValidatedSource(
                    memory_id=source.memory_id,
                    expected_version=source.expected_version,
                    pinned=snapshot_item.pinned,
                )
            )

        if plan_item.action == "merge":
            if len(set(source_ids)) != len(source_ids):
                collector.add("merge_sources_not_distinct", item_index=item_index)
                source_issue = True
            if plan_item.primary_memory_id not in set(source_ids):
                collector.add(
                    "merge_primary_not_in_sources",
                    item_index=item_index,
                    memory_id=plan_item.primary_memory_id,
                    field="primary_memory_id",
                )
                source_issue = True
            if len(pinned_ids) > 1:
                collector.add("merge_multiple_pinned_sources", item_index=item_index)
                source_issue = True
            elif pinned_ids and plan_item.primary_memory_id != pinned_ids[0]:
                collector.add(
                    "merge_pinned_source_not_primary",
                    item_index=item_index,
                    memory_id=pinned_ids[0],
                    field="primary_memory_id",
                )
                source_issue = True

        normalized_target = None
        if plan_item.action in {"update", "merge"}:
            normalized_target = _normalize_organization_target(
                plan_item.target,
                collector=collector,
                item_index=item_index,
            )

        summary: dict[str, Any] = {"action": plan_item.action}
        source_summary = [{"memory_id": source.memory_id, "expected_version": source.expected_version} for source in raw_sources]
        if plan_item.action in {"keep", "update"}:
            summary["source"] = source_summary[0]
        else:
            summary["sources"] = source_summary
        if plan_item.action == "merge":
            summary["primary_memory_id"] = plan_item.primary_memory_id
        if normalized_target is not None:
            summary["target"] = {
                "memory_key": normalized_target.memory_key,
                "memory_type": normalized_target.memory_type.value,
                "content_token_count": normalized_target.content_token_count,
                "content_hash": normalized_target.content_hash,
            }
        if plan_item.action == "conflict":
            summary["reason"] = plan_item.reason
        summary_items.append(summary)

        if source_issue or len(validated_sources) != len(raw_sources):
            continue
        validated_item = MemoryOrganizationValidatedItem(
            action=plan_item.action,
            sources=tuple(validated_sources),
            target=normalized_target,
            primary_memory_id=(plan_item.primary_memory_id if plan_item.action == "merge" else None),
            reason=(plan_item.reason if plan_item.action == "conflict" else None),
        )
        validated_items.append(validated_item)

        if plan_item.action in {"keep", "conflict"}:
            for source in validated_sources:
                snapshot_item = snapshot_by_id[source.memory_id]
                memory_key, content_hash = _snapshot_record_identity(snapshot_item)
                final_records.append((source.memory_id, memory_key, content_hash))
        elif normalized_target is not None and plan_item.action == "update":
            final_records.append((validated_sources[0].memory_id, normalized_target.memory_key, normalized_target.content_hash))
        elif normalized_target is not None and plan_item.action == "merge":
            final_records.append((plan_item.primary_memory_id, normalized_target.memory_key, normalized_target.content_hash))

    for snapshot_item in snapshot.items:
        occurrence_count = source_occurrences.get(snapshot_item.memory_id, 0)
        if occurrence_count == 0:
            collector.add("source_missing", memory_id=snapshot_item.memory_id)

    if not collector.total:
        memory_keys: dict[str, int] = {}
        content_hashes: dict[str, int] = {}
        for memory_id, memory_key, content_hash in final_records:
            if memory_key in memory_keys:
                collector.add("final_memory_key_conflict", memory_id=memory_id, field="memory_key")
            else:
                memory_keys[memory_key] = memory_id
            if content_hash in content_hashes:
                collector.add("final_content_hash_conflict", memory_id=memory_id, field="content_hash")
            else:
                content_hashes[content_hash] = memory_id

    plan_summary = {
        "items": summary_items,
        "final_record_count": len(final_records),
    }
    if collector.total:
        raise MemoryOrganizationPlanInvalidError(
            collector.errors,
            action_counts=counts,
            plan_summary=plan_summary,
            validation_error_count=collector.total,
            validation_errors_truncated=collector.total > len(collector.errors),
        )
    return MemoryOrganizationValidatedPlan(items=tuple(validated_items), final_record_count=len(final_records))


def calculate_organization_required_output_tokens(snapshot_count: int) -> int:
    if isinstance(snapshot_count, bool) or not isinstance(snapshot_count, int):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field="snapshot_count")
    if snapshot_count < 0:
        raise MemoryValidationError(ERR_VALUE_MUST_BE_NON_NEGATIVE, field="snapshot_count")
    return snapshot_count * (MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS)


_ORGANIZATION_PAYLOAD_FIELDS = frozenset({"trigger", "snapshot", "organization_model"})
_ORGANIZATION_TRIGGERS = frozenset({"manual", "auto"})
_ORGANIZATION_SNAPSHOT_FIELDS = frozenset(
    {
        "digest",
        "count",
        "active_embedding_revision",
        "index_revision",
        "policy_version",
        "items",
    }
)
_ORGANIZATION_MODEL_FIELDS = frozenset(
    {
        "channel_id",
        "channel_name",
        "model_id",
        "usage",
        "protocol",
        "base_url",
        "api_key",
        "http_proxy",
        "custom_headers",
        "temperature",
        "top_p",
        "timeout",
        "context_window_k",
        "context_window_tokens",
        "max_tokens",
        "snapshot_count",
        "required_output_tokens",
        "policy_version",
    }
)
_CONTEXT_LENGTH_ERROR_TERMS = (
    "context length exceeded",
    "maximum context length",
    "max context length",
    "context window",
    "context limit exceeded",
    "too many tokens",
    "prompt too long",
    "prompt is too long",
    "input too long",
    "input is too long",
    "input length exceeded",
    "token limit exceeded",
)


def _raise_organization_payload_invalid() -> None:
    raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)


def _strict_non_negative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _strict_positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _strict_text(value: Any, *, allow_blank: bool = False) -> bool:
    return isinstance(value, str) and (allow_blank or bool(value.strip()))


def _strict_number(value: Any, *, minimum: float, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum or (maximum is not None and normalized > maximum):
        return None
    return normalized


def _restore_organization_snapshot(value: Any) -> MemoryOrganizationSnapshot:
    if not isinstance(value, dict) or set(value) != _ORGANIZATION_SNAPSHOT_FIELDS:
        _raise_organization_payload_invalid()

    digest = value.get("digest")
    count = value.get("count")
    active_embedding_revision = value.get("active_embedding_revision")
    index_revision = value.get("index_revision")
    policy_version = value.get("policy_version")
    raw_items = value.get("items")
    if not _strict_text(digest) or not _strict_non_negative_integer(count) or not _strict_positive_integer(active_embedding_revision) or not _strict_non_negative_integer(index_revision) or not _strict_positive_integer(policy_version) or not isinstance(raw_items, list):
        _raise_organization_payload_invalid()

    items: list[MemoryOrganizationSnapshotItem] = []
    seen_memory_ids: set[int] = set()
    for raw_item in raw_items:
        try:
            item = MemoryOrganizationSnapshotItem.model_validate(raw_item)
        except Exception as exc:
            raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
        if item.memory_id in seen_memory_ids:
            _raise_organization_payload_invalid()
        seen_memory_ids.add(item.memory_id)
        items.append(item)

    if count != len(items):
        _raise_organization_payload_invalid()
    if [item.memory_id for item in items] != sorted(item.memory_id for item in items):
        _raise_organization_payload_invalid()

    expected_digest = build_organization_snapshot_digest(
        items,
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
    )
    if digest != expected_digest:
        _raise_organization_payload_invalid()
    return MemoryOrganizationSnapshot(
        digest=digest,
        count=count,
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
        items=tuple(items),
    )


def _restore_organization_model_config(
    value: Any,
    *,
    snapshot: MemoryOrganizationSnapshot,
) -> MemoryOrganizationModelConfig:
    if not isinstance(value, dict) or set(value) != _ORGANIZATION_MODEL_FIELDS:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    channel_id = value.get("channel_id")
    channel_name = value.get("channel_name")
    model_id = value.get("model_id")
    usage = value.get("usage")
    raw_protocol = value.get("protocol")
    base_url = value.get("base_url")
    api_key = value.get("api_key")
    raw_http_proxy = value.get("http_proxy")
    raw_custom_headers = value.get("custom_headers")
    temperature = _strict_number(value.get("temperature"), minimum=0, maximum=2)
    raw_top_p = value.get("top_p")
    top_p = None if raw_top_p is None else _strict_number(raw_top_p, minimum=0, maximum=1)
    timeout = _strict_number(value.get("timeout"), minimum=0.000001)
    context_window_k = value.get("context_window_k")
    context_window_tokens = value.get("context_window_tokens")
    max_tokens = value.get("max_tokens")
    snapshot_count = value.get("snapshot_count")
    required_output_tokens = value.get("required_output_tokens")
    policy_version = value.get("policy_version")

    if (
        not _strict_positive_integer(channel_id)
        or not _strict_text(channel_name)
        or not _strict_text(model_id)
        or usage != ModelUsage.CHAT.value
        or not isinstance(raw_protocol, str)
        or not _strict_text(base_url)
        or not _is_valid_organization_base_url(base_url)
        or not _strict_text(api_key)
        or temperature is None
        or (raw_top_p is not None and top_p is None)
        or timeout is None
        or not _strict_positive_integer(context_window_k)
        or not _strict_positive_integer(context_window_tokens)
        or context_window_tokens != context_window_k * CONTEXT_WINDOW_TOKENS_PER_K
        or not _strict_positive_integer(max_tokens)
        or not _strict_non_negative_integer(snapshot_count)
        or snapshot_count != snapshot.count
        or not _strict_non_negative_integer(required_output_tokens)
        or required_output_tokens != calculate_organization_required_output_tokens(snapshot.count)
        or not _strict_positive_integer(policy_version)
        or policy_version != snapshot.policy_version
        or max_tokens < required_output_tokens
    ):
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    try:
        protocol_enum = ModelProtocol(raw_protocol.upper())
        if protocol_enum not in MODEL_PROTOCOLS_BY_USAGE[ModelUsage.CHAT]:
            raise ValueError("organization protocol is not a chat protocol")
        protocol = resolve_model_protocol({"protocol": protocol_enum.value})
    except (KeyError, TypeError, ValueError) as exc:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID) from exc
    if protocol != raw_protocol:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    if not isinstance(raw_custom_headers, dict):
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)
    try:
        custom_headers = normalize_model_custom_headers(raw_custom_headers)
        http_proxy = get_channel_http_proxy({"http_proxy": raw_http_proxy})
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID) from exc
    if custom_headers != raw_custom_headers or http_proxy != raw_http_proxy:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    return MemoryOrganizationModelConfig(
        channel_id=channel_id,
        channel_name=channel_name,
        model_id=model_id,
        usage=usage,
        protocol=protocol,
        context_window_k=context_window_k,
        context_window_tokens=context_window_tokens,
        max_tokens=max_tokens,
        snapshot_count=snapshot_count,
        required_output_tokens=required_output_tokens,
        policy_version=policy_version,
        base_url=base_url,
        api_key=api_key,
        http_proxy=http_proxy,
        custom_headers=custom_headers,
        temperature=temperature,
        top_p=top_p,
        timeout=timeout,
    )


def restore_organization_execution_payload(payload: Any) -> MemoryOrganizationExecutionPayload:
    if not isinstance(payload, dict) or set(payload) != _ORGANIZATION_PAYLOAD_FIELDS:
        _raise_organization_payload_invalid()
    trigger = payload.get("trigger")
    if not isinstance(trigger, str) or trigger not in _ORGANIZATION_TRIGGERS:
        _raise_organization_payload_invalid()

    snapshot = _restore_organization_snapshot(payload.get("snapshot"))
    organization_model = _restore_organization_model_config(
        payload.get("organization_model"),
        snapshot=snapshot,
    )
    return MemoryOrganizationExecutionPayload(
        trigger=trigger,
        snapshot=snapshot,
        organization_model=organization_model,
    )


def build_organization_execution_request(payload: Any) -> MemoryOrganizationExecutionRequest:
    restored = restore_organization_execution_payload(payload)
    messages = (
        InternalMessage(role=MessageRole.SYSTEM, content=MEMORY_ORGANIZATION_SYSTEM_PROMPT),
        InternalMessage(
            role=MessageRole.USER,
            content=json.dumps(
                [item.model_dump(mode="json") for item in restored.snapshot.items],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )
    usage = measure_context_request_usage(
        messages=list(messages),
        context_window_k=restored.organization_model.context_window_k,
        max_tokens=restored.organization_model.required_output_tokens,
        tools=None,
        safety_margin_tokens=MEMORY_ORGANIZE_CONTEXT_SAFETY_MARGIN_TOKENS,
    )
    budget = MemoryOrganizationExecutionBudget(
        required_input_tokens=usage.required_input_tokens,
        available_input_tokens=(usage.budget.context_window_tokens - usage.budget.output_tokens - usage.budget.safety_margin_tokens),
        context_window_tokens=usage.budget.context_window_tokens,
        max_output_tokens=usage.budget.output_tokens,
        safety_margin_tokens=usage.budget.safety_margin_tokens,
        system_tokens=usage.system_tokens,
        non_system_tokens=usage.non_system_tokens,
        message_tokens=usage.message_tokens,
        tools_tokens=usage.budget.tools_tokens,
    )
    return MemoryOrganizationExecutionRequest(
        trigger=restored.trigger,
        snapshot=restored.snapshot,
        organization_model=restored.organization_model,
        messages=messages,
        budget=budget,
    )


async def call_organization_model(request: MemoryOrganizationExecutionRequest) -> InternalResponse:
    if request.budget.exceeds_hard_window:
        raise MemoryOrganizationContextExceededError(request.budget)
    if request.snapshot.count == 0:
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    model = request.organization_model
    return await LLMClient.generate(
        api_key=model.api_key,
        base_url=model.base_url,
        model_id=model.model_id,
        messages=list(request.messages),
        temperature=model.temperature,
        top_p=model.top_p,
        max_tokens=request.budget.max_output_tokens,
        tools=None,
        protocol=model.protocol,
        timeout=model.timeout,
        request_context_tokens=request.budget.required_input_tokens,
        http_proxy=model.http_proxy,
        custom_headers=dict(model.custom_headers),
    )


def is_external_context_length_error(exc: BaseException) -> bool:
    visited: set[int] = set()

    def visit(value: Any) -> bool:
        if value is None or id(value) in visited:
            return False
        visited.add(id(value))
        if isinstance(value, str):
            normalized = value.lower().replace("_", " ").replace("-", " ")
            if any(term in normalized for term in _CONTEXT_LENGTH_ERROR_TERMS):
                return True
            return False
        if isinstance(value, Mapping):
            return any(visit(key) or visit(item) for key, item in value.items())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(visit(item) for item in value)
        if isinstance(value, BaseException):
            if not isinstance(value, LLMException) and not isinstance(exc, LLMException):
                return visit(str(value))
            if visit(str(value)):
                return True
            for attribute in (
                "message",
                "kwargs",
                "cause",
                "data",
                "code",
                "type",
                "detail",
                "__cause__",
                "__context__",
            ):
                if visit(getattr(value, attribute, None)):
                    return True
            return False
        for attribute in ("detail", "code", "type", "message"):
            if visit(getattr(value, attribute, None)):
                return True
        return visit(str(value)) if not isinstance(value, (int, float, bool)) else False

    if not isinstance(exc, LLMException):
        return False
    return visit(exc)


execute_organization_model = call_organization_model


def build_organization_snapshot_items(
    records: Iterable[LongTermMemoryRecord],
) -> tuple[MemoryOrganizationSnapshotItem, ...]:
    return tuple(
        MemoryOrganizationSnapshotItem.model_validate(
            {
                "memory_id": record.id,
                "expected_version": record.version,
                "memory_key": record.memory_key,
                "memory_type": record.memory_type,
                "content": record.content,
                "content_token_count": record.content_token_count,
                "pinned": record.pinned,
            }
        )
        for record in sorted(records, key=lambda item: item.id or 0)
    )


def build_organization_snapshot_digest(
    items: Iterable[MemoryOrganizationSnapshotItem],
    *,
    active_embedding_revision: int,
    index_revision: int,
    policy_version: int,
) -> str:
    ordered_items = tuple(sorted(items, key=lambda item: item.memory_id))
    digest_payload = {
        "active_embedding_revision": active_embedding_revision,
        "index_revision": index_revision,
        "items": [item.model_dump(mode="json") for item in ordered_items],
        "policy_version": policy_version,
    }
    return hashlib.sha256(canonical_json_dumps(digest_payload).encode("utf-8")).hexdigest()


def build_organization_snapshot(
    records: Iterable[LongTermMemoryRecord],
    *,
    active_embedding_revision: int,
    index_revision: int,
    policy_version: int,
) -> MemoryOrganizationSnapshot:
    items = build_organization_snapshot_items(records)
    digest = build_organization_snapshot_digest(
        items,
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
    )
    return MemoryOrganizationSnapshot(
        digest=digest,
        count=len(items),
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
        items=items,
    )


def build_organization_dedupe_key(
    uid: str,
    *,
    snapshot_digest: str,
    policy_version: int,
    caller_dedupe_key: str | None = None,
) -> str:
    normalized_uid = _normalize_uid(uid)
    normalized_caller_key = _normalize_dedupe_key(caller_dedupe_key) if caller_dedupe_key is not None else None
    payload = {
        "caller_dedupe_key": normalized_caller_key,
        "policy_version": policy_version,
        "scope": "memory_organization",
        "snapshot_digest": snapshot_digest,
        "uid": normalized_uid,
    }
    digest = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"memory_organization:{digest}"


def build_organization_job_payload(
    snapshot: MemoryOrganizationSnapshot,
    organization_model: MemoryOrganizationModelConfig,
    trigger: str = "manual",
) -> dict[str, Any]:
    if not isinstance(trigger, str) or trigger not in _ORGANIZATION_TRIGGERS:
        _raise_organization_payload_invalid()
    payload = {
        "trigger": trigger,
        "snapshot": snapshot.to_job_snapshot(),
        "organization_model": organization_model.to_job_snapshot(),
    }
    canonical_json_dumps(payload)
    return payload


def validate_organization_submission_store(store: LongTermMemoryStore) -> None:
    required = (
        store.active_embedding_channel_id,
        store.active_embedding_model_id,
        store.active_embedding_dimensions,
        store.active_embedding_signature,
        store.active_collection_name,
    )
    if (
        isinstance(store.active_embedding_revision, bool)
        or not isinstance(store.active_embedding_revision, int)
        or store.active_embedding_revision < 1
        or any(value is None or (isinstance(value, str) and not value.strip()) for value in required)
        or isinstance(store.active_embedding_channel_id, bool)
        or not isinstance(store.active_embedding_channel_id, int)
        or store.active_embedding_channel_id < 1
        or isinstance(store.active_embedding_dimensions, bool)
        or not isinstance(store.active_embedding_dimensions, int)
        or store.active_embedding_dimensions < 1
    ):
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)

    try:
        index_status = LongTermMemoryIndexStatus(store.index_status)
        migration_status = LongTermMemoryMigrationStatus(store.migration_status) if store.migration_status is not None else None
    except (TypeError, ValueError) as exc:
        raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT) from exc
    if index_status != LongTermMemoryIndexStatus.READY or migration_status in {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
    }:
        raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)


def _raise_organization_config_invalid() -> None:
    raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)


def _validate_organization_selection(
    channel_id: Any,
    model_id: Any,
    *,
    missing_error: str = ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
) -> tuple[int | None, str | None]:
    if channel_id is None and model_id is None:
        if missing_error == ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED:
            raise MemoryValidationError(missing_error)
        return None, None
    if channel_id is None or model_id is None:
        _raise_organization_config_invalid()
    if isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id < 1:
        _raise_organization_config_invalid()
    if not isinstance(model_id, str) or not model_id.strip() or model_id != model_id.strip():
        _raise_organization_config_invalid()
    return channel_id, model_id


def _is_valid_organization_base_url(base_url: Any) -> bool:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        return False
    if any(character.isspace() or ord(character) < 32 for character in base_url):
        return False
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    return True


def _is_positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


async def _load_organization_model_config_for_selection(
    db: AsyncSession,
    *,
    store: LongTermMemoryStore,
    channel_id: Any,
    model_id: Any,
    snapshot_count: int,
) -> MemoryOrganizationModelConfig:
    normalized_channel_id, normalized_model_id = _validate_organization_selection(channel_id, model_id)
    if normalized_channel_id is None or normalized_model_id is None:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED)
    required_output_tokens = calculate_organization_required_output_tokens(snapshot_count)

    channel = await channel_crud.get(db, normalized_channel_id)
    if channel is None or channel.is_active is not True or not _is_valid_organization_base_url(channel.base_url):
        _raise_organization_config_invalid()

    try:
        api_key = channel.get_decrypted_api_key()
        http_proxy = get_channel_http_proxy(channel)
    except Exception:
        _raise_organization_config_invalid()
    if not isinstance(api_key, str) or not api_key.strip():
        _raise_organization_config_invalid()

    selected_item: ChannelModelItem | None = None
    context_window_k: Any = None
    max_tokens: Any = None
    for raw_item in channel.model_ids or []:
        if not isinstance(raw_item, dict) or raw_item.get("model_id") != normalized_model_id:
            continue
        try:
            item = ChannelModelItem.model_validate(raw_item)
        except Exception:
            continue
        if item.model_id != normalized_model_id or item.usage != ModelUsage.CHAT or item.is_enabled is not True:
            continue
        raw_context_window_k = raw_item.get("context_window_k")
        raw_max_tokens = raw_item.get("max_tokens")
        if not _is_positive_integer(raw_context_window_k) or not _is_positive_integer(raw_max_tokens):
            continue
        selected_item = item
        context_window_k = raw_context_window_k
        max_tokens = raw_max_tokens
        break

    if selected_item is None:
        _raise_organization_config_invalid()
    if max_tokens < required_output_tokens:
        raise MemoryValidationError(
            ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
            params={"required_tokens": required_output_tokens, "available_tokens": max_tokens},
            data={
                "required_tokens": required_output_tokens,
                "available_tokens": max_tokens,
            },
        )

    context_window_tokens = context_window_k * CONTEXT_WINDOW_TOKENS_PER_K
    return MemoryOrganizationModelConfig(
        channel_id=normalized_channel_id,
        channel_name=channel.name,
        model_id=selected_item.model_id,
        usage=selected_item.usage.value,
        protocol=resolve_model_protocol({"protocol": selected_item.protocol.value}),
        base_url=channel.base_url,
        api_key=api_key,
        http_proxy=http_proxy,
        custom_headers=selected_item.advanced_settings.custom_headers,
        temperature=selected_item.temperature if selected_item.temperature is not None else 0.7,
        top_p=selected_item.top_p,
        timeout=MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
        context_window_k=context_window_k,
        context_window_tokens=context_window_tokens,
        max_tokens=max_tokens,
        snapshot_count=snapshot_count,
        required_output_tokens=required_output_tokens,
        policy_version=store.organization_policy_version,
    )


async def load_organization_model_config(
    db: AsyncSession,
    *,
    uid: str,
    snapshot_count: int,
) -> MemoryOrganizationModelConfig:
    normalized_uid = _normalize_uid(uid)
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
    if store is None:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED)
    return await _load_organization_model_config_for_selection(
        db,
        store=store,
        channel_id=store.organization_channel_id,
        model_id=store.organization_model_id,
        snapshot_count=snapshot_count,
    )


async def load_organization_model_config_for_store(
    db: AsyncSession,
    *,
    store: LongTermMemoryStore,
    snapshot_count: int,
) -> MemoryOrganizationModelConfig:
    return await _load_organization_model_config_for_selection(
        db,
        store=store,
        channel_id=store.organization_channel_id,
        model_id=store.organization_model_id,
        snapshot_count=snapshot_count,
    )


async def get_organization_settings(
    db: AsyncSession,
    *,
    uid: str,
    snapshot_count: int,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    required_output_tokens = calculate_organization_required_output_tokens(snapshot_count)
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
    if store is None:
        return {
            "auto_organize_enabled": False,
            "channel_id": None,
            "model_id": None,
            "policy_version": MEMORY_ORGANIZE_POLICY_VERSION,
            "last_job_id": None,
            "last_run_at": None,
            "error": None,
            "snapshot_count": snapshot_count,
            "required_output_tokens": required_output_tokens,
            "model": None,
            "validation_error": None,
        }

    validation_error = None
    model = None
    has_selection = store.organization_channel_id is not None or store.organization_model_id is not None
    if has_selection or store.auto_organize_enabled:
        try:
            model = (await load_organization_model_config(db, uid=normalized_uid, snapshot_count=snapshot_count)).to_public_dict()
        except MemoryValidationError as exc:
            validation_error = exc.message

    return {
        "auto_organize_enabled": store.auto_organize_enabled,
        "channel_id": store.organization_channel_id,
        "model_id": store.organization_model_id,
        "policy_version": store.organization_policy_version,
        "last_job_id": store.organization_last_job_id,
        "last_run_at": store.organization_last_run_at,
        "error": store.organization_error,
        "snapshot_count": snapshot_count,
        "required_output_tokens": model["required_output_tokens"] if model is not None else required_output_tokens,
        "model": model,
        "validation_error": validation_error,
    }


async def update_organization_settings(
    db: AsyncSession,
    *,
    uid: str,
    auto_organize_enabled: bool,
    organization_channel_id: int | None,
    organization_model_id: str | None,
    commit: bool = True,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_commit = _validate_commit(commit)
    if not isinstance(auto_organize_enabled, bool):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field="auto_organize_enabled")
    if (organization_channel_id is None) != (organization_model_id is None):
        _raise_organization_config_invalid()
    if organization_channel_id is not None and organization_model_id is not None:
        _validate_organization_selection(organization_channel_id, organization_model_id)

    try:
        store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
        if store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        if organization_channel_id is not None and organization_model_id is not None:
            await _load_organization_model_config_for_selection(
                db,
                store=store,
                channel_id=organization_channel_id,
                model_id=organization_model_id,
                snapshot_count=0,
            )
        elif auto_organize_enabled:
            raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED)

        updated_store = await memory_store_crud.update_by_uid(
            db,
            uid=normalized_uid,
            auto_organize_enabled=auto_organize_enabled,
            organization_channel_id=organization_channel_id,
            organization_model_id=organization_model_id,
            commit=False,
        )
        if updated_store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        if normalized_commit:
            await db.commit()
        active_count = await memory_record_crud.count_active(db, uid=normalized_uid)
        return await get_organization_settings(db, uid=normalized_uid, snapshot_count=active_count)
    except Exception:
        await db.rollback()
        raise


def _deduplicate_memory_ids(memory_ids: Iterable[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    ordered_ids: list[int] = []
    for memory_id in memory_ids:
        if memory_id not in seen:
            seen.add(memory_id)
            ordered_ids.append(memory_id)
    return tuple(ordered_ids)


def evaluate_organization_merge_pins(
    source_memory_ids: Iterable[int],
    primary_memory_id: int | None,
    pinned_memory_ids: Iterable[int],
) -> MemoryOrganizationPinPolicyResult:
    """Evaluate merge candidates against the server-side pin snapshot."""
    source_ids = _deduplicate_memory_ids(source_memory_ids)
    pinned_ids = _deduplicate_memory_ids(pinned_memory_ids)
    source_id_set = set(source_ids)

    if primary_memory_id not in source_id_set:
        raise ValueError("primary_memory_id must belong to source_memory_ids")
    if not set(pinned_ids).issubset(source_id_set):
        raise ValueError("pinned_memory_ids must be a subset of source_memory_ids")

    if len(pinned_ids) > 1:
        return MemoryOrganizationPinPolicyResult(
            status=MemoryOrganizationPinPolicyStatus.CONFLICT,
            primary_memory_id=None,
            pinned_memory_ids=pinned_ids,
            tombstone_memory_ids=(),
        )

    if len(pinned_ids) == 1 and pinned_ids[0] != primary_memory_id:
        return MemoryOrganizationPinPolicyResult(
            status=MemoryOrganizationPinPolicyStatus.INVALID_PRIMARY,
            primary_memory_id=pinned_ids[0],
            pinned_memory_ids=pinned_ids,
            tombstone_memory_ids=(),
        )

    tombstone_ids = tuple(memory_id for memory_id in source_ids if memory_id != primary_memory_id)
    return MemoryOrganizationPinPolicyResult(
        status=MemoryOrganizationPinPolicyStatus.MERGE,
        primary_memory_id=primary_memory_id,
        pinned_memory_ids=pinned_ids,
        tombstone_memory_ids=tombstone_ids,
    )


__all__ = [
    "MemoryOrganizationConflict",
    "MemoryOrganizationContextExceededError",
    "MemoryOrganizationExecutionBudget",
    "MemoryOrganizationExecutionPayload",
    "MemoryOrganizationExecutionRequest",
    "MemoryOrganizationKeep",
    "MemoryOrganizationMerge",
    "MemoryOrganizationModelConfig",
    "MemoryOrganizationPlanCounts",
    "MemoryOrganizationPlanInvalidError",
    "MemoryOrganizationPinPolicyResult",
    "MemoryOrganizationPinPolicyStatus",
    "MemoryOrganizationPlan",
    "MemoryOrganizationPlanItem",
    "MemoryOrganizationSnapshot",
    "MemoryOrganizationSnapshotItem",
    "MemoryOrganizationSourceReference",
    "MemoryOrganizationTarget",
    "MemoryOrganizationUpdate",
    "MemoryOrganizationValidatedItem",
    "MemoryOrganizationValidatedPlan",
    "MemoryOrganizationValidatedSource",
    "MemoryOrganizationValidatedTarget",
    "build_organization_execution_request",
    "calculate_organization_required_output_tokens",
    "build_organization_dedupe_key",
    "build_organization_job_payload",
    "build_organization_snapshot",
    "build_organization_snapshot_digest",
    "build_organization_snapshot_items",
    "call_organization_model",
    "execute_organization_model",
    "evaluate_organization_merge_pins",
    "get_organization_settings",
    "load_organization_model_config",
    "load_organization_model_config_for_store",
    "is_external_context_length_error",
    "restore_organization_execution_payload",
    "update_organization_settings",
    "validate_organization_model_output",
    "validate_organization_submission_store",
]
