from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.organization import (
    knowledge_organization_fragment_crud,
    knowledge_organization_stage_crud,
)
from app.core.knowledge.managed import build_managed_knowledge_snapshot
from app.core.knowledge.organization_lifecycle import (
    coordinate_organization_cancel_request,
    coordinate_organization_terminal,
)
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationFragmentStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
    KnowledgeOrganizationStage,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database.time import get_database_time

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    KnowledgeJob.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeOrganizationSnapshot.__table__,
    KnowledgeOrganizationSnapshotItem.__table__,
    KnowledgeOrganizationStage.__table__,
    KnowledgeOrganizationFragment.__table__,
)


@pytest_asyncio.fixture(name="session_factory")
async def _session_factory(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(tables=_TABLES, foreign_keys=True, poolclass=NullPool) as factory:
        yield factory


async def _create_managed_container(db: AsyncSession, *, suffix: str = "main") -> KnowledgeBase:
    channel = ModelChannel(
        name=f"organization-knowledge_organization_publication-channel-{suffix}",
        api_key="test-key",
        base_url="https://example.invalid",
        model_ids=[],
    )
    db.add(channel)
    await db.flush()

    prompt = PromptLibrary(uid="user-1", name=f"organization-knowledge_organization_publication-prompt-{suffix}", content="prompt")
    db.add(prompt)
    await db.flush()

    profile = Profile(
        uid="user-1",
        name=f"organization-knowledge_organization_publication-profile-{suffix}",
        prompt_id=prompt.id,
        configs={},
    )
    db.add(profile)
    await db.flush()

    knowledge_base = KnowledgeBase(
        uid="user-1",
        name=f"managed-knowledge_organization_publication-{suffix}",
        embedding_channel_id=channel.id,
        embedding_model_id="embedding-model",
        embedding_dimensions=1536,
        collection_name=f"managed-knowledge_organization_publication-{suffix}-collection",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=channel.id,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=1536,
        active_embedding_signature="embedding-signature",
        active_embedding_revision=3,
        active_collection_name=f"managed-knowledge_organization_publication-{suffix}-active",
        index_revision=5,
    )
    db.add(knowledge_base)
    await db.commit()
    await db.refresh(knowledge_base)
    return knowledge_base


async def _add_item(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    knowledge_key: str,
    content: str,
    llm_maintainable: bool = True,
    is_recallable: bool = True,
) -> ManagedKnowledgeItem:
    item = ManagedKnowledgeItem(
        knowledge_base_id=knowledge_base_id,
        uid="user-1",
        knowledge_key=knowledge_key,
        content=content,
        content_token_count=max(1, len(content.split())),
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        version=1,
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        source_reference={"source": knowledge_key},
        created_by=ManagedKnowledgeActorType.LLM,
        last_modified_by=ManagedKnowledgeActorType.LLM,
        llm_maintainable=llm_maintainable,
        indexed_version=1 if is_recallable else 0,
        vector_item_ids=[f"vector-{knowledge_key}"] if is_recallable else [],
        is_recallable=is_recallable,
        pending_job_id=None,
    )
    db.add(item)
    await db.flush()
    db.add(
        ManagedKnowledgeRevision(
            knowledge_base_id=knowledge_base_id,
            uid="user-1",
            knowledge_id=item.id,
            version=1,
            operation=ManagedKnowledgeRevisionOperation.CREATE,
            before_snapshot=None,
            after_snapshot=build_managed_knowledge_snapshot(item),
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            source_reference={"source": knowledge_key},
            modified_by=ManagedKnowledgeActorType.LLM,
        )
    )
    await db.commit()
    await db.refresh(item)
    return item


async def _create_running_organization_job(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    suffix: str,
    operation: KnowledgeJobOperation = KnowledgeJobOperation.MANUAL_ORGANIZE,
    owner: str = "knowledge_organization_publication-parent-worker",
) -> KnowledgeJob:
    available_at = await get_database_time(db)
    job, created = await knowledge_job_crud.create(
        db,
        uid="user-1",
        operation=operation,
        dedupe_key=f"knowledge_organization_publication-parent:{knowledge_base_id}:{suffix}",
        request_hash=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        active_change_key=f"kb-organization:{knowledge_base_id}",
        status=KnowledgeJobStatus.PENDING,
        knowledge_base_id=knowledge_base_id,
        payload={"source": "knowledge_organization_publication"},
        available_at=available_at,
        max_attempts=1,
        commit=False,
    )
    assert created is True
    assert job.id is not None
    claimed = await knowledge_job_crud.try_claim(
        db,
        uid="user-1",
        job_id=job.id,
        owner=owner,
        lease_seconds=60,
        enabled_operations=(operation,),
        commit=False,
    )
    assert claimed is not None
    await db.commit()
    return claimed


async def _create_pending_organization_job(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    suffix: str,
) -> KnowledgeJob:
    available_at = await get_database_time(db)
    job, created = await knowledge_job_crud.create(
        db,
        uid="user-1",
        operation=KnowledgeJobOperation.MANUAL_ORGANIZE,
        dedupe_key=f"knowledge_organization_publication-pending:{knowledge_base_id}:{suffix}",
        request_hash=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        active_change_key=f"kb-organization:{knowledge_base_id}",
        status=KnowledgeJobStatus.PENDING,
        knowledge_base_id=knowledge_base_id,
        payload={"source": "knowledge_organization_publication"},
        available_at=available_at,
        max_attempts=1,
    )
    assert created is True
    assert job.id is not None
    return job


async def _mark_organization_job_terminal(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
    owner: str,
    status: KnowledgeJobStatus,
    error: str | None = None,
) -> bool:
    if status == KnowledgeJobStatus.SUCCEEDED:
        changed = await knowledge_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            commit=False,
        )
    elif status == KnowledgeJobStatus.FAILED:
        changed = await knowledge_job_crud.mark_failed(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            error=error,
            commit=False,
        )
    elif status == KnowledgeJobStatus.CANCELLED:
        changed = await knowledge_job_crud.mark_cancelled(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            commit=False,
        )
    else:
        raise ValueError(f"unsupported terminal status: {status}")

    if changed:
        await coordinate_organization_terminal(db, uid, job_id, error=error, commit=False)
    await db.commit()
    return changed


async def _request_organization_cancel(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
):
    cancellation = await knowledge_job_crud.request_cancel(
        db,
        uid=uid,
        job_id=job_id,
        commit=False,
    )
    if cancellation.changed:
        await coordinate_organization_cancel_request(db, uid, job_id, commit=False)
        await coordinate_organization_terminal(db, uid, job_id, commit=False)
    await db.commit()
    return cancellation


def _full_plan(knowledge_ids: list[int]) -> KnowledgeOrganizationPlan:
    keep_id, update_id, merge_primary_id, merge_secondary_id, conflict_id, conflict_peer_id = knowledge_ids
    return KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "keep",
                    "source": {"knowledge_id": keep_id, "expected_version": 1},
                    "summary": "keep source",
                },
                {
                    "action": "update",
                    "source": {"knowledge_id": update_id, "expected_version": 1},
                    "target": {"knowledge_key": "updated-topic", "content": "updated organization content"},
                    "summary": "update source",
                },
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": merge_primary_id, "expected_version": 1},
                        {"knowledge_id": merge_secondary_id, "expected_version": 1},
                    ],
                    "primary_knowledge_id": merge_primary_id,
                    "target": {"knowledge_key": "merged-topic", "content": "merged organization content"},
                    "summary": "merge sources",
                },
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": conflict_id, "expected_version": 1},
                        {"knowledge_id": conflict_peer_id, "expected_version": 1},
                    ],
                    "reason": "sources need review",
                    "summary": "conflicting sources",
                },
            ]
        }
    )


async def _complete_stage(
    db: AsyncSession,
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    plan: KnowledgeOrganizationPlan,
    stage_key: str = "stage-15-final",
) -> KnowledgeOrganizationStage:
    work_key = hashlib.sha256(f"knowledge_organization_publication:{snapshot.id}:{stage_key}".encode()).hexdigest()
    model_key = hashlib.sha256(b"knowledge_organization_publication-model").hexdigest()
    stage, created = await knowledge_organization_stage_crud.create_stage(
        db,
        stage=KnowledgeOrganizationStage(
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            snapshot_id=snapshot.id,
            work_key=work_key,
            snapshot_key=snapshot.snapshot_key,
            stage_key=stage_key,
            stage_index=0,
            lower_stage_key=None,
            model_key=model_key,
            model_snapshot={
                "execution_model": {
                    "channel_id": 1,
                    "model_id": "knowledge_organization_publication-model",
                    "protocol": "openai",
                }
            },
            expected_fragment_count=1,
        ),
    )
    assert created is True
    fragment, fragment_created = await knowledge_organization_fragment_crud.write_ordered(
        db,
        fragment=KnowledgeOrganizationFragment(
            dedupe_key=knowledge_organization_fragment_crud.build_dedupe_key(
                work_key=stage.work_key,
                stage_key=stage.stage_key,
                model_key=stage.model_key,
                fragment_index=0,
            ),
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            snapshot_id=snapshot.id,
            stage_id=stage.id,
            work_key=stage.work_key,
            snapshot_key=snapshot.snapshot_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
            fragment_index=0,
            candidate_scope={"knowledge_ids": [1]},
            result=plan.model_dump(mode="json"),
            status=KnowledgeOrganizationFragmentStatus.COMPLETED,
        ),
    )
    assert fragment is not None and fragment_created is True
    stage = await knowledge_organization_stage_crud.get_by_id(db, stage_id=stage.id)
    assert stage is not None
    assert (
        await knowledge_organization_stage_crud.mark_completed(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )
        is True
    )
    persisted = await knowledge_organization_stage_crud.get_by_id(db, stage_id=stage.id)
    assert persisted is not None
    return persisted


async def _seed_items(db: AsyncSession, *, knowledge_base_id: int, count: int) -> list[ManagedKnowledgeItem]:
    return [
        await _add_item(
            db,
            knowledge_base_id=knowledge_base_id,
            knowledge_key=f"topic-{index}",
            content=f"organization content {index}",
        )
        for index in range(count)
    ]


def _zero_progress() -> dict[str, int]:
    return {
        "stage_count": 0,
        "completed_stage_count": 0,
        "running_stage_count": 0,
        "failed_stage_count": 0,
        "invalidated_stage_count": 0,
        "expected_fragment_count": 0,
        "succeeded_fragment_count": 0,
        "completed_fragment_count": 0,
        "invalidated_fragment_count": 0,
    }
