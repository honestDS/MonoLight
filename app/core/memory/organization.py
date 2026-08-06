from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


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
    "MemoryOrganizationPinPolicyResult",
    "MemoryOrganizationPinPolicyStatus",
    "evaluate_organization_merge_pins",
]
