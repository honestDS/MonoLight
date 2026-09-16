from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.core.memory.organization import (
    MemoryOrganizationPinPolicyResult,
    MemoryOrganizationPinPolicyStatus,
    evaluate_organization_merge_pins,
)


def test_merge_without_pins_deduplicates_sources_and_preserves_tombstone_order() -> None:
    result = evaluate_organization_merge_pins(
        source_memory_ids=[3, 1, 3, 2, 1],
        primary_memory_id=2,
        pinned_memory_ids=[],
    )

    assert result == MemoryOrganizationPinPolicyResult(
        status=MemoryOrganizationPinPolicyStatus.MERGE,
        primary_memory_id=2,
        pinned_memory_ids=(),
        tombstone_memory_ids=(3, 1),
    )


def test_merge_with_primary_pin_excludes_pinned_memory_from_tombstones() -> None:
    result = evaluate_organization_merge_pins(
        source_memory_ids=[1, 2, 3],
        primary_memory_id=2,
        pinned_memory_ids=[2, 2],
    )

    assert result.status is MemoryOrganizationPinPolicyStatus.MERGE
    assert result.primary_memory_id == 2
    assert result.pinned_memory_ids == (2,)
    assert result.tombstone_memory_ids == (1, 3)


def test_single_pin_replaces_model_primary_without_tombstones() -> None:
    result = evaluate_organization_merge_pins(
        source_memory_ids=[1, 2, 3],
        primary_memory_id=1,
        pinned_memory_ids=[2],
    )

    assert result.status is MemoryOrganizationPinPolicyStatus.INVALID_PRIMARY
    assert result.primary_memory_id == 2
    assert result.pinned_memory_ids == (2,)
    assert result.tombstone_memory_ids == ()


def test_multiple_pins_are_a_conflict_and_protect_all_pinned_records() -> None:
    result = evaluate_organization_merge_pins(
        source_memory_ids=[1, 2, 3],
        primary_memory_id=2,
        pinned_memory_ids=[3, 1, 3],
    )

    assert result.status is MemoryOrganizationPinPolicyStatus.CONFLICT
    assert result.primary_memory_id is None
    assert result.pinned_memory_ids == (3, 1)
    assert result.tombstone_memory_ids == ()


@pytest.mark.parametrize(
    ("source_memory_ids", "primary_memory_id", "pinned_memory_ids"),
    [
        ([1, 2], 3, []),
        ([1, 2], 1, [3]),
    ],
)
def test_primary_and_pinned_ids_must_be_in_sources(
    source_memory_ids: list[int],
    primary_memory_id: int,
    pinned_memory_ids: list[int],
) -> None:
    with pytest.raises(ValueError):
        evaluate_organization_merge_pins(source_memory_ids, primary_memory_id, pinned_memory_ids)


def test_pin_snapshot_is_not_mutated_and_result_is_immutable() -> None:
    pinned_memory_ids = [2]

    result = evaluate_organization_merge_pins([1, 2], 2, pinned_memory_ids)

    assert pinned_memory_ids == [2]
    with pytest.raises(FrozenInstanceError):
        result.pinned_memory_ids = (1,)
