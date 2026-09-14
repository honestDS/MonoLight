from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import ERR_KNOWLEDGE_ORGANIZATION_PLAN_STALE
from app.core.crud.knowledge.job import is_organization_operation, knowledge_job_crud
from app.core.crud.knowledge.managed import (
    managed_knowledge_item_crud,
    organization_lock_token_for_job,
)
from app.core.crud.knowledge.organization import (
    knowledge_organization_fragment_crud,
    knowledge_organization_snapshot_crud,
    knowledge_organization_snapshot_item_crud,
    knowledge_organization_stage_crud,
)
from app.core.knowledge.errors import KnowledgeOrganizationExecutionError
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationPlan,
    KnowledgeOrganizationPlanItem,
)
from app.models.knowledge_base import (
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationFragmentStatus,
    KnowledgeOrganizationStageStatus,
)
from app.providers.database.time import get_database_time

_STALE_STATUS = "organization_plan_stale"
_PLAN_ACTIONS = frozenset({"keep", "update", "merge", "conflict"})
_MUTATION_ACTIONS = frozenset({"update", "merge"})
_SNAPSHOT_PAGE_SIZE = 200


class KnowledgeOrganizationPlanStaleError(KnowledgeOrganizationExecutionError):
    def __init__(self) -> None:
        super().__init__(
            message=ERR_KNOWLEDGE_ORGANIZATION_PLAN_STALE,
            code=409,
            status=_STALE_STATUS,
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationPublicationResult:
    mutation_job_ids: tuple[int, ...]


def _stale() -> NoReturn:
    raise KnowledgeOrganizationPlanStaleError()


def _normalize_plan(value: Any) -> KnowledgeOrganizationPlan:
    try:
        return value if isinstance(value, KnowledgeOrganizationPlan) else KnowledgeOrganizationPlan.model_validate(value)
    except (TypeError, ValueError):
        _stale()


def _fragment_plan(fragment_result: Any) -> KnowledgeOrganizationPlan:
    if not isinstance(fragment_result, Mapping):
        _stale()
    plan_value = fragment_result.get("plan")
    if plan_value is None and "items" in fragment_result:
        plan_value = fragment_result
    return _normalize_plan(plan_value)


def _plans_match(left: KnowledgeOrganizationPlan, right: KnowledgeOrganizationPlan) -> bool:
    return canonical_json_dumps(left.model_dump(mode="json")) == canonical_json_dumps(right.model_dump(mode="json"))


def _item_sources(item: KnowledgeOrganizationPlanItem) -> tuple[Any, ...]:
    if item.action in {"keep", "update"}:
        return (item.source,)
    return item.sources


def _validate_plan_sources(
    plan: KnowledgeOrganizationPlan,
) -> dict[int, int]:
    expected_versions: dict[int, int] = {}
    seen_source_ids: set[int] = set()
    for item in plan.items:
        if item.action not in _PLAN_ACTIONS:
            _stale()
        sources = _item_sources(item)
        for source in sources:
            if source.knowledge_id in seen_source_ids:
                _stale()
            expected_versions[source.knowledge_id] = source.expected_version
            seen_source_ids.add(source.knowledge_id)
        if item.action == "merge" and item.primary_knowledge_id not in {source.knowledge_id for source in sources}:
            _stale()

    return expected_versions


def _validate_current_items(
    items: list[Any],
    *,
    uid: str,
    knowledge_base_id: int,
    snapshot_versions: Mapping[int, int],
    organization_job_id: int,
) -> None:
    by_id: dict[int, Any] = {}
    for item in items:
        if item.id is None or item.id in by_id:
            _stale()
        by_id[item.id] = item
    if set(by_id) != set(snapshot_versions):
        _stale()

    lock_token = organization_lock_token_for_job(organization_job_id)
    for knowledge_id, expected_version in snapshot_versions.items():
        item = by_id[knowledge_id]
        if (
            item.uid != uid
            or item.knowledge_base_id != knowledge_base_id
            or item.id != knowledge_id
            or item.deleted_at is not None
            or item.llm_maintainable is not True
            or item.is_recallable is not True
            or item.pending_job_id is not None
            or item.indexed_version != item.version
            or item.version != expected_version
            or item.organization_lock_token != lock_token
        ):
            _stale()


def _mutation_payload(
    *,
    snapshot_id: int,
    stage_id: int,
    plan_item_index: int,
    item: KnowledgeOrganizationPlanItem,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "stage_id": stage_id,
        "plan_item_index": plan_item_index,
        "action": item.action,
        "sources": [
            {
                "knowledge_id": source.knowledge_id,
                "expected_version": source.expected_version,
            }
            for source in _item_sources(item)
        ],
    }
    if item.action == "merge":
        payload["primary_knowledge_id"] = item.primary_knowledge_id
    return payload


async def load_knowledge_organization_mutation_item(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    snapshot_id: int,
    stage_id: int,
    plan_item_index: int,
) -> KnowledgeOrganizationPlanItem:
    snapshot = await knowledge_organization_snapshot_crud.get_by_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        snapshot_id=snapshot_id,
    )
    stage = await knowledge_organization_stage_crud.get_by_id(db, stage_id=stage_id)
    if snapshot is None or stage is None or stage.uid != uid or stage.knowledge_base_id != knowledge_base_id or stage.snapshot_id != snapshot_id or stage.status != KnowledgeOrganizationStageStatus.COMPLETED or stage.expected_fragment_count != 1:
        _stale()

    fragment = await knowledge_organization_fragment_crud.get_by_stage_and_index(
        db,
        stage_id=stage_id,
        fragment_index=0,
    )
    if fragment is None or fragment.uid != uid or fragment.knowledge_base_id != knowledge_base_id or fragment.snapshot_id != snapshot_id or fragment.stage_id != stage_id or fragment.fragment_index != 0 or fragment.status != KnowledgeOrganizationFragmentStatus.COMPLETED:
        _stale()

    plan = _fragment_plan(fragment.result)
    if isinstance(plan_item_index, bool) or not isinstance(plan_item_index, int) or plan_item_index < 0 or plan_item_index >= len(plan.items):
        _stale()
    item = plan.items[plan_item_index]
    if item.action not in _MUTATION_ACTIONS:
        _stale()
    return item


def _mutation_target(item: KnowledgeOrganizationPlanItem) -> tuple[int, int]:
    if item.action == "update":
        source = item.source
        return source.knowledge_id, source.expected_version
    primary_knowledge_id = item.primary_knowledge_id
    for source in item.sources:
        if source.knowledge_id == primary_knowledge_id:
            return source.knowledge_id, source.expected_version
    _stale()


async def publish_knowledge_organization_plan(
    session_factory: Any,
    *,
    uid: str,
    knowledge_base_id: int,
    organization_job_id: int,
    owner: str,
    snapshot_id: int,
    final_stage_id: int,
    plan: KnowledgeOrganizationPlan,
) -> KnowledgeOrganizationPublicationResult:
    normalized_plan = _normalize_plan(plan)

    async with session_factory() as db:
        try:
            parent_job = await knowledge_job_crud.lock_by_id(db, uid=uid, job_id=organization_job_id)
            database_time = await get_database_time(db)
            if (
                parent_job is None
                or parent_job.uid != uid
                or parent_job.knowledge_base_id != knowledge_base_id
                or not is_organization_operation(parent_job.operation)
                or parent_job.status != KnowledgeJobStatus.RUNNING
                or parent_job.locked_by != owner
                or parent_job.lock_until is None
                or parent_job.lock_until < database_time
                or parent_job.cancel_requested_at is not None
            ):
                _stale()

            snapshot = await knowledge_organization_snapshot_crud.get_by_id(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
                snapshot_id=snapshot_id,
            )
            if snapshot is None or snapshot.id != snapshot_id or snapshot.item_count < 0:
                _stale()

            stage = await knowledge_organization_stage_crud.get_by_id(db, stage_id=final_stage_id)
            if stage is None or stage.id != final_stage_id or stage.uid != uid or stage.knowledge_base_id != knowledge_base_id or stage.snapshot_id != snapshot_id or stage.status != KnowledgeOrganizationStageStatus.COMPLETED or stage.expected_fragment_count != 1:
                _stale()

            fragment = await knowledge_organization_fragment_crud.get_by_stage_and_index(
                db,
                stage_id=final_stage_id,
                fragment_index=0,
            )
            if fragment is None or fragment.uid != uid or fragment.knowledge_base_id != knowledge_base_id or fragment.snapshot_id != snapshot_id or fragment.stage_id != final_stage_id or fragment.fragment_index != 0 or fragment.status != KnowledgeOrganizationFragmentStatus.COMPLETED:
                _stale()
            if not _plans_match(_fragment_plan(fragment.result), normalized_plan):
                _stale()

            expected_versions = _validate_plan_sources(normalized_plan)
            after_sequence = -1
            read_count = 0
            seen_snapshot_ids: set[int] = set()
            while read_count < snapshot.item_count:
                page_limit = min(_SNAPSHOT_PAGE_SIZE, snapshot.item_count - read_count)
                page = await knowledge_organization_snapshot_item_crud.list_with_revision_page(
                    db,
                    snapshot_id=snapshot_id,
                    after_sequence=after_sequence,
                    limit=page_limit,
                )
                if not page or len(page) > _SNAPSHOT_PAGE_SIZE or read_count + len(page) > snapshot.item_count:
                    _stale()

                page_versions: dict[int, int] = {}
                for row, _revision in page:
                    if row.uid != uid or row.knowledge_base_id != knowledge_base_id or row.snapshot_id != snapshot_id or row.sequence != after_sequence + 1 or row.knowledge_id not in expected_versions or row.expected_version != expected_versions[row.knowledge_id] or row.knowledge_id in seen_snapshot_ids:
                        _stale()
                    page_versions[row.knowledge_id] = row.expected_version
                    seen_snapshot_ids.add(row.knowledge_id)
                    after_sequence = row.sequence

                read_count += len(page)
                current_items = await managed_knowledge_item_crud.get_by_ids(
                    db,
                    uid=uid,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_ids=tuple(page_versions),
                )
                _validate_current_items(
                    current_items,
                    uid=uid,
                    knowledge_base_id=knowledge_base_id,
                    snapshot_versions=page_versions,
                    organization_job_id=organization_job_id,
                )

            trailing_page = await knowledge_organization_snapshot_item_crud.list_with_revision_page(
                db,
                snapshot_id=snapshot_id,
                after_sequence=after_sequence,
                limit=1,
            )
            if trailing_page or read_count != snapshot.item_count or seen_snapshot_ids != set(expected_versions):
                _stale()

            mutation_job_ids: list[int] = []
            for plan_item_index, item in enumerate(normalized_plan.items):
                if item.action not in _MUTATION_ACTIONS:
                    continue

                payload = _mutation_payload(
                    snapshot_id=snapshot_id,
                    stage_id=final_stage_id,
                    plan_item_index=plan_item_index,
                    item=item,
                )
                dedupe_key = f"knowledge-organization:{organization_job_id}:mutation:{plan_item_index}"
                active_change_key = f"knowledge-organization-mutation:{organization_job_id}:{plan_item_index}"
                request_hash = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
                knowledge_id, expected_version = _mutation_target(item)
                child_job, _created = await knowledge_job_crud.create(
                    db,
                    uid=uid,
                    commit=False,
                    parent_job_id=organization_job_id,
                    operation=KnowledgeJobOperation.ORGANIZE_MUTATION,
                    dedupe_key=dedupe_key,
                    request_hash=request_hash,
                    active_change_key=active_change_key,
                    status=KnowledgeJobStatus.PENDING,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_id=knowledge_id,
                    expected_version=expected_version,
                    payload=payload,
                    available_at=database_time,
                )
                if child_job.id is None:
                    _stale()
                if (
                    child_job.operation != KnowledgeJobOperation.ORGANIZE_MUTATION
                    or child_job.parent_job_id != organization_job_id
                    or child_job.knowledge_base_id != knowledge_base_id
                    or child_job.knowledge_id != knowledge_id
                    or child_job.expected_version != expected_version
                    or child_job.request_hash != request_hash
                    or child_job.payload != payload
                ):
                    _stale()
                mutation_job_ids.append(child_job.id)

            await db.commit()
            return KnowledgeOrganizationPublicationResult(mutation_job_ids=tuple(mutation_job_ids))
        except Exception:
            if db.in_transaction():
                await db.rollback()
            raise


__all__ = [
    "KnowledgeOrganizationPlanStaleError",
    "KnowledgeOrganizationPublicationResult",
    "load_knowledge_organization_mutation_item",
    "publish_knowledge_organization_plan",
]
