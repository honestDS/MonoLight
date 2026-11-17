from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.knowledge.managed import build_managed_knowledge_snapshot
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.organization_publication import publish_knowledge_organization_plan
from app.core.knowledge.organization_run import submit_auto_knowledge_organization
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from app.models.knowledge_base import (
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)
from app.providers.database.time import get_database_time
from tests.integration.knowledge.knowledge_publication_support import (
    _complete_stage,
    _create_managed_container,
    _create_running_organization_job,
    _full_plan,
    _seed_items,
)
from tests.integration.knowledge.knowledge_publication_support import _session_factory as _session_factory


@pytest.mark.asyncio
async def test_publication_uses_bounded_snapshot_and_current_item_pages(session_factory, monkeypatch):
    import app.core.knowledge.organization_publication as publication_module

    owner = "knowledge_organization_publication-publication-bounded-pages"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="bounded-pages")
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=6)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="bounded-pages",
            owner=owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = _full_plan([item.id for item in items])
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key="stage-15-bounded-pages")
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    snapshot_page_calls: list[tuple[int, int]] = []
    current_item_calls: list[tuple[int, ...]] = []
    original_list_with_revision_page = publication_module.knowledge_organization_snapshot_item_crud.list_with_revision_page
    original_get_by_ids = publication_module.managed_knowledge_item_crud.get_by_ids

    async def recording_list_with_revision_page(*args, **kwargs):
        snapshot_page_calls.append((kwargs["after_sequence"], kwargs["limit"]))
        return await original_list_with_revision_page(*args, **kwargs)

    async def recording_get_by_ids(*args, **kwargs):
        current_item_calls.append(tuple(kwargs["knowledge_ids"]))
        return await original_get_by_ids(*args, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "_SNAPSHOT_PAGE_SIZE",
        2,
    )
    monkeypatch.setattr(
        publication_module.knowledge_organization_snapshot_item_crud,
        "list_with_revision_page",
        recording_list_with_revision_page,
    )
    monkeypatch.setattr(
        publication_module.managed_knowledge_item_crud,
        "get_by_ids",
        recording_get_by_ids,
    )

    result = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        organization_job_id=parent_id,
        owner=owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )

    assert snapshot_page_calls == [(-1, 2), (1, 2), (3, 2), (5, 1)]
    assert len(current_item_calls) == 3
    assert all(len(knowledge_ids) <= 2 for knowledge_ids in current_item_calls)
    assert len(result.mutation_job_ids) == 2

    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)

    assert len(children) == 2
    assert {child.operation for child in children} == {KnowledgeJobOperation.ORGANIZE_MUTATION}
    assert {child.status for child in children} == {KnowledgeJobStatus.PENDING}
    assert {child.id for child in children} == set(result.mutation_job_ids)


@pytest.mark.asyncio
async def test_publication_uses_default_pages_for_401_item_keep_plan(session_factory, monkeypatch):
    import app.core.knowledge.organization_publication as publication_module

    owner = "knowledge_organization_publication-publication-default-large-pages"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="default-large-pages")
        assert knowledge_base.id is not None
        items = [
            ManagedKnowledgeItem(
                knowledge_base_id=knowledge_base.id,
                uid="user-1",
                knowledge_key=f"large-topic-{index}",
                content=f"large organization content {index}",
                content_token_count=4,
                content_hash=hashlib.sha256(f"large organization content {index}".encode()).hexdigest(),
                version=1,
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                source_reference={"source": f"large-topic-{index}"},
                created_by=ManagedKnowledgeActorType.LLM,
                last_modified_by=ManagedKnowledgeActorType.LLM,
                llm_maintainable=True,
                indexed_version=1,
                vector_item_ids=[f"vector-large-topic-{index}"],
                is_recallable=True,
                pending_job_id=None,
            )
            for index in range(401)
        ]
        db.add_all(items)
        await db.flush()
        assert all(item.id is not None for item in items)
        revisions = [
            ManagedKnowledgeRevision(
                knowledge_base_id=knowledge_base.id,
                uid="user-1",
                knowledge_id=item.id,
                version=1,
                operation=ManagedKnowledgeRevisionOperation.CREATE,
                before_snapshot=None,
                after_snapshot=build_managed_knowledge_snapshot(item),
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                source_reference=item.source_reference,
                modified_by=ManagedKnowledgeActorType.LLM,
            )
            for item in items
        ]
        db.add_all(revisions)
        await db.commit()

        knowledge_ids = [item.id for item in items if item.id is not None]
        assert len(knowledge_ids) == 401
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="default-large-pages",
            owner=owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "keep",
                        "source": {"knowledge_id": knowledge_id, "expected_version": 1},
                        "summary": "keep large page item",
                    }
                    for knowledge_id in knowledge_ids
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-default-large-pages",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    snapshot_page_calls: list[tuple[int, int]] = []
    current_item_calls: list[tuple[int, ...]] = []
    original_list_with_revision_page = publication_module.knowledge_organization_snapshot_item_crud.list_with_revision_page
    original_get_by_ids = publication_module.managed_knowledge_item_crud.get_by_ids

    async def recording_list_with_revision_page(*args, **kwargs):
        snapshot_page_calls.append((kwargs["after_sequence"], kwargs["limit"]))
        return await original_list_with_revision_page(*args, **kwargs)

    async def recording_get_by_ids(*args, **kwargs):
        current_item_calls.append(tuple(kwargs["knowledge_ids"]))
        return await original_get_by_ids(*args, **kwargs)

    monkeypatch.setattr(
        publication_module.knowledge_organization_snapshot_item_crud,
        "list_with_revision_page",
        recording_list_with_revision_page,
    )
    monkeypatch.setattr(
        publication_module.managed_knowledge_item_crud,
        "get_by_ids",
        recording_get_by_ids,
    )

    result = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        organization_job_id=parent_id,
        owner=owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )

    assert result.mutation_job_ids == ()
    assert snapshot_page_calls == [(-1, 200), (199, 200), (399, 1), (400, 1)]
    assert [len(knowledge_ids) for knowledge_ids in current_item_calls] == [200, 200, 1]
    assert all(len(knowledge_ids) <= 200 for knowledge_ids in current_item_calls)
    validated_ids = [knowledge_id for batch in current_item_calls for knowledge_id in batch]
    assert len(validated_ids) == 401
    assert len(set(validated_ids)) == 401
    assert set(validated_ids) == set(knowledge_ids)

    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)

    assert children == []


@pytest.mark.asyncio
async def test_auto_organization_counts_candidates_without_loading_full_list(session_factory, monkeypatch):
    import app.core.knowledge.organization_run as organization_run

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="auto-count-only")
        organization_last_run_at = await get_database_time(db) - timedelta(seconds=1)
        knowledge_base.organization_last_run_at = organization_last_run_at
        await db.commit()
        await _seed_items(db, knowledge_base_id=knowledge_base.id, count=5)

    count_calls: list[tuple[bool, object | None]] = []
    original_count = organization_run.managed_knowledge_item_crud.count_organization_candidates

    async def recording_count(*args, **kwargs):
        count_calls.append(("updated_after" in kwargs, kwargs.get("updated_after")))
        return await original_count(*args, **kwargs)

    monkeypatch.setattr(organization_run.managed_knowledge_item_crud, "count_organization_candidates", recording_count)

    async with session_factory() as db:
        job = await submit_auto_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )

    assert job is not None
    assert job.operation == KnowledgeJobOperation.AUTO_ORGANIZE
    assert count_calls == [(False, None), (True, organization_last_run_at)]
