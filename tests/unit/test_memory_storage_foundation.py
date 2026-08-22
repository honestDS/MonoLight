import re
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import SQLModel

import app.models as app_models
from app.core.constants import (
    ERR_MEMORY_ACTIVE_MUTATION_KEY_CLEAR_STATUS_INVALID,
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_POLICY_VERSION,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.crud.memory import (
    memory_embedding_delta_crud,
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.i18n import t
from app.core.memory import build_memory_collection_name, build_memory_vector_item_id
from app.models.memory import (
    LongTermMemoryCapacityStatus,
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingDeltaStatus,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
    LongTermMemorySource,
    LongTermMemoryStore,
)

MEMORY_TABLES = [
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryEmbeddingDelta.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
]


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=MEMORY_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def test_long_term_memory_v2_models_expose_defaults_enums_constraints_and_indexes():
    store = LongTermMemoryStore.model_validate({"uid": "model-user"})
    record = LongTermMemoryRecord.model_validate({"uid": "model-user"})
    revision = LongTermMemoryRevision.model_validate({"uid": "model-user", "memory_id": 1, "version": 1})

    assert store.max_active_records == MEMORY_MAX_ACTIVE_RECORDS
    assert store.organize_trigger_records == MEMORY_ORGANIZE_TRIGGER_RECORDS
    assert store.auto_organize_enabled is False
    assert store.organization_policy_version == MEMORY_ORGANIZE_POLICY_VERSION
    assert store.capacity_status == LongTermMemoryCapacityStatus.NORMAL
    assert record.content_token_count == 0
    assert record.pinned is False
    assert record.last_recalled_at is None
    assert revision.content_token_count == 0
    assert LongTermMemorySource.AUTO_ORGANIZE.value == "auto_organize"
    assert LongTermMemoryMutationOperation.CREATE_WITH_EVICTION.value == "create_with_eviction"
    assert LongTermMemoryMutationOperation.ORGANIZE.value == "organize"
    assert LongTermMemoryMutationOperation.ORGANIZE_MERGE.value == "organize_merge"
    assert app_models.LongTermMemoryCapacityStatus is LongTermMemoryCapacityStatus
    assert app_models.LongTermMemoryMutationOperation is LongTermMemoryMutationOperation
    assert app_models.LongTermMemorySource is LongTermMemorySource

    assert {
        "organize_trigger_records",
        "auto_organize_enabled",
        "organization_channel_id",
        "organization_model_id",
        "organization_policy_version",
        "organization_last_job_id",
        "organization_last_run_at",
        "organization_error",
        "capacity_status",
    } <= set(LongTermMemoryStore.__table__.c.keys())
    assert {"content_token_count", "pinned", "last_recalled_at"} <= set(LongTermMemoryRecord.__table__.c.keys())
    assert "content_token_count" in LongTermMemoryRevision.__table__.c
    assert LongTermMemoryRecord.__table__.dialect_options["sqlite"].get("autoincrement") is True

    eviction_index = next(index for index in LongTermMemoryRecord.__table__.indexes if index.name == "ix_ltm_record_eviction_candidate")
    assert tuple(eviction_index.columns.keys()) == (
        "uid",
        "is_active",
        "pinned",
        "last_recalled_at",
        "updated_at",
        "id",
    )

    with pytest.raises(ValidationError):
        LongTermMemoryStore.model_validate({"uid": "model-user", "max_active_records": 51})
    with pytest.raises(ValidationError):
        LongTermMemoryStore.model_validate({"uid": "model-user", "organize_trigger_records": 44})


def test_long_term_memory_v2_models_compile_for_sqlite_and_mysql():
    dialects = [sqlite.dialect(), mysql.dialect()]
    v2_tables = [
        LongTermMemoryStore.__table__,
        LongTermMemoryRecord.__table__,
        LongTermMemoryRevision.__table__,
    ]

    store_columns = set(LongTermMemoryStore.__table__.c.keys())
    record_columns = set(LongTermMemoryRecord.__table__.c.keys())
    revision_columns = set(LongTermMemoryRevision.__table__.c.keys())
    assert {
        "organize_trigger_records",
        "auto_organize_enabled",
        "organization_channel_id",
        "organization_model_id",
        "organization_policy_version",
        "organization_last_job_id",
        "organization_last_run_at",
        "organization_error",
        "capacity_status",
    } <= store_columns
    assert {"content_token_count", "pinned", "last_recalled_at"} <= record_columns
    assert "content_token_count" in revision_columns
    assert any(index.name == "ix_ltm_record_eviction_candidate" for index in LongTermMemoryRecord.__table__.indexes)

    for dialect in dialects:
        for table in v2_tables:
            assert str(CreateTable(table).compile(dialect=dialect)).strip()
            for index in table.indexes:
                assert str(CreateIndex(index).compile(dialect=dialect)).strip()
    assert "AUTOINCREMENT" in str(CreateTable(LongTermMemoryRecord.__table__).compile(dialect=sqlite.dialect())).upper()


async def _create_record(
    db: AsyncSession,
    *,
    uid: str,
    memory_key: str,
    content_hash: str,
    is_active: bool = True,
) -> LongTermMemoryRecord:
    return await memory_record_crud.create(
        db,
        uid=uid,
        memory_key=memory_key,
        content=memory_key,
        content_hash=content_hash,
        is_active=is_active,
    )


@pytest.mark.asyncio
async def test_record_crud_isolates_get_list_update_delete_and_count_by_uid(db_session: AsyncSession):
    record_a = await _create_record(
        db_session,
        uid="user-a",
        memory_key="shared-key",
        content_hash="shared-hash",
    )
    record_b_shared = await _create_record(
        db_session,
        uid="user-b",
        memory_key="shared-key",
        content_hash="shared-hash",
    )
    record_b_private = await _create_record(
        db_session,
        uid="user-b",
        memory_key="private-b",
        content_hash="private-hash",
    )

    assert await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record_b_shared.id) is None
    assert await memory_record_crud.get_by_key(db_session, uid="user-a", memory_key="private-b") is None
    assert await memory_record_crud.get_by_memory_key(db_session, uid="user-a", memory_key="private-b") is None
    assert await memory_record_crud.get_by_content_hash(db_session, uid="user-a", content_hash="private-hash") is None
    assert [item.id for item in await memory_record_crud.list_by_uid(db_session, uid="user-a")] == [record_a.id]
    assert [item.id for item in await memory_record_crud.get_page(db_session, uid="user-a")] == [record_a.id]
    assert await memory_record_crud.count_active(db_session, uid="user-a") == 1
    assert await memory_record_crud.count_active(db_session, uid="user-b") == 2

    assert (
        await memory_record_crud.update_if_version(
            db_session,
            uid="user-a",
            memory_id=record_b_private.id,
            expected_version=0,
            content="must-not-update",
        )
        is None
    )
    assert await memory_record_crud.delete(db_session, uid="user-a", memory_id=record_b_private.id) is None
    assert (await memory_record_crud.get_by_id(db_session, uid="user-b", memory_id=record_b_private.id)).content == "private-b"

    assert await memory_record_crud.delete(db_session, uid="user-a", memory_id=record_a.id) is not None
    assert await memory_record_crud.count_active(db_session, uid="user-a") == 0
    assert await memory_record_crud.count_active(db_session, uid="user-b") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duplicate_memory_key", "duplicate_content_hash"),
    [("same-key", "different-hash"), ("different-key", "same-hash")],
)
async def test_record_unique_key_and_hash_are_scoped_to_one_uid(
    db_session: AsyncSession,
    duplicate_memory_key: str,
    duplicate_content_hash: str,
):
    await _create_record(
        db_session,
        uid="user-a",
        memory_key="same-key",
        content_hash="same-hash",
    )

    with pytest.raises(IntegrityError):
        await _create_record(
            db_session,
            uid="user-a",
            memory_key=duplicate_memory_key,
            content_hash=duplicate_content_hash,
        )
    await db_session.rollback()

    other_user_record = await _create_record(
        db_session,
        uid="user-b",
        memory_key="same-key",
        content_hash="same-hash",
    )
    assert other_user_record.uid == "user-b"


@pytest.mark.asyncio
async def test_record_update_if_version_requires_uid_id_and_expected_version(db_session: AsyncSession):
    record = await _create_record(
        db_session,
        uid="user-a",
        memory_key="key-1",
        content_hash="hash-1",
    )

    updated = await memory_record_crud.update_if_version(
        db_session,
        uid="user-a",
        memory_id=record.id,
        expected_version=0,
        content="version-one",
    )
    assert updated is not None
    assert updated.version == 1
    assert updated.content == "version-one"

    for uid, memory_id, expected_version in (
        ("user-a", record.id, 0),
        ("user-b", record.id, 1),
        ("user-a", record.id + 1000, 1),
    ):
        assert (
            await memory_record_crud.update_if_version(
                db_session,
                uid=uid,
                memory_id=memory_id,
                expected_version=expected_version,
                content="must-not-update",
            )
            is None
        )
        current = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record.id)
        assert current.version == 1
        assert current.content == "version-one"


@pytest.mark.asyncio
async def test_store_get_or_create_is_idempotent_and_update_by_uid_isolated(db_session: AsyncSession):
    store_a, created = await memory_store_crud.get_or_create(
        db_session,
        uid="user-a",
        max_active_records=10,
    )
    same_store, created_again = await memory_store_crud.get_or_create(
        db_session,
        uid="user-a",
        max_active_records=99,
    )
    store_b, _ = await memory_store_crud.get_or_create(db_session, uid="user-b")

    assert created is True
    assert created_again is False
    assert same_store.id == store_a.id
    assert same_store.max_active_records == 10

    updated_b = await memory_store_crud.update_by_uid(
        db_session,
        uid="user-b",
        active_embedding_revision=4,
    )
    assert updated_b is not None
    assert updated_b.id == store_b.id
    assert updated_b.active_embedding_revision == 4
    assert (await memory_store_crud.get_by_uid(db_session, uid="user-a")).active_embedding_revision == 0
    assert await memory_store_crud.update_by_uid(db_session, uid="missing-user", active_embedding_revision=8) is None


@pytest.mark.asyncio
async def test_revision_crud_scopes_writes_and_reads_by_uid(db_session: AsyncSession):
    revision_a = await memory_revision_crud.write(
        db_session,
        uid="user-a",
        memory_id=101,
        version=1,
        memory_key="a-key",
        content="a-content",
    )
    revision_b = await memory_revision_crud.create(
        db_session,
        uid="user-b",
        memory_id=202,
        version=1,
        memory_key="b-key",
        content="b-content",
    )

    assert await memory_revision_crud.get_by_memory_id(db_session, uid="user-a", memory_id=101, version=1) is not None
    assert await memory_revision_crud.get_by_memory_id(db_session, uid="user-a", memory_id=202) is None
    assert [item.id for item in await memory_revision_crud.list_by_memory_id(db_session, uid="user-a", memory_id=101)] == [revision_a.id]
    assert await memory_revision_crud.get_by_memory_id(db_session, uid="user-b", memory_id=101) is None
    assert revision_b.uid == "user-b"


@pytest.mark.asyncio
async def test_embedding_revision_list_and_update_are_scoped_by_uid(db_session: AsyncSession):
    revision_a = await memory_embedding_revision_crud.write(
        db_session,
        uid="user-a",
        revision=1,
        to_signature="signature-a",
    )
    revision_b = await memory_embedding_revision_crud.create(
        db_session,
        uid="user-b",
        revision=2,
        to_signature="signature-b",
    )

    assert [item.id for item in await memory_embedding_revision_crud.list_by_uid(db_session, uid="user-a")] == [revision_a.id]
    assert await memory_embedding_revision_crud.get_by_revision(db_session, uid="user-a", revision=2) is None

    updated = await memory_embedding_revision_crud.update_by_revision(
        db_session,
        uid="user-a",
        revision=1,
        status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
    )
    assert updated is not None
    assert updated.status == LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED
    assert (
        await memory_embedding_revision_crud.update_by_revision(
            db_session,
            uid="user-a",
            revision=2,
            status=LongTermMemoryEmbeddingRevisionStatus.FAILED,
        )
        is None
    )
    assert (await memory_embedding_revision_crud.get_by_revision(db_session, uid="user-b", revision=2)).status == LongTermMemoryEmbeddingRevisionStatus.CONFIRMED
    assert revision_b.uid == "user-b"


@pytest.mark.asyncio
async def test_embedding_delta_list_high_water_and_update_are_scoped_by_uid(db_session: AsyncSession):
    delta_a = await memory_embedding_delta_crud.write(
        db_session,
        uid="user-a",
        migration_job_id=10,
        sequence=1,
        snapshot={"uid": "user-a"},
    )
    await memory_embedding_delta_crud.create(
        db_session,
        uid="user-b",
        migration_job_id=20,
        sequence=1,
        snapshot={"uid": "user-b"},
    )

    assert [item.id for item in await memory_embedding_delta_crud.list_by_migration_job(db_session, uid="user-a", migration_job_id=10)] == [delta_a.id]
    assert await memory_embedding_delta_crud.list_by_migration_job(db_session, uid="user-a", migration_job_id=20) == []
    assert await memory_embedding_delta_crud.get_high_water_sequence(db_session, uid="user-a", migration_job_id=10) == 1
    assert await memory_embedding_delta_crud.get_high_water_sequence(db_session, uid="user-a", migration_job_id=20) == 0

    updated = await memory_embedding_delta_crud.update_status(
        db_session,
        uid="user-a",
        migration_job_id=10,
        sequence=1,
        status=LongTermMemoryEmbeddingDeltaStatus.APPLIED,
    )
    assert updated is not None
    assert updated.status == LongTermMemoryEmbeddingDeltaStatus.APPLIED
    assert (
        await memory_embedding_delta_crud.update_status(
            db_session,
            uid="user-a",
            migration_job_id=20,
            sequence=1,
            status=LongTermMemoryEmbeddingDeltaStatus.FAILED,
        )
        is None
    )
    assert (await memory_embedding_delta_crud.list_by_migration_job(db_session, uid="user-b", migration_job_id=20))[0].status == LongTermMemoryEmbeddingDeltaStatus.PENDING


async def _create_job(
    db: AsyncSession,
    *,
    uid: str,
    dedupe_key: str,
    active_mutation_key: str | None = None,
) -> tuple[LongTermMemoryMutationJob, bool]:
    return await memory_job_crud.create_job(
        db,
        uid=uid,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key=dedupe_key,
        active_mutation_key=active_mutation_key,
        payload={"dedupe_key": dedupe_key},
    )


@pytest.mark.asyncio
async def test_job_retry_by_uid_and_dedupe_returns_existing_job(db_session: AsyncSession):
    first, first_created = await _create_job(db_session, uid="user-a", dedupe_key="request-1")
    retry, retry_created = await _create_job(db_session, uid="user-a", dedupe_key="request-1")

    assert first_created is True
    assert retry_created is False
    assert retry.id == first.id


@pytest.mark.asyncio
async def test_job_rejects_different_dedupe_keys_using_same_active_key(db_session: AsyncSession):
    await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="request-1",
        active_mutation_key="memory-1",
    )

    with pytest.raises(IntegrityError):
        await _create_job(
            db_session,
            uid="user-a",
            dedupe_key="request-2",
            active_mutation_key="memory-1",
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_job_terminal_clear_releases_active_key_for_next_job(db_session: AsyncSession):
    first, _ = await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="request-1",
        active_mutation_key="memory-1",
    )

    completed = await memory_job_crud.update_status(
        db_session,
        uid="user-a",
        job_id=first.id,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        clear_active_mutation_key=True,
    )
    next_job, next_created = await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="request-2",
        active_mutation_key="memory-1",
    )

    assert completed is not None
    assert completed.active_mutation_key is None
    assert next_created is True
    assert next_job.id != first.id


@pytest.mark.asyncio
async def test_job_cannot_clear_active_key_before_terminal_status(db_session: AsyncSession):
    job, _ = await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="request-1",
        active_mutation_key="memory-1",
    )

    with pytest.raises(ValueError, match=re.escape(t(ERR_MEMORY_ACTIVE_MUTATION_KEY_CLEAR_STATUS_INVALID))):
        await memory_job_crud.update_status(
            db_session,
            uid="user-a",
            job_id=job.id,
            status=LongTermMemoryMutationStatus.RUNNING,
            clear_active_mutation_key=True,
        )

    current = await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=job.id)
    assert current.active_mutation_key == "memory-1"
    assert current.status == LongTermMemoryMutationStatus.PENDING


@pytest.mark.asyncio
async def test_job_queries_are_isolated_by_uid(db_session: AsyncSession):
    job_a, _ = await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="shared-request",
        active_mutation_key="active-a",
    )
    job_b, _ = await _create_job(
        db_session,
        uid="user-b",
        dedupe_key="shared-request",
        active_mutation_key="active-b",
    )

    assert [job.id for job in await memory_job_crud.list_by_uid(db_session, uid="user-a")] == [job_a.id]
    assert await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=job_b.id) is None
    assert await memory_job_crud.get_by_dedupe_key(db_session, uid="user-a", dedupe_key="shared-request") == job_a
    assert await memory_job_crud.get_by_active_mutation_key(db_session, uid="user-a", active_mutation_key="active-b") is None


@pytest.mark.parametrize(
    ("uid", "signature", "revision", "purpose"),
    [
        ("", "model-v1", 1, "fact"),
        ("user-a", "", 1, "fact"),
        ("user-a", "model-v1", True, "fact"),
        ("user-a", "model-v1", -1, "fact"),
        ("user-a", "model-v1", 10_000_000_000, "fact"),
        ("user-a", "model-v1", 1, "Fact"),
        ("user-a", "model-v1", 1, "bad purpose"),
        ("user-a", "model-v1", 1, "a" * 17),
    ],
)
def test_memory_collection_name_rejects_invalid_parameters(uid, signature, revision, purpose):
    with pytest.raises(ValueError):
        build_memory_collection_name(uid, signature, revision, purpose)


def test_memory_collection_name_is_stable_unique_and_chroma_compatible():
    uid = "user-1234567890@example.test"
    name = build_memory_collection_name(uid, "provider:model-v1", 7, "fact")

    assert name == build_memory_collection_name(uid, "provider:model-v1", 7, "fact")
    assert uid not in name
    assert name != build_memory_collection_name("other-user", "provider:model-v1", 7, "fact")
    assert name != build_memory_collection_name(uid, "provider:model-v2", 7, "fact")
    assert name != build_memory_collection_name(uid, "provider:model-v1", 8, "fact")
    assert name != build_memory_collection_name(uid, "provider:model-v1", 7, "profile")
    assert 3 <= len(name) <= 63
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,61}[A-Za-z0-9]", name)


@pytest.mark.parametrize(
    "memory_id",
    [0, -1, True, "1"],
)
def test_memory_vector_item_id_rejects_invalid_memory_id(memory_id):
    with pytest.raises(ValueError):
        build_memory_vector_item_id(memory_id, 1)


@pytest.mark.parametrize(
    "version",
    [0, -1, True, "1"],
)
def test_memory_vector_item_id_rejects_invalid_version(version):
    with pytest.raises(ValueError):
        build_memory_vector_item_id(1, version)


def test_memory_vector_item_id_is_versioned():
    assert build_memory_vector_item_id(42, 1) == "memory_42_v1"
    assert build_memory_vector_item_id(42, 2) != build_memory_vector_item_id(42, 1)
