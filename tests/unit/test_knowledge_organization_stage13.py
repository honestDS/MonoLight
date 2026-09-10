from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from app.core.crud.knowledge.organization import knowledge_organization_fragment_crud, knowledge_organization_stage_crud
from app.core.knowledge.managed import build_managed_knowledge_snapshot
from app.core.knowledge.organization import (
    build_knowledge_organization_work_identity,
    create_knowledge_organization_snapshot,
    load_knowledge_organization_snapshot_items,
)
from app.core.utils.time import get_local_time
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseType,
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationFragmentStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationStage,
    KnowledgeOrganizationStageStatus,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from scripts import migration_20260910_add_knowledge_organization_stage as organization_migration

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeOrganizationSnapshot.__table__,
    KnowledgeOrganizationStage.__table__,
    KnowledgeOrganizationFragment.__table__,
)


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-organization.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _create_managed_container(db: AsyncSession) -> KnowledgeBase:
    channel = ModelChannel(
        name="organization-embedding",
        api_key="test-key",
        base_url="https://example.invalid",
        model_ids=[],
    )
    db.add(channel)
    await db.flush()

    prompt = PromptLibrary(uid="user-1", name="organization-prompt", content="prompt")
    db.add(prompt)
    await db.flush()

    profile = Profile(uid="user-1", name="organization-profile", prompt_id=prompt.id, configs={})
    db.add(profile)
    await db.flush()

    knowledge_base = KnowledgeBase(
        uid="user-1",
        name="managed-organization",
        embedding_channel_id=channel.id,
        embedding_model_id="embedding-model",
        embedding_dimensions=1536,
        collection_name="managed-organization-collection",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=channel.id,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=1536,
        active_embedding_signature="embedding-signature",
        active_embedding_revision=7,
        active_collection_name="managed-organization-active",
        index_revision=11,
    )
    db.add(knowledge_base)
    await db.commit()
    await db.refresh(knowledge_base)
    return knowledge_base


async def _add_managed_item(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    knowledge_key: str,
    content: str,
    llm_maintainable: bool,
    is_recallable: bool = True,
    version: int = 1,
) -> tuple[ManagedKnowledgeItem, ManagedKnowledgeRevision]:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    item = ManagedKnowledgeItem(
        knowledge_base_id=knowledge_base_id,
        uid="user-1",
        knowledge_key=knowledge_key,
        content=content,
        content_token_count=max(1, len(content.split())),
        content_hash=content_hash,
        version=version,
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        source_reference={"source": knowledge_key},
        created_by=ManagedKnowledgeActorType.LLM,
        last_modified_by=ManagedKnowledgeActorType.LLM,
        llm_maintainable=llm_maintainable,
        indexed_version=version if is_recallable else 0,
        vector_item_ids=[f"vector-{knowledge_key}"] if is_recallable else [],
        is_recallable=is_recallable,
        pending_job_id=None,
    )
    db.add(item)
    await db.flush()
    revision = ManagedKnowledgeRevision(
        knowledge_base_id=knowledge_base_id,
        uid="user-1",
        knowledge_id=item.id,
        version=version,
        operation=ManagedKnowledgeRevisionOperation.CREATE if version == 1 else ManagedKnowledgeRevisionOperation.UPDATE,
        before_snapshot=None,
        after_snapshot=build_managed_knowledge_snapshot(item),
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        source_reference={"source": knowledge_key},
        modified_by=ManagedKnowledgeActorType.LLM,
    )
    db.add(revision)
    await db.commit()
    await db.refresh(item)
    await db.refresh(revision)
    return item, revision


async def _build_snapshot(db: AsyncSession) -> KnowledgeOrganizationSnapshot:
    knowledge_base = await _create_managed_container(db)
    await _add_managed_item(
        db,
        knowledge_base_id=knowledge_base.id,
        knowledge_key="included",
        content="published maintainable knowledge",
        llm_maintainable=True,
    )
    await _add_managed_item(
        db,
        knowledge_base_id=knowledge_base.id,
        knowledge_key="locked",
        content="user locked knowledge",
        llm_maintainable=False,
    )
    await _add_managed_item(
        db,
        knowledge_base_id=knowledge_base.id,
        knowledge_key="unpublished",
        content="unpublished knowledge",
        llm_maintainable=True,
        is_recallable=False,
    )
    return await create_knowledge_organization_snapshot(
        db,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
    )


def _make_stage(
    snapshot: KnowledgeOrganizationSnapshot,
    *,
    expected_fragment_count: int = 3,
    stage_key: str = "stage-0",
    model_snapshot: dict | None = None,
) -> KnowledgeOrganizationStage:
    frozen_model = model_snapshot or {
        "channel_id": 9,
        "model_id": "organization-model",
        "context_window_tokens": 128000,
        "max_output_tokens": 4096,
    }
    work_key, model_key = build_knowledge_organization_work_identity(
        snapshot_key=snapshot.snapshot_key,
        model_snapshot=frozen_model,
    )
    return KnowledgeOrganizationStage(
        uid=snapshot.uid,
        knowledge_base_id=snapshot.knowledge_base_id,
        snapshot_id=snapshot.id,
        work_key=work_key,
        snapshot_key=snapshot.snapshot_key,
        stage_key=stage_key,
        stage_index=0,
        lower_stage_key=None,
        model_key=model_key,
        model_snapshot=frozen_model,
        expected_fragment_count=expected_fragment_count,
    )


def _make_fragment(
    stage: KnowledgeOrganizationStage,
    *,
    fragment_index: int,
    snapshot_key: str | None = None,
    model_key: str | None = None,
) -> KnowledgeOrganizationFragment:
    return KnowledgeOrganizationFragment(
        dedupe_key=knowledge_organization_fragment_crud.build_dedupe_key(
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            model_key=model_key or stage.model_key,
            fragment_index=fragment_index,
        ),
        uid=stage.uid,
        knowledge_base_id=stage.knowledge_base_id,
        snapshot_id=stage.snapshot_id,
        stage_id=stage.id,
        work_key=stage.work_key,
        snapshot_key=snapshot_key or stage.snapshot_key,
        stage_key=stage.stage_key,
        model_key=model_key or stage.model_key,
        fragment_index=fragment_index,
        candidate_scope={"knowledge_ids": [fragment_index + 1]},
        result={"action": "keep", "fragment_index": fragment_index},
        status=KnowledgeOrganizationFragmentStatus.COMPLETED,
    )


@pytest.mark.asyncio
async def test_snapshot_only_contains_published_llm_maintainable_items_and_freezes_revision_content(session_factory):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        assert snapshot.item_count == 1
        assert snapshot.active_embedding_revision == 7
        assert snapshot.index_revision == 11
        assert snapshot.boundary_revision_id > 0
        assert len(snapshot.items) == 1
        snapshot_item = snapshot.items[0]
        assert snapshot_item["knowledge_key"] == "included"
        assert snapshot_item["llm_maintainable"] is True
        assert snapshot_item["content_reference"]["revision_id"] > 0

        duplicate_snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=snapshot.knowledge_base_id,
        )
        assert duplicate_snapshot.id == snapshot.id
        assert duplicate_snapshot.snapshot_key == snapshot.snapshot_key

        resolved_before = await load_knowledge_organization_snapshot_items(db, snapshot=snapshot)
        assert [item["content"] for item in resolved_before] == ["published maintainable knowledge"]

        current = (
            await db.execute(
                select(ManagedKnowledgeItem).where(
                    ManagedKnowledgeItem.id == snapshot_item["knowledge_id"],
                )
            )
        ).scalar_one()
        current.version = 2
        current.content = "new current content"
        current.content_hash = hashlib.sha256(current.content.encode("utf-8")).hexdigest()
        current.indexed_version = 2
        db.add(
            ManagedKnowledgeRevision(
                knowledge_base_id=current.knowledge_base_id,
                uid=current.uid,
                knowledge_id=current.id,
                version=2,
                operation=ManagedKnowledgeRevisionOperation.UPDATE,
                before_snapshot=None,
                after_snapshot=build_managed_knowledge_snapshot(current),
                source_type=current.source_type,
                source_reference=current.source_reference,
                modified_by=current.last_modified_by,
            )
        )
        await db.commit()

        resolved_after = await load_knowledge_organization_snapshot_items(db, snapshot=snapshot)
        assert resolved_after == resolved_before


@pytest.mark.asyncio
async def test_same_snapshot_and_model_generate_same_work_identity():
    first_model = {
        "channel_id": 1,
        "model_id": "organizer",
        "parameters": {"temperature": 0.2, "top_p": 0.9},
    }
    same_model_different_key_order = {
        "parameters": {"top_p": 0.9, "temperature": 0.2},
        "model_id": "organizer",
        "channel_id": 1,
    }

    assert build_knowledge_organization_work_identity(
        snapshot_key="a" * 64,
        model_snapshot=first_model,
    ) == build_knowledge_organization_work_identity(
        snapshot_key="a" * 64,
        model_snapshot=same_model_different_key_order,
    )
    assert build_knowledge_organization_work_identity(
        snapshot_key="a" * 64,
        model_snapshot=first_model,
    ) != build_knowledge_organization_work_identity(
        snapshot_key="b" * 64,
        model_snapshot=first_model,
    )


@pytest.mark.asyncio
async def test_concurrent_stage_creation_is_idempotent(session_factory):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        stage_values = _make_stage(snapshot).model_dump(exclude={"id", "created_at", "completed_at"})

    async def create_once() -> tuple[int, bool]:
        async with session_factory() as db:
            persisted, created = await knowledge_organization_stage_crud.create_stage(
                db,
                stage=KnowledgeOrganizationStage.model_validate(stage_values),
            )
            assert persisted.id is not None
            return persisted.id, created

    first, second = await asyncio.gather(create_once(), create_once())
    assert first[0] == second[0]
    assert sorted((first[1], second[1])) == [False, True]


@pytest.mark.asyncio
async def test_stage_identity_conflict_is_not_treated_as_idempotent(session_factory):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        stage, created = await knowledge_organization_stage_crud.create_stage(
            db,
            stage=_make_stage(snapshot, expected_fragment_count=2),
        )
        assert created is True

        conflicting = _make_stage(snapshot, expected_fragment_count=3)
        conflicting.work_key = stage.work_key
        conflicting.stage_key = stage.stage_key
        with pytest.raises(ValueError):
            await knowledge_organization_stage_crud.create_stage(db, stage=conflicting)


@pytest.mark.asyncio
async def test_fragment_duplicate_is_idempotent_only_when_result_is_identical(session_factory):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        stage, _ = await knowledge_organization_stage_crud.create_stage(
            db,
            stage=_make_stage(snapshot, expected_fragment_count=2),
        )
        first_fragment = _make_fragment(stage, fragment_index=0)
        first, first_created = await knowledge_organization_fragment_crud.write_ordered(db, fragment=first_fragment)
        assert first is not None and first_created is True

        duplicate, duplicate_created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(stage, fragment_index=0),
        )
        assert duplicate is not None and duplicate_created is False
        assert duplicate.id == first.id

        conflicting_fragment = _make_fragment(stage, fragment_index=0)
        conflicting_fragment.result = {"action": "update", "fragment_index": 0}
        conflict, conflict_created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=conflicting_fragment,
        )
        assert conflict is None
        assert conflict_created is False


@pytest.mark.asyncio
async def test_concurrent_fragment_conflict_is_not_treated_as_idempotent(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        stage, _ = await knowledge_organization_stage_crud.create_stage(
            db,
            stage=_make_stage(snapshot, expected_fragment_count=2),
        )
        first_fragment = _make_fragment(stage, fragment_index=0)
        conflicting_fragment = _make_fragment(stage, fragment_index=0)
        conflicting_fragment.result = {"action": "update", "fragment_index": 0}

    original_get = knowledge_organization_fragment_crud.get_by_dedupe_key
    initial_get_lock = asyncio.Lock()
    initial_get_ready = asyncio.Event()
    initial_get_count = 0

    async def synchronized_get(db, *, dedupe_key):
        nonlocal initial_get_count
        wait_for_peer = False
        async with initial_get_lock:
            if initial_get_count < 2:
                initial_get_count += 1
                wait_for_peer = True
                if initial_get_count == 2:
                    initial_get_ready.set()
        if wait_for_peer:
            await initial_get_ready.wait()
            return None
        return await original_get(db, dedupe_key=dedupe_key)

    monkeypatch.setattr(
        knowledge_organization_fragment_crud,
        "get_by_dedupe_key",
        synchronized_get,
    )

    async def write_once(fragment: KnowledgeOrganizationFragment):
        async with session_factory() as db:
            return await knowledge_organization_fragment_crud.write_ordered(
                db,
                fragment=fragment,
            )

    first_result, conflicting_result = await asyncio.gather(
        write_once(first_fragment),
        write_once(conflicting_fragment),
    )
    results = (first_result, conflicting_result)
    created = [result for result in results if result[1] is True]
    rejected = [result for result in results if result[1] is False]

    assert len(created) == 1
    assert created[0][0] is not None
    assert rejected == [(None, False)]


@pytest.mark.asyncio
async def test_stage_failure_and_invalidation_preserve_error_and_invalidate_completed_fragments(
    session_factory,
):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        stage, _ = await knowledge_organization_stage_crud.create_stage(
            db,
            stage=_make_stage(snapshot, expected_fragment_count=2),
        )
        persisted, created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(stage, fragment_index=0),
        )
        assert persisted is not None and created is True

        stage_identity = {
            "work_key": stage.work_key,
            "stage_key": stage.stage_key,
            "snapshot_key": stage.snapshot_key,
            "model_key": stage.model_key,
        }
        assert (
            await knowledge_organization_stage_crud.mark_failed(
                db,
                **stage_identity,
                error="organization model failed",
            )
            is True
        )

        failed = await knowledge_organization_stage_crud.get_by_identity(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
        )
        assert failed is not None
        assert failed.status == KnowledgeOrganizationStageStatus.FAILED
        assert failed.error == "organization model failed"
        assert (
            await knowledge_organization_stage_crud.get_resume_fragment_index(
                db,
                **stage_identity,
            )
            is None
        )

        assert (
            await knowledge_organization_stage_crud.invalidate(
                db,
                **stage_identity,
            )
            is True
        )

        invalidated = await knowledge_organization_stage_crud.get_by_identity(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
        )
        fragments = list(
            (
                await db.execute(
                    select(KnowledgeOrganizationFragment)
                    .where(
                        KnowledgeOrganizationFragment.stage_id == stage.id,
                    )
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert invalidated is not None
        assert invalidated.status == KnowledgeOrganizationStageStatus.INVALIDATED
        assert invalidated.error == "organization model failed"
        assert [fragment.status for fragment in fragments] == [
            KnowledgeOrganizationFragmentStatus.INVALIDATED,
        ]


@pytest.mark.asyncio
async def test_completed_prefix_is_recoverable_and_completion_requires_full_contiguous_identity(session_factory):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        stage, created = await knowledge_organization_stage_crud.create_stage(db, stage=_make_stage(snapshot))
        assert created is True
        stage_identity = {
            "work_key": stage.work_key,
            "stage_key": stage.stage_key,
            "snapshot_key": stage.snapshot_key,
            "model_key": stage.model_key,
        }

        first, first_created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(stage, fragment_index=0),
        )
        second, second_created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(stage, fragment_index=1),
        )
        assert first is not None and first_created is True
        assert second is not None and second_created is True
        assert (
            await knowledge_organization_stage_crud.mark_completed(
                db,
                **stage_identity,
            )
            is False
        )

    async with session_factory() as db:
        resumed = await knowledge_organization_stage_crud.get_resume_fragment_index(
            db,
            **stage_identity,
        )
        assert resumed == 2
        persisted_stage = await knowledge_organization_stage_crud.get_by_identity(
            db,
            work_key=stage_identity["work_key"],
            stage_key=stage_identity["stage_key"],
        )
        assert persisted_stage is not None

        wrong_identity, wrong_identity_created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(persisted_stage, fragment_index=2, model_key="f" * 64),
        )
        assert wrong_identity is None
        assert wrong_identity_created is False
        persisted_stage = await knowledge_organization_stage_crud.get_by_identity(
            db,
            work_key=stage_identity["work_key"],
            stage_key=stage_identity["stage_key"],
        )
        assert persisted_stage is not None

        third, third_created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(persisted_stage, fragment_index=2),
        )
        assert third is not None and third_created is True
        assert (
            await knowledge_organization_stage_crud.mark_completed(
                db,
                work_key=stage_identity["work_key"],
                stage_key=stage_identity["stage_key"],
                snapshot_key="0" * 64,
                model_key=stage_identity["model_key"],
            )
            is False
        )
        assert (
            await knowledge_organization_stage_crud.mark_completed(
                db,
                **stage_identity,
            )
            is True
        )
        completed = await knowledge_organization_stage_crud.get_by_identity(
            db,
            work_key=stage_identity["work_key"],
            stage_key=stage_identity["stage_key"],
        )
        assert completed is not None
        assert completed.status == KnowledgeOrganizationStageStatus.COMPLETED
        assert completed.succeeded_fragment_count == 3


@pytest.mark.asyncio
async def test_out_of_order_fragment_is_rejected_without_advancing_prefix(session_factory):
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        stage, _ = await knowledge_organization_stage_crud.create_stage(db, stage=_make_stage(snapshot))
        stage_identity = {
            "work_key": stage.work_key,
            "stage_key": stage.stage_key,
            "snapshot_key": stage.snapshot_key,
            "model_key": stage.model_key,
        }

        out_of_order, created = await knowledge_organization_fragment_crud.write_ordered(
            db,
            fragment=_make_fragment(stage, fragment_index=1),
        )
        assert out_of_order is None
        assert created is False
        assert (
            await knowledge_organization_stage_crud.get_resume_fragment_index(
                db,
                **stage_identity,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_expired_temporary_state_is_cleaned_in_bounded_batches(session_factory):
    old_time = get_local_time() - timedelta(days=2)
    cutoff = get_local_time() - timedelta(days=1)
    async with session_factory() as db:
        snapshot = await _build_snapshot(db)
        snapshot.created_at = old_time
        await db.commit()
        stage = _make_stage(snapshot, expected_fragment_count=2)
        stage.created_at = old_time
        stage, _ = await knowledge_organization_stage_crud.create_stage(db, stage=stage)
        for fragment_index in range(2):
            fragment = _make_fragment(stage, fragment_index=fragment_index)
            fragment.created_at = old_time
            persisted, created = await knowledge_organization_fragment_crud.write_ordered(db, fragment=fragment)
            assert persisted is not None and created is True

        for _ in range(8):
            deleted = await knowledge_organization_stage_crud.cleanup_expired(
                db,
                before=cutoff,
                batch_size=1,
            )
            assert deleted in {0, 1}
            if deleted == 0:
                break

        assert (await db.execute(select(KnowledgeOrganizationFragment))).scalars().all() == []
        assert (await db.execute(select(KnowledgeOrganizationStage))).scalars().all() == []
        assert (await db.execute(select(KnowledgeOrganizationSnapshot))).scalars().all() == []


@pytest.mark.asyncio
async def test_stage13_migration_creates_organization_tables(tmp_path: Path):
    database_path = tmp_path / "migration.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=(
                    PromptLibrary.__table__,
                    ModelChannel.__table__,
                    Profile.__table__,
                    KnowledgeBase.__table__,
                    ManagedKnowledgeItem.__table__,
                    ManagedKnowledgeRevision.__table__,
                ),
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            await organization_migration.migrate(db)
            await db.commit()
        async with engine.connect() as connection:
            table_names = set(await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_table_names()))
    finally:
        await engine.dispose()

    assert organization_migration.MIGRATION_ID == "20260910_add_knowledge_organization_stage_v1"
    assert {
        "knowledge_organization_snapshot",
        "knowledge_organization_stage",
        "knowledge_organization_fragment",
    } <= table_names
