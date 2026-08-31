from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.profile.profile import profile_crud
from app.core.dispatch_context import build_dispatch_context
from app.core.knowledge.deletion import delete_owned_knowledge_base
from app.core.knowledge.errors import ManagedKnowledgeContainerConflictError
from app.core.knowledge.managed import managed_knowledge_service
from app.core.knowledge.managed_container import get_or_create_managed_knowledge_base
from app.core.knowledge_jobs import manager as knowledge_job_manager_module
from app.core.tools.longterm_memory import (
    MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA,
    LongTermMemoryExecutor,
)
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobStatus,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeSourceType,
)
from app.models.memory import LongTermMemoryStore
from app.models.profile import Profile
from app.models.prompt import PromptLibrary

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    LongTermMemoryStore.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseCollectionOwner.__table__,
    KnowledgeBaseProfileBinding.__table__,
    KnowledgeBaseDocument.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeJob.__table__,
)


@pytest_asyncio.fixture
async def stage6_database(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "managed-knowledge-tool-stage6.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _create_runtime(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Profile, ModelChannel]:
    async with session_factory() as db:
        channel = ModelChannel(
            name="stage6-embedding",
            api_key="test-api-key",
            base_url="https://embedding.invalid/v1",
            is_active=True,
            model_ids=[
                {
                    "model_id": "embedding-model",
                    "usage": "EMBEDDING",
                    "protocol": "OPENAI_EMBEDDING",
                    "embedding_dimensions": 3,
                    "is_enabled": True,
                }
            ],
        )
        db.add(channel)
        await db.flush()

        profile = Profile(
            name="Stage 6 Profile",
            uid="user-1",
            configs={"memory": {"enabled": True}},
        )
        db.add(profile)
        await db.flush()

        db.add(
            LongTermMemoryStore(
                uid="user-1",
                active_embedding_channel_id=channel.id,
                active_embedding_model_id="embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="embedding-signature-v7",
                active_embedding_revision=7,
                active_collection_name="memory-active-v7",
                index_revision=4,
            )
        )
        await db.commit()
        await db.refresh(profile)
        await db.refresh(channel)
        return profile, channel


def _executor(
    db: AsyncSession,
    profile: Profile,
    *,
    tool_call_id: str,
    source_message_id: int = 101,
) -> LongTermMemoryExecutor:
    context = build_dispatch_context(
        mode="interactive",
        source="interactive_tool",
        uid="user-1",
        session_id="session-stage6",
        profile=profile,
        db=db,
        tool_call_id=tool_call_id,
        source_message_id=source_message_id,
    )
    executor = LongTermMemoryExecutor(project_root=".", uid=context.uid)
    executor.set_config(
        SimpleNamespace(
            memory=SimpleNamespace(
                enabled=True,
                top_k=5,
                candidate_k=8,
                result_max_chars=2000,
            )
        )
    )
    executor.set_runtime_context(dispatch_context=context)
    return executor


async def _create_direct_item(
    db: AsyncSession,
    *,
    profile: Profile,
    knowledge_key: str,
    content: str,
    actor: ManagedKnowledgeActorType = ManagedKnowledgeActorType.LLM,
):
    container = await get_or_create_managed_knowledge_base(
        db,
        uid="user-1",
        profile_id=profile.id,
    )
    result = await managed_knowledge_service.create(
        db,
        uid="user-1",
        knowledge_base_id=container.knowledge_base.id,
        knowledge_key=knowledge_key,
        content=content,
        source_type=(ManagedKnowledgeSourceType.USER_API if actor == ManagedKnowledgeActorType.USER else ManagedKnowledgeSourceType.LLM_TOOL),
        actor=actor,
    )
    assert result.item is not None
    return container.knowledge_base, result.item


def test_step6_schema_has_no_knowledge_base_creation_or_user_document_target():
    properties = MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA["function"]["parameters"]["properties"]
    operations = MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"]

    assert "knowledge_create" in operations
    assert "knowledge_update" in operations
    assert "knowledge_delete" in operations
    assert "create_knowledge_base" not in operations
    assert "knowledge_base_create" not in operations
    assert "knowledge_base_id" not in properties
    assert "document_id" not in properties


@pytest.mark.asyncio
async def test_step6_first_knowledge_create_lazily_creates_managed_container_and_safe_job(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, channel = await _create_runtime(stage6_database)
    knowledge_content = "The billing service uses idempotency keys for payment retries."

    async with stage6_database() as db:
        executor = _executor(db, profile, tool_call_id="call-knowledge-create")
        payload = json.loads(
            await executor.execute(
                operation="knowledge_create",
                knowledge_key="billing.idempotency",
                knowledge_content=knowledge_content,
            )
        )

    assert payload["status"] == "accepted"
    assert payload["mutation_status"] == "created"
    assert payload["knowledge_base_created"] is True
    assert payload["knowledge_id"] > 0
    assert payload["current_version"] == 1
    assert knowledge_content not in json.dumps(payload, ensure_ascii=False)

    async with stage6_database() as db:
        knowledge_bases = list(
            (
                await db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.uid == "user-1",
                        KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(knowledge_bases) == 1
        knowledge_base = knowledge_bases[0]
        assert knowledge_base.managed_profile_id == profile.id
        assert knowledge_base.active_embedding_channel_id == channel.id
        assert knowledge_base.active_collection_name != "memory-active-v7"

        binding = await db.scalar(
            select(KnowledgeBaseProfileBinding).where(
                KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base.id,
                KnowledgeBaseProfileBinding.profile_id == profile.id,
                KnowledgeBaseProfileBinding.uid == "user-1",
            )
        )
        assert binding is not None

        item = await db.get(ManagedKnowledgeItem, payload["knowledge_id"])
        assert item is not None
        assert item.knowledge_base_id == knowledge_base.id
        assert item.content == knowledge_content
        assert item.source_type == ManagedKnowledgeSourceType.LLM_TOOL
        assert item.created_by == ManagedKnowledgeActorType.LLM
        assert item.last_modified_by == ManagedKnowledgeActorType.LLM
        assert item.llm_maintainable is True

        job = await db.get(KnowledgeJob, payload["job_id"])
        assert job is not None
        assert job.source_profile_id == profile.id
        assert job.source_session_id == "session-stage6"
        assert job.source_message_id == 101
        assert knowledge_content not in json.dumps(job.payload, ensure_ascii=False)
        assert set(job.payload) == {
            "content_length",
            "content_hash",
            "knowledge_key_hash",
        }


@pytest.mark.asyncio
async def test_step6_managed_update_and_delete_use_current_profile_container(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        _knowledge_base, update_item = await _create_direct_item(
            db,
            profile=profile,
            knowledge_key="api.contract",
            content="The API uses version one.",
        )
        _knowledge_base, delete_item = await _create_direct_item(
            db,
            profile=profile,
            knowledge_key="api.legacy",
            content="The legacy endpoint is supported.",
        )

        update_executor = _executor(db, profile, tool_call_id="call-knowledge-update")
        update_payload = json.loads(
            await update_executor.execute(
                operation="knowledge_update",
                knowledge_id=update_item.id,
                knowledge_expected_version=1,
                knowledge_content="The API uses version two.",
            )
        )
        assert update_payload["status"] == "accepted"
        assert update_payload["mutation_status"] == "updated"
        assert update_payload["knowledge_id"] == update_item.id
        assert update_payload["current_version"] == 2

        delete_executor = _executor(db, profile, tool_call_id="call-knowledge-delete")
        delete_payload = json.loads(
            await delete_executor.execute(
                operation="knowledge_delete",
                knowledge_id=delete_item.id,
                knowledge_expected_version=1,
            )
        )
        assert delete_payload["status"] == "accepted"
        assert delete_payload["mutation_status"] == "deleted"
        assert delete_payload["knowledge_id"] == delete_item.id
        assert delete_payload["current_version"] == 2

    async with stage6_database() as db:
        updated = await db.get(ManagedKnowledgeItem, update_item.id)
        deleted = await db.get(ManagedKnowledgeItem, delete_item.id)
        assert updated is not None
        assert updated.content == "The API uses version two."
        assert updated.knowledge_key == "api.contract"
        assert updated.version == 2
        assert updated.last_modified_by == ManagedKnowledgeActorType.LLM
        assert deleted is not None
        assert deleted.deleted_at is not None
        assert deleted.is_recallable is False
        assert deleted.version == 2


@pytest.mark.asyncio
async def test_step6_managed_version_conflict_is_rejected_without_mutation(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        _knowledge_base, item = await _create_direct_item(
            db,
            profile=profile,
            knowledge_key="release.channel",
            content="Releases use the stable channel.",
        )
        item_id = item.id
        executor = _executor(db, profile, tool_call_id="call-version-conflict")
        payload = json.loads(
            await executor.execute(
                operation="knowledge_update",
                knowledge_id=item_id,
                knowledge_expected_version=2,
                knowledge_content="Releases use the preview channel.",
            )
        )

    assert payload["status"] == "failed"
    async with stage6_database() as db:
        current = await db.get(ManagedKnowledgeItem, item_id)
        assert current is not None
        assert current.version == 1
        assert current.content == "Releases use the stable channel."
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_step6_user_manual_edit_can_lock_item_against_llm_update_and_delete(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        knowledge_base, item = await _create_direct_item(
            db,
            profile=profile,
            knowledge_key="security.policy",
            content="The policy initially allows mode A.",
        )
        edited = await managed_knowledge_service.update(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=item.id,
            expected_version=1,
            knowledge_key="security.policy",
            content="The user requires mode B.",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
        )
        assert edited.item is not None
        assert edited.item.version == 2
        assert edited.item.llm_maintainable is False
        item_id = edited.item.id

        update_executor = _executor(db, profile, tool_call_id="call-locked-update")
        update_payload = json.loads(
            await update_executor.execute(
                operation="knowledge_update",
                knowledge_id=item_id,
                knowledge_expected_version=2,
                knowledge_content="The LLM tries to restore mode A.",
            )
        )
        delete_executor = _executor(db, profile, tool_call_id="call-locked-delete")
        delete_payload = json.loads(
            await delete_executor.execute(
                operation="knowledge_delete",
                knowledge_id=item_id,
                knowledge_expected_version=2,
            )
        )

    assert update_payload["status"] == "failed"
    assert delete_payload["status"] == "failed"
    async with stage6_database() as db:
        current = await db.get(ManagedKnowledgeItem, item_id)
        assert current is not None
        assert current.version == 2
        assert current.content == "The user requires mode B."
        assert current.deleted_at is None
        assert current.llm_maintainable is False
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_step6_user_knowledge_base_document_cannot_be_managed_mutation_target(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        user_knowledge_base = KnowledgeBase(
            uid="user-1",
            name="User KB",
            embedding_channel_id=channel.id,
            embedding_model_id="embedding-model",
            embedding_dimensions=3,
            collection_name="user-kb-collection",
            knowledge_base_type=KnowledgeBaseType.USER,
        )
        db.add(user_knowledge_base)
        await db.flush()
        db.add(
            KnowledgeBaseProfileBinding(
                uid="user-1",
                knowledge_base_id=user_knowledge_base.id,
                profile_id=profile.id,
            )
        )
        document = KnowledgeBaseDocument(
            knowledge_base_id=user_knowledge_base.id,
            filename="manual.md",
            content="User maintained source document.",
            chunk_size=1000,
            chunk_overlap=100,
            batch_size=16,
        )
        db.add(document)
        await db.commit()
        await db.refresh(document)
        document_id = document.id

        executor = _executor(db, profile, tool_call_id="call-user-kb-forbidden")
        payload = json.loads(
            await executor.execute(
                operation="knowledge_update",
                knowledge_id=document_id,
                knowledge_expected_version=1,
                knowledge_content="LLM must not overwrite this document.",
            )
        )

    assert payload["status"] == "failed"
    async with stage6_database() as db:
        stored_document = await db.get(KnowledgeBaseDocument, document_id)
        assert stored_document is not None
        assert stored_document.content == "User maintained source document."
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_step6_user_can_reenable_llm_maintenance_for_current_version(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        knowledge_base, item = await _create_direct_item(
            db,
            profile=profile,
            knowledge_key="operations.window",
            content="Maintenance runs on Tuesday.",
        )
        edited = await managed_knowledge_service.update(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=item.id,
            expected_version=1,
            knowledge_key="operations.window",
            content="Maintenance runs on Wednesday.",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
            llm_maintainable=True,
        )
        assert edited.item is not None
        assert edited.item.version == 2
        assert edited.item.llm_maintainable is True
        item_id = edited.item.id

        executor = _executor(db, profile, tool_call_id="call-reenabled-update")
        payload = json.loads(
            await executor.execute(
                operation="knowledge_update",
                knowledge_id=item_id,
                knowledge_expected_version=2,
                knowledge_content="Maintenance runs on Wednesday at 02:00 UTC.",
            )
        )

    assert payload["status"] == "accepted"
    assert payload["mutation_status"] == "updated"
    assert payload["current_version"] == 3
    async with stage6_database() as db:
        current = await db.get(ManagedKnowledgeItem, item_id)
        assert current is not None
        assert current.version == 3
        assert current.knowledge_key == "operations.window"
        assert current.content == "Maintenance runs on Wednesday at 02:00 UTC."
        assert current.llm_maintainable is True


@pytest.mark.asyncio
async def test_step6_profile_delete_lock_blocks_managed_update_until_profile_is_gone(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        _knowledge_base, item = await _create_direct_item(
            db,
            profile=profile,
            knowledge_key="lifecycle.topic",
            content="The original managed knowledge.",
        )
        item_id = item.id

    async with stage6_database() as delete_db:
        locked_profile = await profile_crud.lock_for_runtime_use(
            delete_db,
            profile_id=profile.id,
            uid="user-1",
        )
        assert locked_profile is not None
        await profile_crud.delete_locked(
            delete_db,
            profile=locked_profile,
            commit=False,
        )

        async def update_after_delete() -> dict:
            async with stage6_database() as update_db:
                executor = _executor(
                    update_db,
                    profile,
                    tool_call_id="call-after-profile-delete",
                )
                return json.loads(
                    await executor.execute(
                        operation="knowledge_update",
                        knowledge_id=item_id,
                        knowledge_expected_version=1,
                        knowledge_content="This update must not be written after profile deletion.",
                    )
                )

        update_task = asyncio.create_task(update_after_delete())
        await asyncio.sleep(0.05)
        assert update_task.done() is False

        await delete_db.commit()
        payload = await asyncio.wait_for(update_task, timeout=5)

    assert payload["status"] == "failed"
    async with stage6_database() as db:
        assert await db.get(Profile, profile.id) is None
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_step6_duplicate_create_does_not_expose_existing_write_identity(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        _knowledge_base, item = await _create_direct_item(
            db,
            profile=profile,
            knowledge_key="duplicate.topic",
            content="Stable existing managed knowledge.",
        )
        await db.execute(
            update(ManagedKnowledgeItem)
            .where(ManagedKnowledgeItem.id == item.id)
            .values(
                indexed_version=item.version,
                vector_item_ids=[f"managed_{item.id}_chunk_0"],
                is_recallable=True,
                pending_job_id=None,
            )
        )
        await db.commit()

        executor = _executor(db, profile, tool_call_id="call-duplicate-create")
        payload = json.loads(
            await executor.execute(
                operation="knowledge_create",
                knowledge_key="duplicate.topic",
                knowledge_content="Stable existing managed knowledge.",
            )
        )

    assert payload["status"] == "existing_key"
    assert payload["mutation_status"] == "existing_key"
    assert payload["knowledge_base_created"] is False
    assert "job_id" not in payload
    assert "knowledge_id" not in payload
    assert "current_version" not in payload

    async with stage6_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0
        assert await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem)) == 1


@pytest.mark.asyncio
async def test_step6_failed_job_replay_reports_failed_instead_of_accepted(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        executor = _executor(db, profile, tool_call_id="call-replay-failed")
        first = json.loads(
            await executor.execute(
                operation="knowledge_create",
                knowledge_key="replay.failed",
                knowledge_content="A managed knowledge publication that will fail.",
            )
        )
        assert first["status"] == "accepted"
        job_id = first["job_id"]
        knowledge_id = first["knowledge_id"]

        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == job_id)
            .values(
                status=KnowledgeJobStatus.FAILED,
                active_change_key=None,
                locked_by=None,
                lock_until=None,
            )
        )
        await db.execute(update(ManagedKnowledgeItem).where(ManagedKnowledgeItem.id == knowledge_id).values(pending_job_id=None))
        await db.commit()

        replay_executor = _executor(db, profile, tool_call_id="call-replay-failed")
        replay = json.loads(
            await replay_executor.execute(
                operation="knowledge_create",
                knowledge_key="replay.failed",
                knowledge_content="A managed knowledge publication that will fail.",
            )
        )

    assert replay["status"] == "failed"
    assert replay["mutation_status"] == "unchanged"
    assert replay["job_id"] == job_id
    assert replay["knowledge_id"] == knowledge_id

    async with stage6_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 1
        current_job = await db.get(KnowledgeJob, job_id)
        assert current_job is not None
        assert current_job.status == KnowledgeJobStatus.FAILED


@pytest.mark.asyncio
async def test_step6_managed_update_cannot_cross_profile_boundary(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile_a, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        profile_b = Profile(
            name="Stage 6 Other Profile",
            uid="user-1",
            configs={"memory": {"enabled": True}},
        )
        db.add(profile_b)
        await db.commit()
        await db.refresh(profile_b)

        _knowledge_base_b, item_b = await _create_direct_item(
            db,
            profile=profile_b,
            knowledge_key="profile-b.topic",
            content="Knowledge owned by profile B.",
        )
        item_b_id = item_b.id
        item_b_version = item_b.version

        executor = _executor(db, profile_a, tool_call_id="call-cross-profile-update")
        payload = json.loads(
            await executor.execute(
                operation="knowledge_update",
                knowledge_id=item_b_id,
                knowledge_expected_version=item_b_version,
                knowledge_content="Profile A must not overwrite profile B.",
            )
        )

    assert payload["status"] == "failed"
    async with stage6_database() as db:
        current = await db.get(ManagedKnowledgeItem, item_b_id)
        assert current is not None
        assert current.content == "Knowledge owned by profile B."
        assert current.version == item_b_version
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_step6_managed_knowledge_base_delete_cascades_and_recreates_new_container(
    stage6_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_runtime(stage6_database)

    async with stage6_database() as db:
        executor = _executor(db, profile, tool_call_id="call-before-kb-delete")
        first = json.loads(
            await executor.execute(
                operation="knowledge_create",
                knowledge_key="lifecycle.recreate",
                knowledge_content="The original managed knowledge container content.",
            )
        )
        assert first["status"] == "accepted"
        old_knowledge_id = first["knowledge_id"]
        old_job_id = first["job_id"]

        old_knowledge_base = await db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.uid == "user-1",
                KnowledgeBase.managed_profile_id == profile.id,
                KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
            )
        )
        assert old_knowledge_base is not None
        old_knowledge_base_id = old_knowledge_base.id
        old_collection_name = old_knowledge_base.active_collection_name

        await delete_owned_knowledge_base(
            db,
            knowledge_base_id=old_knowledge_base_id,
            requester_uid="user-1",
            is_superuser=False,
        )

    async with stage6_database() as db:
        assert await db.get(KnowledgeBase, old_knowledge_base_id) is None
        assert await db.get(ManagedKnowledgeItem, old_knowledge_id) is None
        assert await db.get(KnowledgeJob, old_job_id) is None
        assert await db.scalar(select(func.count()).select_from(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == old_knowledge_base_id)) == 0
        old_owner = await db.get(KnowledgeBaseCollectionOwner, old_collection_name)
        assert old_owner is not None
        assert old_owner.knowledge_base_id is None

        executor = _executor(db, profile, tool_call_id="call-after-kb-delete")
        second = json.loads(
            await executor.execute(
                operation="knowledge_create",
                knowledge_key="lifecycle.recreate",
                knowledge_content="The recreated managed knowledge container content.",
            )
        )
        assert second["status"] == "accepted"
        assert second["knowledge_base_created"] is True

        new_knowledge_base = await db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.uid == "user-1",
                KnowledgeBase.managed_profile_id == profile.id,
                KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
            )
        )
        assert new_knowledge_base is not None
        assert new_knowledge_base.id != old_knowledge_base_id
        assert new_knowledge_base.active_collection_name != old_collection_name


@pytest.mark.asyncio
async def test_step6_profile_create_retries_once_after_container_conflict(
    stage6_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, _channel = await _create_runtime(stage6_database)
    original = knowledge_job_manager_module.get_or_create_managed_knowledge_base
    call_count = 0

    async def conflict_once(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ManagedKnowledgeContainerConflictError()
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        knowledge_job_manager_module,
        "get_or_create_managed_knowledge_base",
        conflict_once,
    )

    async with stage6_database() as db:
        result = await knowledge_job_manager_module.knowledge_job_manager.submit_create_for_profile(
            db,
            uid="user-1",
            profile_id=profile.id,
            knowledge_key="retry.container",
            content="A stable managed knowledge item after container retry.",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
            dedupe_key="stage6-container-retry",
            source_session_id="session-stage6",
            source_message_id=101,
        )

    assert call_count == 2
    assert result.knowledge_base_created is True
    assert result.item is not None
    assert result.job is not None

    async with stage6_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 1
        assert await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem)) == 1
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 1
