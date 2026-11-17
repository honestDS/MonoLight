from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationExecutionResult,
    KnowledgeOrganizationPlan,
)
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseIndexStatus,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeItem,
)
from app.providers.database.time import get_database_time
from tests.integration.knowledge.knowledge_publication_support import (
    _add_item,
    _complete_stage,
    _create_managed_container,
    _create_running_organization_job,
)
from tests.integration.knowledge.knowledge_publication_support import _session_factory as _session_factory


@pytest.mark.asyncio
async def test_real_worker_publishes_update_and_completes_organization_parent(
    session_factory,
    monkeypatch,
):
    import app.core.knowledge.organization_run as organization_run

    parent_owner = "knowledge_organization_publication-real-worker-parent"
    mutation_owner = "knowledge_organization_publication-real-worker-mutation"
    update_owner = "knowledge_organization_publication-real-worker-update"
    target_key = "real-worker-updated-topic"
    target_content = "updated through the real knowledge worker path"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="real-worker")
        channel = await db.get(ModelChannel, knowledge_base.embedding_channel_id)
        assert channel is not None
        channel.model_ids = [
            {
                "model_id": "embedding-model",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 1536,
            }
        ]
        await db.commit()

        item = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-topic",
            content="original real worker content",
        )
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="real-worker",
            owner=parent_owner,
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
                        "action": "update",
                        "source": {"knowledge_id": item.id, "expected_version": 1},
                        "target": {"knowledge_key": target_key, "content": target_content},
                        "summary": "publish through the worker",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-real-worker",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        target_id = item.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    organization_run_calls: list[dict[str, int | str]] = []

    async def fake_organization_run(*_args, **kwargs):
        organization_run_calls.append(
            {
                "uid": kwargs["uid"],
                "knowledge_base_id": kwargs["knowledge_base_id"],
                "organization_job_id": kwargs["organization_job_id"],
            }
        )
        return KnowledgeOrganizationExecutionResult(
            plan=plan,
            model_id=final_stage.model_snapshot["execution_model"]["model_id"],
            stage_count=1,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            final_stage_key=final_stage.stage_key,
            work_key=final_stage.work_key,
        )

    embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
    upsert_calls: list[tuple[str, list[str], list[str], list[list[float]], list[dict]]] = []

    async def fake_get_or_create_collection(collection_name: str, **_kwargs):
        return {"name": collection_name}

    async def fake_embed(
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **kwargs,
    ) -> list[list[float]]:
        assert isinstance(config, EmbeddingRuntimeConfig)
        assert config.embedding_dimensions == 1536
        assert kwargs["dimensions"] == 1536
        embedding_calls.append((config, list(texts)))
        return [[0.1] * 1536 for _ in texts]

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        **_kwargs,
    ) -> int:
        upsert_calls.append(
            (
                collection_name,
                list(item_ids),
                list(documents),
                [list(vector) for vector in embeddings],
                [dict(metadata) for metadata in metadatas],
            )
        )
        return len(item_ids)

    monkeypatch.setattr(organization_run, "_execute_knowledge_organization_run", fake_organization_run)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)

    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    consumer = KnowledgeJobConsumer(
        executor,
        session_factory=session_factory,
        poll_interval_seconds=0.01,
    )

    async def wait_for_children(expected_count: int) -> list[KnowledgeJob]:
        async def poll() -> list[KnowledgeJob]:
            while True:
                async with session_factory() as db:
                    children = await knowledge_job_crud.list_children(
                        db,
                        uid="user-1",
                        parent_job_id=parent_id,
                    )
                if len(children) >= expected_count:
                    return children
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=2.0)

    parent_task = asyncio.create_task(consumer._execute(parent, parent_owner))
    try:
        children = await wait_for_children(1)
        assert organization_run_calls == [
            {
                "uid": "user-1",
                "knowledge_base_id": knowledge_base_id,
                "organization_job_id": parent_id,
            }
        ]
        assert len(children) == 1
        assert children[0].operation == KnowledgeJobOperation.ORGANIZE_MUTATION
        assert children[0].payload["snapshot_id"] == snapshot_id
        assert children[0].payload["stage_id"] == final_stage_id
        async with session_factory() as db:
            running_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        assert running_parent is not None and running_parent.status == KnowledgeJobStatus.RUNNING
        assert not parent_task.done()

        async with session_factory() as db:
            mutation_job = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=children[0].id,
            )
            assert mutation_job is not None
            mutation_job.available_at = await get_database_time(db)
            await db.flush()
            claimed_mutation = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=mutation_job.id,
                owner=mutation_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
                commit=False,
            )
            assert claimed_mutation is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_mutation, mutation_owner),
            timeout=2.0,
        )

        async with session_factory() as db:
            direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
            mutation_job = next(child for child in direct_children if child.operation == KnowledgeJobOperation.ORGANIZE_MUTATION)
            managed_update = next(child for child in direct_children if child.operation == KnowledgeJobOperation.MANAGED_UPDATE)
        assert mutation_job.status == KnowledgeJobStatus.SUCCEEDED
        assert managed_update.parent_job_id == parent_id
        assert managed_update.knowledge_id == target_id
        assert managed_update.expected_version == 2
        assert managed_update.status == KnowledgeJobStatus.PENDING

        async with session_factory() as db:
            managed_update = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=managed_update.id,
            )
            assert managed_update is not None
            managed_update.available_at = await get_database_time(db)
            await db.flush()
            claimed_update = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=managed_update.id,
                owner=update_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.MANAGED_UPDATE,),
                commit=False,
            )
            assert claimed_update is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_update, update_owner),
            timeout=2.0,
        )
        await asyncio.wait_for(parent_task, timeout=2.0)
    finally:
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        target = await db.get(ManagedKnowledgeItem, target_id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)
        all_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id))).scalars())

    assert final_parent is not None
    assert final_parent.status == KnowledgeJobStatus.SUCCEEDED
    assert final_parent.active_change_key is None
    assert final_parent.locked_by is None
    assert {(child.operation, child.status) for child in direct_children} == {
        (KnowledgeJobOperation.ORGANIZE_MUTATION, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_UPDATE, KnowledgeJobStatus.SUCCEEDED),
    }
    assert target is not None
    assert target.knowledge_key == target_key
    assert target.content == target_content
    assert target.version == 2
    assert target.indexed_version == target.version
    assert target.is_recallable is True
    assert target.pending_job_id is None
    assert all(item.organization_lock_token is None for item in all_items)
    assert current_knowledge_base is not None
    assert current_knowledge_base.index_status == KnowledgeBaseIndexStatus.READY
    assert current_knowledge_base.organization_last_job_id == parent_id
    assert current_knowledge_base.organization_last_run_at is not None
    assert current_knowledge_base.organization_error is None

    assert len(embedding_calls) == 1
    embedding_config, embedded_texts = embedding_calls[0]
    assert embedding_config.declared_dimensions == 1536
    assert embedded_texts == [target_content]
    assert len(upsert_calls) == 1
    collection_name, vector_ids, documents, embeddings, metadatas = upsert_calls[0]
    assert collection_name == "managed-knowledge_organization_publication-real-worker-active"
    assert vector_ids == target.vector_item_ids
    assert documents == [target_content]
    assert len(vector_ids) == len(embeddings) == len(metadatas)
    assert all(len(vector) == 1536 for vector in embeddings)


@pytest.mark.asyncio
async def test_real_worker_publishes_merge_with_secondary_identity_and_completes_organization_parent(
    session_factory,
    monkeypatch,
):
    import app.core.knowledge.organization_run as organization_run

    parent_owner = "knowledge_organization_publication-real-worker-merge-parent"
    mutation_owner = "knowledge_organization_publication-real-worker-merge-mutation"
    delete_owner = "knowledge_organization_publication-real-worker-merge-delete"
    update_owner = "knowledge_organization_publication-real-worker-merge-update"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="real-worker-merge")
        channel = await db.get(ModelChannel, knowledge_base.embedding_channel_id)
        assert channel is not None
        channel.model_ids = [
            {
                "model_id": "embedding-model",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 1536,
            }
        ]
        await db.commit()

        primary = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-merge-primary",
            content="original real worker merge primary content",
        )
        secondary = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-merge-secondary",
            content="original real worker merge secondary content",
        )
        secondary_key = secondary.knowledge_key
        assert secondary_key is not None
        target_key = secondary_key
        target_content = secondary.content
        primary_id = primary.id
        secondary_id = secondary.id
        assert primary_id is not None and secondary_id is not None
        primary_vector_item_ids = list(primary.vector_item_ids)
        secondary_vector_item_ids = list(secondary.vector_item_ids)

        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="real-worker-merge",
            owner=parent_owner,
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
                        "action": "merge",
                        "sources": [
                            {"knowledge_id": primary_id, "expected_version": 1},
                            {"knowledge_id": secondary_id, "expected_version": 1},
                        ],
                        "primary_knowledge_id": primary_id,
                        "target": {
                            "knowledge_key": target_key,
                            "content": target_content,
                        },
                        "summary": "publish merge through the real worker",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-real-worker-merge",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id
        collection_name = knowledge_base.active_collection_name
        assert collection_name is not None

    organization_run_calls: list[dict[str, int | str]] = []

    async def fake_organization_run(*_args, **kwargs):
        organization_run_calls.append(
            {
                "uid": kwargs["uid"],
                "knowledge_base_id": kwargs["knowledge_base_id"],
                "organization_job_id": kwargs["organization_job_id"],
            }
        )
        return KnowledgeOrganizationExecutionResult(
            plan=plan,
            model_id=final_stage.model_snapshot["execution_model"]["model_id"],
            stage_count=1,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            final_stage_key=final_stage.stage_key,
            work_key=final_stage.work_key,
        )

    embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
    upsert_calls: list[tuple[str, list[str], list[str], list[list[float]], list[dict]]] = []
    delete_calls: list[tuple[str, list[str]]] = []

    async def fake_get_or_create_collection(collection_name: str, **_kwargs):
        return {"name": collection_name}

    async def fake_embed(
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **kwargs,
    ) -> list[list[float]]:
        assert isinstance(config, EmbeddingRuntimeConfig)
        assert config.embedding_dimensions == 1536
        assert kwargs["dimensions"] == 1536
        embedding_calls.append((config, list(texts)))
        return [[0.1] * 1536 for _ in texts]

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        **_kwargs,
    ) -> int:
        upsert_calls.append(
            (
                collection_name,
                list(item_ids),
                list(documents),
                [list(vector) for vector in embeddings],
                [dict(metadata) for metadata in metadatas],
            )
        )
        return len(item_ids)

    async def fake_delete(collection_name: str, item_ids: list[str], **_kwargs) -> int:
        delete_calls.append((collection_name, list(item_ids)))
        return len(item_ids)

    monkeypatch.setattr(organization_run, "_execute_knowledge_organization_run", fake_organization_run)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_delete_collection_items", fake_delete)

    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    consumer = KnowledgeJobConsumer(
        executor,
        session_factory=session_factory,
        poll_interval_seconds=0.01,
    )

    async def wait_for_children(expected_count: int) -> list[KnowledgeJob]:
        async def poll() -> list[KnowledgeJob]:
            while True:
                async with session_factory() as db:
                    children = await knowledge_job_crud.list_children(
                        db,
                        uid="user-1",
                        parent_job_id=parent_id,
                    )
                if len(children) >= expected_count:
                    return children
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=2.0)

    async def execute_child(job_id: int, owner: str, operation: KnowledgeJobOperation) -> None:
        async with session_factory() as db:
            job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=job_id)
            assert job is not None
            job.available_at = await get_database_time(db)
            await db.flush()
            claimed = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=job_id,
                owner=owner,
                lease_seconds=60,
                enabled_operations=(operation,),
                commit=False,
            )
            assert claimed is not None
            await db.commit()
        await asyncio.wait_for(consumer._execute(claimed, owner), timeout=2.0)

    parent_task = asyncio.create_task(consumer._execute(parent, parent_owner))
    try:
        children = await wait_for_children(1)
        assert organization_run_calls == [
            {
                "uid": "user-1",
                "knowledge_base_id": knowledge_base_id,
                "organization_job_id": parent_id,
            }
        ]
        assert len(children) == 1
        mutation_job = children[0]
        assert mutation_job.operation == KnowledgeJobOperation.ORGANIZE_MUTATION
        assert mutation_job.status == KnowledgeJobStatus.PENDING
        assert mutation_job.payload["snapshot_id"] == snapshot_id
        assert mutation_job.payload["stage_id"] == final_stage_id
        assert not parent_task.done()

        await execute_child(mutation_job.id, mutation_owner, KnowledgeJobOperation.ORGANIZE_MUTATION)

        children = await wait_for_children(3)
        children_by_operation = {child.operation: child for child in children}
        assert len(children) == 3
        assert set(children_by_operation) == {
            KnowledgeJobOperation.ORGANIZE_MUTATION,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
            KnowledgeJobOperation.MANAGED_UPDATE,
        }
        assert children_by_operation[KnowledgeJobOperation.ORGANIZE_MUTATION].status == KnowledgeJobStatus.SUCCEEDED
        assert children_by_operation[KnowledgeJobOperation.MANAGED_DELETE_CLEANUP].status == KnowledgeJobStatus.PENDING
        assert children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE].status == KnowledgeJobStatus.PENDING

        await execute_child(
            children_by_operation[KnowledgeJobOperation.MANAGED_DELETE_CLEANUP].id,
            delete_owner,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
        )
        await execute_child(
            children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE].id,
            update_owner,
            KnowledgeJobOperation.MANAGED_UPDATE,
        )
        await asyncio.wait_for(parent_task, timeout=2.0)
    finally:
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        final_primary = await db.get(ManagedKnowledgeItem, primary_id)
        final_secondary = await db.get(ManagedKnowledgeItem, secondary_id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)
        all_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id).order_by(ManagedKnowledgeItem.id))).scalars())

    assert final_parent is not None
    assert final_parent.status == KnowledgeJobStatus.SUCCEEDED
    assert final_parent.active_change_key is None
    assert final_parent.locked_by is None
    assert final_parent.lock_until is None
    assert len(direct_children) == 3
    assert {(child.operation, child.status) for child in direct_children} == {
        (KnowledgeJobOperation.ORGANIZE_MUTATION, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_DELETE_CLEANUP, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_UPDATE, KnowledgeJobStatus.SUCCEEDED),
    }
    assert final_primary is not None
    assert final_primary.knowledge_key == target_key
    assert final_primary.content == target_content
    assert final_primary.version == 2
    assert final_primary.indexed_version == 2
    assert final_primary.is_recallable is True
    assert final_primary.pending_job_id is None
    assert final_primary.organization_lock_token is None
    assert final_primary.vector_item_ids != primary_vector_item_ids
    assert final_secondary is None
    assert all(item.organization_lock_token is None for item in all_items)
    assert current_knowledge_base is not None
    assert current_knowledge_base.index_status == KnowledgeBaseIndexStatus.READY
    assert current_knowledge_base.organization_last_job_id == parent_id
    assert current_knowledge_base.organization_last_run_at is not None
    assert current_knowledge_base.organization_error is None

    assert len(embedding_calls) == 1
    embedding_config, embedded_texts = embedding_calls[0]
    assert embedding_config.declared_dimensions == 1536
    assert embedded_texts == [target_content]
    assert len(upsert_calls) == 1
    upsert_collection, vector_ids, documents, embeddings, metadatas = upsert_calls[0]
    assert upsert_collection == collection_name
    assert vector_ids == final_primary.vector_item_ids
    assert vector_ids != primary_vector_item_ids
    assert documents == [target_content]
    assert len(vector_ids) == len(embeddings) == len(metadatas)
    assert all(len(vector) == 1536 for vector in embeddings)
    assert any(deleted_collection == collection_name and set(secondary_vector_item_ids).issubset(deleted_ids) for deleted_collection, deleted_ids in delete_calls)


@pytest.mark.asyncio
async def test_real_worker_failed_update_fails_parent_and_rolls_back_item(
    session_factory,
    monkeypatch,
):
    import app.core.knowledge.organization_run as organization_run

    parent_owner = "knowledge_organization_publication-real-worker-failure-parent"
    mutation_owner = "knowledge_organization_publication-real-worker-failure-mutation"
    update_owner = "knowledge_organization_publication-real-worker-failure-update"
    target_key = "real-worker-failure-updated-topic"
    target_content = "updated through the failing real knowledge worker path"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="real-worker-failure")
        channel = await db.get(ModelChannel, knowledge_base.embedding_channel_id)
        assert channel is not None
        channel.model_ids = [
            {
                "model_id": "embedding-model",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 1536,
            }
        ]
        await db.commit()

        item = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-failure-topic",
            content="original real worker failure content",
        )
        before_state = (
            item.knowledge_key,
            item.content,
            item.version,
            item.indexed_version,
            item.is_recallable,
            list(item.vector_item_ids),
        )
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="real-worker-failure",
            owner=parent_owner,
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
                        "action": "update",
                        "source": {"knowledge_id": item.id, "expected_version": 1},
                        "target": {"knowledge_key": target_key, "content": target_content},
                        "summary": "fail publication through the worker",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-real-worker-failure",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        target_id = item.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    organization_run_calls: list[dict[str, int | str]] = []

    async def fake_organization_run(*_args, **kwargs):
        organization_run_calls.append(
            {
                "uid": kwargs["uid"],
                "knowledge_base_id": kwargs["knowledge_base_id"],
                "organization_job_id": kwargs["organization_job_id"],
            }
        )
        return KnowledgeOrganizationExecutionResult(
            plan=plan,
            model_id=final_stage.model_snapshot["execution_model"]["model_id"],
            stage_count=1,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            final_stage_key=final_stage.stage_key,
            work_key=final_stage.work_key,
        )

    collection_calls: list[str] = []
    embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
    upsert_calls: list[tuple[str, list[str], list[str], list[list[float]], list[dict]]] = []

    async def fake_get_or_create_collection(collection_name: str, **_kwargs):
        collection_calls.append(collection_name)
        return {"name": collection_name}

    async def fake_embed(
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **kwargs,
    ) -> list[list[float]]:
        assert isinstance(config, EmbeddingRuntimeConfig)
        assert config.embedding_dimensions == 1536
        assert kwargs["dimensions"] == 1536
        embedding_calls.append((config, list(texts)))
        raise RuntimeError("embedding backend unavailable")

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        **_kwargs,
    ) -> int:
        upsert_calls.append(
            (
                collection_name,
                list(item_ids),
                list(documents),
                [list(vector) for vector in embeddings],
                [dict(metadata) for metadata in metadatas],
            )
        )
        return len(item_ids)

    monkeypatch.setattr(organization_run, "_execute_knowledge_organization_run", fake_organization_run)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)

    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    consumer = KnowledgeJobConsumer(
        executor,
        session_factory=session_factory,
        poll_interval_seconds=0.01,
    )

    async def wait_for_children(expected_count: int) -> list[KnowledgeJob]:
        async def poll() -> list[KnowledgeJob]:
            while True:
                async with session_factory() as db:
                    children = await knowledge_job_crud.list_children(
                        db,
                        uid="user-1",
                        parent_job_id=parent_id,
                    )
                if len(children) >= expected_count:
                    return children
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=2.0)

    parent_task = asyncio.create_task(consumer._execute(parent, parent_owner))
    try:
        children = await wait_for_children(1)
        assert organization_run_calls == [
            {
                "uid": "user-1",
                "knowledge_base_id": knowledge_base_id,
                "organization_job_id": parent_id,
            }
        ]
        assert len(children) == 1
        mutation_wrapper = children[0]
        assert mutation_wrapper.operation == KnowledgeJobOperation.ORGANIZE_MUTATION
        assert mutation_wrapper.status == KnowledgeJobStatus.PENDING
        assert mutation_wrapper.active_change_key is not None
        assert mutation_wrapper.payload["snapshot_id"] == snapshot_id
        assert mutation_wrapper.payload["stage_id"] == final_stage_id

        async with session_factory() as db:
            running_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
            assert running_parent is not None
            assert running_parent.status == KnowledgeJobStatus.RUNNING
            assert running_parent.active_change_key is not None

            mutation_job = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=mutation_wrapper.id,
            )
            assert mutation_job is not None
            mutation_job.available_at = await get_database_time(db)
            await db.flush()
            claimed_mutation = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=mutation_job.id,
                owner=mutation_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
                commit=False,
            )
            assert claimed_mutation is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_mutation, mutation_owner),
            timeout=2.0,
        )

        async with session_factory() as db:
            direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
            mutation_job = next(child for child in direct_children if child.operation == KnowledgeJobOperation.ORGANIZE_MUTATION)
            managed_update = next(child for child in direct_children if child.operation == KnowledgeJobOperation.MANAGED_UPDATE)
        assert mutation_job.status == KnowledgeJobStatus.SUCCEEDED
        assert mutation_job.active_change_key is None
        assert mutation_job.result is not None
        assert mutation_job.result["mutation_job_ids"] == [managed_update.id]
        assert managed_update.status == KnowledgeJobStatus.PENDING
        assert managed_update.active_change_key is not None
        assert not parent_task.done()

        async with session_factory() as db:
            managed_update = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=managed_update.id,
            )
            assert managed_update is not None
            managed_update.max_attempts = 1
            managed_update.available_at = await get_database_time(db)
            await db.flush()
            claimed_update = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=managed_update.id,
                owner=update_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.MANAGED_UPDATE,),
                commit=False,
            )
            assert claimed_update is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_update, update_owner),
            timeout=2.0,
        )
        await asyncio.wait_for(parent_task, timeout=2.0)
    finally:
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        target = await db.get(ManagedKnowledgeItem, target_id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)

    assert final_parent is not None
    assert final_parent.status == KnowledgeJobStatus.FAILED
    assert final_parent.active_change_key is None
    assert final_parent.locked_by is None
    assert final_parent.lock_until is None
    children_by_operation = {child.operation: child for child in direct_children}
    assert {(child.operation, child.status) for child in direct_children} == {
        (KnowledgeJobOperation.ORGANIZE_MUTATION, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_UPDATE, KnowledgeJobStatus.FAILED),
    }
    assert children_by_operation[KnowledgeJobOperation.ORGANIZE_MUTATION].active_change_key is None
    failed_update = children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE]
    assert failed_update.active_change_key is None
    assert failed_update.error
    assert failed_update.finished_at is not None
    assert target is not None
    assert (
        target.knowledge_key,
        target.content,
        target.version,
        target.indexed_version,
        target.is_recallable,
        list(target.vector_item_ids),
    ) == before_state
    assert target.pending_job_id is None
    assert target.organization_lock_token is None
    assert current_knowledge_base is not None
    assert current_knowledge_base.organization_error is not None

    assert collection_calls == [current_knowledge_base.active_collection_name]
    assert len(embedding_calls) == 1
    embedding_config, embedded_texts = embedding_calls[0]
    assert embedding_config.declared_dimensions == 1536
    assert embedded_texts == [target_content]
    assert upsert_calls == []
