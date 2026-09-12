from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from app.core.constants import (
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_ORGANIZATION_PLAN_INVALID,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS,
)
from app.core.memory.errors import MemoryContentTooLongError, MemoryValidationError
from app.core.memory.normalization import (
    build_memory_content_hash,
    normalize_memory_content_for_publication,
    normalize_memory_key,
)
from app.core.memory.organization_types import (
    MemoryOrganizationPlan,
    MemoryOrganizationSnapshotItem,
    MemoryOrganizationTarget,
)
from app.core.prompts import MEMORY_ORGANIZATION_SYSTEM_PROMPT
from app.core.utils.context_budget import measure_context_request_usage
from app.models.memory import (
    LongTermMemoryType,
)
from app.models.message import InternalMessage, MessageRole

from .organization_contracts import (
    MemoryOrganizationPlanCounts,
    MemoryOrganizationSnapshot,
    MemoryOrganizationValidatedItem,
    MemoryOrganizationValidatedPlan,
    MemoryOrganizationValidatedSource,
    MemoryOrganizationValidatedTarget,
)

__all__ = [
    "MemoryOrganizationPlanInvalidError",
    "validate_organization_model_output",
    "calculate_organization_required_output_tokens",
    "calculate_organization_required_input_tokens",
]


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
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "snapshot_count"})
    if snapshot_count < 0:
        raise MemoryValidationError(ERR_VALUE_MUST_BE_NON_NEGATIVE, params={"field": "snapshot_count"})
    return snapshot_count * (MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS)


def _build_organization_execution_messages(
    snapshot_items: Iterable[MemoryOrganizationSnapshotItem],
) -> tuple[InternalMessage, ...]:
    return (
        InternalMessage(role=MessageRole.SYSTEM, content=MEMORY_ORGANIZATION_SYSTEM_PROMPT),
        InternalMessage(
            role=MessageRole.USER,
            content=json.dumps(
                [item.model_dump(mode="json") for item in snapshot_items],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )


def calculate_organization_required_input_tokens(
    snapshot_items: Iterable[MemoryOrganizationSnapshotItem],
) -> int:
    items = tuple(snapshot_items)
    messages = _build_organization_execution_messages(items)
    usage = measure_context_request_usage(
        messages=list(messages),
        context_window_k=1,
        max_tokens=0,
        tools=None,
        safety_margin_tokens=0,
    )
    return usage.required_input_tokens
